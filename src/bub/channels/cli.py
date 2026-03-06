"""CLI channel adapter."""

from __future__ import annotations

import asyncio
import inspect
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from hashlib import md5
from pathlib import Path
from typing import Any

from loguru import logger
from prompt_toolkit import PromptSession
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.history import FileHistory
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from rich import get_console

from bub.app.runtime import AppRuntime
from bub.channels.base import BaseChannel
from bub.cli.builtin_commands import register_builtin_commands
from bub.cli.commands import CommandContext, CommandRegistry
from bub.cli.render import CliRenderer
from bub.core.agent_loop import LoopResult

# Double Ctrl-C within this window triggers a force-cancel.
_DOUBLE_CTRL_C_WINDOW = 1.5


class CliChannel(BaseChannel[str]):
    """Interactive terminal channel with always-visible input prompt.

    The input loop runs continuously. Agent execution happens in a background
    task so the prompt stays visible at the bottom of the terminal at all times.
    User input during agent execution is injected into the next agent step.

    Interruption:
        - Ctrl-C once: graceful stop (finishes current step, then exits loop)
        - Ctrl-C twice (within 1.5s) or /stop: force-cancel the agent task
    """

    name = "cli"

    def __init__(self, runtime: AppRuntime, *, session_id: str = "cli") -> None:
        super().__init__(runtime)
        self._session_id = session_id
        self._session = runtime.get_session(session_id)
        self._renderer = CliRenderer(get_console())
        self._mode = "agent"
        self._last_tape_info: object | None = None
        self._stop_requested = False
        self._agent_running = False
        self._agent_task: asyncio.Task[None] | None = None
        self._on_receive: Callable[[str], Awaitable[None]] | None = None
        self._last_interrupt: float = 0.0
        self._task_cache: str = ""
        self._task_cache_time: float = 0.0

        # Slash command registry.
        self._command_registry = CommandRegistry()
        register_builtin_commands(self._command_registry)

        # Build prompt after registry is ready (needs tool names).
        self._prompt = self._build_prompt()

        # Wire up live callback for step/sub-agent/tool visibility.
        self._session.model_runner.set_live_callback(self._on_live_event)

    @property
    def command_registry(self) -> CommandRegistry:
        return self._command_registry

    @property
    def debounce_enabled(self) -> bool:
        return False

    def _on_live_event(self, event: str, data: dict[str, Any]) -> None:
        """Callback invoked by ModelRunner during execution."""
        self._renderer.live_event(event, data)

    async def start(self, on_receive: Callable[[str], Awaitable[None]]) -> None:
        self._on_receive = on_receive
        self._renderer.welcome(model=self.runtime.settings.model, workspace=str(self.runtime.workspace))
        await self._refresh_tape_info()

        # Keep patch_stdout active for the entire session so the prompt
        # always stays at the bottom, even while the agent prints output.
        with patch_stdout(raw=True):
            while not self._stop_requested:
                try:
                    raw = (await self._prompt.prompt_async(self._prompt_message())).strip()
                except KeyboardInterrupt:
                    self._handle_interrupt()
                    continue
                except EOFError:
                    break

                if not raw:
                    continue

                # Handle slash commands.
                if await self._handle_slash_command(raw):
                    continue

                # If agent is running, inject message into its loop.
                if self._agent_running:
                    self._session.inject_message(raw)
                    self._renderer.info(f"Queued message (will be injected at next step): {raw}")
                    continue

                # Launch agent as a background task so the input loop continues.
                request = self._normalize_input(raw)
                request = self._expand_at_references(request)
                self._display_user_input(raw)
                self._agent_running = True
                self._agent_task = asyncio.create_task(self._run_agent(on_receive, request))

        self._renderer.info("Bye.")

    def _handle_interrupt(self) -> None:
        """Handle Ctrl-C: graceful stop first, force-cancel on double press."""
        now = time.monotonic()
        if not self._agent_running:
            self._renderer.info("No agent running. Press Ctrl-D to exit.")
            return

        if now - self._last_interrupt < _DOUBLE_CTRL_C_WINDOW:
            # Double Ctrl-C -> force-cancel.
            self.force_cancel()
        else:
            # First Ctrl-C -> graceful stop.
            self._last_interrupt = now
            self._session.model_runner.request_stop()
            self._renderer.info("Stopping after current step... (Ctrl-C again to force-cancel)")

    def force_cancel(self) -> None:
        """Force-cancel the running agent task."""
        if self._agent_task is not None and not self._agent_task.done():
            self._agent_task.cancel()
            self._renderer.info("Force-cancelled agent.")
        self._last_interrupt = 0.0

    async def _run_agent(self, on_receive: Callable[[str], Awaitable[None]], request: str) -> None:
        """Execute the agent in the background, keeping the input loop free."""
        try:
            await on_receive(request)
        except asyncio.CancelledError:
            self._renderer.info("Agent cancelled.")
        except Exception:
            logger.exception("cli.agent.error")
        finally:
            self._agent_running = False
            self._agent_task = None
            # Refresh prompt symbol by invalidating the prompt app.
            if self._prompt.app and self._prompt.app.is_running:
                self._prompt.app.invalidate()

    async def _handle_slash_command(self, raw: str) -> bool:
        """Handle slash commands via the command registry. Returns True if handled."""
        if not self._command_registry.is_command(raw):
            return False

        ctx = CommandContext(
            renderer=self._renderer,
            session=self._session,
            channel=self,
            agent_running=self._agent_running,
        )
        result = self._command_registry.execute(raw, ctx)

        # Handle both sync and async results.
        if inspect.isawaitable(result):
            result = await result

        if result is not None:
            self._renderer.info(str(result))
        return True

    def is_mentioned(self, message: str) -> bool:
        _ = message
        return True

    async def get_session_prompt(self, message: str) -> tuple[str, str]:
        return self._session_id, message

    def format_prompt(self, prompt: str) -> str:
        return prompt

    async def process_output(self, session_id: str, output: LoopResult) -> None:
        _ = session_id
        await self._refresh_tape_info()
        if output.immediate_output:
            self._renderer.command_output(output.immediate_output)
        if output.error:
            self._renderer.error(output.error)
        if output.assistant_output:
            self._renderer.assistant_output(output.assistant_output)
        if output.exit_requested:
            self._stop_requested = True

    async def _refresh_tape_info(self) -> None:
        try:
            self._last_tape_info = await self._session.tape.info()
        except Exception as exc:
            self._last_tape_info = None
            logger.debug("cli.tape_info.unavailable session_id={} error={}", self._session_id, exc)

    def _build_prompt(self) -> PromptSession[str]:
        kb = KeyBindings()

        @kb.add("c-x", eager=True)
        def _toggle_mode(event) -> None:
            self._mode = "shell" if self._mode == "agent" else "agent"
            event.app.invalidate()

        def _tool_sort_key(tool_name: str) -> tuple[str, str]:
            section, _, name = tool_name.rpartition(".")
            return (section, name)

        history_file = self._history_file(self.runtime.settings.resolve_home(), self.runtime.workspace)
        history_file.parent.mkdir(parents=True, exist_ok=True)
        history = FileHistory(str(history_file))

        # Build completions: tool names (,prefix) + slash commands (/prefix).
        tool_names = sorted((f",{tool}" for tool in self._session.tool_view.all_tools()), key=_tool_sort_key)
        slash_cmds = [f"/{cmd.name}" for cmd in self._command_registry.list_commands()]
        slash_aliases = [f"/{a}" for cmd in self._command_registry.list_commands() for a in cmd.aliases]
        completions = tool_names + slash_cmds + slash_aliases

        completer = WordCompleter(completions, ignore_case=True)
        return PromptSession(
            completer=completer,
            complete_while_typing=True,
            key_bindings=kb,
            history=history,
            bottom_toolbar=self._render_bottom_toolbar,
        )

    def _prompt_message(self) -> FormattedText:
        cwd = Path.cwd().name
        if self._agent_running:
            symbol = "\u21aa"
        elif self._mode == "agent":
            symbol = ">"
        else:
            symbol = ","
        return FormattedText([("bold", f"{cwd} {symbol} ")])

    def _render_bottom_toolbar(self) -> FormattedText:
        info = self._last_tape_info
        now = datetime.now().strftime("%H:%M")
        left = f"{now}  mode:{self._mode}"
        if self._agent_running:
            paused = getattr(self._session.model_runner, "_paused", False)
            if paused:
                left += "  [paused]"
            else:
                left += "  [running]"
        task_summary = self._get_task_summary()
        panels_count = len(self._renderer.panels)
        right = (
            f"panels:{panels_count}  "
            f"model:{self.runtime.settings.model}  "
            f"entries:{getattr(info, 'entries', '-')} "
            f"anchors:{getattr(info, 'anchors', '-')} "
            f"last:{getattr(info, 'last_anchor', None) or '-'}"
        )
        if task_summary:
            right = f"{task_summary}  {right}"
        return FormattedText([("", f"{left}  {right}")])

    def _get_task_summary(self) -> str:
        """Read task file and return summary string, cached for 5 seconds."""
        now = time.monotonic()
        if now - self._task_cache_time < 5:
            return self._task_cache
        from bub.tools.task import _load_tasks

        tasks = _load_tasks(self.runtime.workspace)
        if not tasks:
            result = ""
        else:
            done = sum(1 for t in tasks if t.get("status") == "completed")
            active = sum(1 for t in tasks if t.get("status") == "in_progress")
            total = len(tasks)
            parts = [f"tasks:{done}/{total}"]
            if active:
                parts.append(f"active:{active}")
            result = " ".join(parts)
        self._task_cache = result
        self._task_cache_time = now
        return result

    def _display_user_input(self, raw: str) -> None:
        """Show user input in a compact form — collapse long pastes."""
        lines = raw.splitlines()
        if len(lines) > 5:
            preview = "\n".join(lines[:3])
            self._renderer.info(f"{preview}\n[... pasted {len(lines)} lines total]")

    def _expand_at_references(self, text: str) -> str:
        """Expand @file and @folder references to inline content."""
        import re

        def _replace(match: re.Match[str]) -> str:
            path_str = match.group(1)
            p = Path(path_str).expanduser()
            if not p.is_absolute():
                p = self.runtime.workspace / p
            if p.is_file():
                try:
                    content = p.read_text(encoding="utf-8", errors="replace")
                except Exception as exc:
                    return f"[error reading {p}: {exc}]"
                else:
                    return f"\n<file path=\"{p}\">\n{content}\n</file>\n"
            elif p.is_dir():
                try:
                    entries = sorted(p.iterdir())
                except Exception as exc:
                    return f"[error listing {p}: {exc}]"
                else:
                    listing = "\n".join(
                        f"  {'d' if e.is_dir() else 'f'} {e.name}" for e in entries[:100]
                    )
                    if len(entries) > 100:
                        listing += f"\n  ... ({len(entries)} entries total)"
                    return f"\n<directory path=\"{p}\">\n{listing}\n</directory>\n"
            else:
                return match.group(0)  # not a valid path, keep original

        return re.sub(r"@([\w/.~-]+(?:[\w/.-]+)*)", _replace, text)

    def _normalize_input(self, raw: str) -> str:
        if self._mode != "shell":
            return raw
        if raw.startswith(","):
            return raw
        return f", {raw}"

    @staticmethod
    def _history_file(home: Path, workspace: Path) -> Path:
        workspace_hash = md5(str(workspace).encode("utf-8")).hexdigest()  # noqa: S324
        return home / "history" / f"{workspace_hash}.history"

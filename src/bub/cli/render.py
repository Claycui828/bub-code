"""CLI rendering helpers with collapsible output panels."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    from pathlib import Path

    from bub.app.runtime import SessionRuntime

FOLD_SUMMARY_MAX = 80


@dataclass
class OutputPanel:
    """One tracked output block that can be folded/unfolded."""

    index: int
    title: str
    body: str
    style: str
    kind: str = "generic"  # "assistant" | "command" | "error" | "tool" | "sub_agent" | "system"
    folded: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class CliRenderer:
    """Rich-based renderer for interactive CLI with collapsible panels."""

    console: Console
    panels: list[OutputPanel] = field(default_factory=list)

    # -- public rendering --

    def welcome(self, *, model: str, workspace: str) -> None:
        body = (
            f"workspace: {workspace}\n"
            f"model: {model}\n"
            "internal command prefix: ','\n"
            "shell command prefix: ',' at line start (Ctrl-X for shell mode)\n"
            "type '/help' for slash commands, ',help' for agent commands"
        )
        self.console.print(Panel(body, title="Bub", border_style="cyan"))

    def info(self, text: str) -> None:
        if not text.strip():
            return
        self.console.print(Text(text, style="bright_black"))

    def command_output(self, text: str) -> None:
        if not text.strip():
            return
        self._add_panel("Command", text, "green", kind="command")

    def assistant_output(self, text: str) -> None:
        if not text.strip():
            return
        self._add_panel("Assistant", text, "blue", kind="assistant")

    def error(self, text: str) -> None:
        if not text.strip():
            return
        self._add_panel("Error", text, "red", kind="error")

    def live_event(self, event: str, data: dict) -> None:
        """Render a live event from the agent loop."""
        handler = self._LIVE_HANDLERS.get(event)
        if handler:
            handler(self, data)

    # -- main agent events --

    def _render_step_start(self, data: dict) -> None:
        step = data.get("step", "?")
        model = data.get("model", "")
        self.console.print(Text(f"  \u27f3 step {step}  {model}", style="dim cyan"))

    def _render_user_injected(self, data: dict) -> None:
        for msg in data.get("messages", []):
            self.console.print(Text(f"  \u21aa injected: {msg}", style="bold yellow"))

    def _render_tool_start(self, data: dict) -> None:
        name = data.get("name", "?")
        args_summary = data.get("args_summary", "")
        self.console.print(Text(f"  \U0001f527 {name}: {args_summary}", style="dim green"))

    def _render_tool_end(self, data: dict) -> None:
        name = data.get("name", "?")
        status = data.get("status", "ok")
        elapsed = data.get("elapsed_ms", 0)
        preview = data.get("output_preview", "")
        style, symbol = ("green", "\u2713") if status == "ok" else ("red", "\u2717")
        line = f"  {symbol} {name} ({elapsed:.0f}ms)"
        if preview:
            line += f" \u2014 {preview}"
        self.console.print(Text(line, style=style))

    def _render_tool_error(self, data: dict) -> None:
        name = data.get("name", "?")
        error = data.get("error", "")
        self.console.print(Text(f"  \u2717 {name}: {error}", style="bold red"))

    def _render_think_start(self, data: dict) -> None:
        step = data.get("step", "?")
        model = data.get("model", "")
        self.console.print(Text(f"  \U0001f4ad thinking... (step {step}, {model})", style="dim cyan"))

    def _render_think_end(self, data: dict) -> None:
        step = data.get("step", "?")
        if data.get("has_tool_calls", False):
            self.console.print(Text(f"  \U0001f4ad step {step}: tool calls requested", style="dim cyan"))

    def _render_tape_anchor(self, data: dict) -> None:
        name = data.get("name", "")
        summary = data.get("summary", "")
        self.console.print()
        self.console.print(Text(f"  \U0001f4cc anchor: \"{name}\"", style="bold yellow"))
        if summary:
            self.console.print(Text(f"     {summary}", style="dim yellow"))
        self.console.print()

    def _render_step_paused(self, data: dict) -> None:
        step = data.get("step", "?")
        reason = data.get("reason", "")
        self.console.print(Text(f"  \u23f8 paused after step {step} ({reason})", style="bold yellow"))

    # -- sub-agent events (streaming box display) --

    def _render_sub_agent_start(self, data: dict) -> None:
        agent_id = data.get("agent_id", "?")
        agent_type = data.get("agent_type", "general")
        desc = data.get("description", "sub-agent")
        prompt_preview = _truncate(data.get("prompt", ""), 80)
        self.console.print(Text(f"  \u250c {agent_id} [{agent_type}]: {desc}", style="bold magenta"))
        if prompt_preview:
            self.console.print(Text(f"  \u2502 {agent_id}  prompt: {prompt_preview}", style="dim magenta"))

    def _render_sub_agent_end(self, data: dict) -> None:
        agent_id = data.get("agent_id", "")
        status = data.get("status", "")
        result = data.get("result", "")
        style = "green" if status == "completed" else "red"
        self.console.print(Text(f"  \u2514 {agent_id} [{status}]", style=style))
        # Store full result as a foldable panel for later reference.
        if result:
            self._add_panel(
                f"Result: {agent_id} [{status}]",
                result,
                style,
                kind="sub_agent",
                metadata={"agent_id": agent_id, "status": status},
            )

    def _render_sub_agent_step(self, data: dict) -> None:
        agent_id = data.get("agent_id", "?")
        step = data.get("step", "?")
        self.console.print(Text(f"  \u2502 {agent_id}  \u27f3 step {step}", style="dim magenta"))

    def _render_sub_agent_tool_start(self, data: dict) -> None:
        agent_id = data.get("agent_id", "?")
        name = data.get("name", "?")
        args_summary = data.get("args_summary", "")
        self.console.print(Text(f"  \u2502 {agent_id}  \U0001f527 {name}: {args_summary}", style="dim green"))

    def _render_sub_agent_tool_end(self, data: dict) -> None:
        agent_id = data.get("agent_id", "?")
        name = data.get("name", "?")
        status = data.get("status", "ok")
        elapsed = data.get("elapsed_ms", 0)
        preview = data.get("output_preview", "")
        sym_style, symbol = ("green", "\u2713") if status == "ok" else ("red", "\u2717")
        line = f"  \u2502 {agent_id}  {symbol} {name} ({elapsed:.0f}ms)"
        if preview:
            line += f" \u2014 {preview}"
        self.console.print(Text(line, style=sym_style))

    def _render_sub_agent_tool_error(self, data: dict) -> None:
        agent_id = data.get("agent_id", "?")
        name = data.get("name", "?")
        error = data.get("error", "")
        self.console.print(Text(f"  \u2502 {agent_id}  \u2717 {name}: {error}", style="bold red"))

    def _render_sub_agent_think(self, data: dict) -> None:
        agent_id = data.get("agent_id", "?")
        step = data.get("step", "?")
        self.console.print(Text(f"  \u2502 {agent_id}  \U0001f4ad thinking... (step {step})", style="dim magenta"))

    # -- event handler map --

    _LIVE_HANDLERS: ClassVar[dict[str, Callable[[CliRenderer, dict], None]]] = {
        "step.start": _render_step_start,
        "user.injected": _render_user_injected,
        "tool.start": _render_tool_start,
        "tool.end": _render_tool_end,
        "tool.error": _render_tool_error,
        "think.start": _render_think_start,
        "think.end": _render_think_end,
        "sub_agent.start": _render_sub_agent_start,
        "sub_agent.end": _render_sub_agent_end,
        "sub_agent.step.start": _render_sub_agent_step,
        "sub_agent.tool.start": _render_sub_agent_tool_start,
        "sub_agent.tool.end": _render_sub_agent_tool_end,
        "sub_agent.tool.error": _render_sub_agent_tool_error,
        "sub_agent.think.start": _render_sub_agent_think,
        "tape.anchor": _render_tape_anchor,
        "step.paused": _render_step_paused,
    }

    # -- panel management --

    def _add_panel(self, title: str, body: str, style: str, *, kind: str = "generic", metadata: dict | None = None) -> None:
        idx = len(self.panels)
        panel = OutputPanel(index=idx, title=title, body=body, style=style, kind=kind, metadata=metadata or {})
        self.panels.append(panel)
        self._print_panel(panel)

    def _print_panel(self, panel: OutputPanel) -> None:
        tag = f"[{panel.index}]"
        if panel.folded:
            summary = _truncate(panel.body, FOLD_SUMMARY_MAX)
            self.console.print(
                Text(f"  {tag} {panel.title}: {summary} ", style="dim"),
                Text("[folded]", style="dim italic"),
            )
        else:
            self.console.print(Panel(panel.body, title=f"{tag} {panel.title}", border_style=panel.style))

    def fold_last(self) -> None:
        if not self.panels:
            self.info("No panels to fold.")
            return
        self._set_folded(len(self.panels) - 1, folded=True)

    def unfold_last(self) -> None:
        if not self.panels:
            self.info("No panels to unfold.")
            return
        self._set_folded(len(self.panels) - 1, folded=False)

    def fold_at(self, index: int) -> None:
        self._set_folded(index, folded=True)

    def unfold_at(self, index: int) -> None:
        self._set_folded(index, folded=False)

    def _set_folded(self, index: int, *, folded: bool) -> None:
        if index < 0 or index >= len(self.panels):
            raise IndexError(f"Panel index {index} out of range (0-{len(self.panels) - 1})")
        panel = self.panels[index]
        panel.folded = folded
        self._print_panel(panel)

    def list_panels(self) -> None:
        if not self.panels:
            self.info("No output panels.")
            return
        for panel in self.panels:
            state = "folded" if panel.folded else "open"
            summary = _truncate(panel.body, 60)
            self.console.print(Text(f"  [{panel.index}] {panel.title} ({state}): {summary}", style="bright_black"))

    def clear(self) -> None:
        """Clear the console and reset panels."""
        self.panels.clear()
        self.console.clear()

    def search_panels(self, query: str) -> None:
        """Search across all panels for a query string."""
        query_lower = query.lower()
        matches = [p for p in self.panels if query_lower in p.body.lower() or query_lower in p.title.lower()]
        if not matches:
            self.info(f"No panels matching '{query}'.")
            return
        self.info(f"Found {len(matches)} panel(s) matching '{query}':")
        for panel in matches:
            state = "folded" if panel.folded else "open"
            summary = _truncate(panel.body, 60)
            self.console.print(Text(f"  [{panel.index}] {panel.title} ({state}): {summary}", style="bright_black"))

    def render_status(
        self,
        *,
        agent_running: bool,
        session: SessionRuntime | None,
        model: str,
    ) -> None:
        """Render agent status overview."""
        lines: list[str] = []
        state = "running" if agent_running else "idle"
        lines.append(f"  State: {state}")
        lines.append(f"  Model: {model}")
        lines.append(f"  Panels: {len(self.panels)}")
        if session:
            runner = session.model_runner
            paused = getattr(runner, "_paused", False)
            step_mode = getattr(runner, "_step_mode", False)
            if paused:
                lines.append("  Mode: PAUSED")
            elif step_mode:
                lines.append("  Mode: SINGLE-STEP")
            queue_size = runner._message_queue.qsize()
            if queue_size:
                lines.append(f"  Queued messages: {queue_size}")
        self.console.print(Panel("\n".join(lines), title="Status", border_style="cyan"))

    async def render_context(self, session: SessionRuntime) -> None:
        """Render tape context chain."""
        try:
            info = await session.tape.info()
        except Exception:
            self.info("Could not load tape info.")
            return

        lines: list[str] = []
        lines.append(f"  Entries: {getattr(info, 'entries', '-')}")
        lines.append(f"  Anchors: {getattr(info, 'anchors', '-')}")
        last = getattr(info, "last_anchor", None)
        if last:
            lines.append(f"  Last anchor: {last}")
        tape_name = getattr(info, "name", None) or getattr(session.tape, "_tape_name", "?")
        lines.append(f"  Tape: {tape_name}")
        self.console.print(Panel("\n".join(lines), title="Context", border_style="yellow"))

    def render_tasks(self, workspace: Path) -> None:
        """Render task list panel."""
        from bub.tools.task import _load_tasks

        tasks = _load_tasks(workspace)
        if not tasks:
            self.info("No tasks.")
            return

        status_symbols = {"in_progress": "\u25b6", "pending": "\u25cb", "blocked": "\u2298", "completed": "\u2713"}
        status_order = ["in_progress", "pending", "blocked", "completed"]
        by_status: dict[str, list[dict]] = {}
        for task in tasks:
            by_status.setdefault(task.get("status", "pending"), []).append(task)

        lines: list[str] = []
        for status in status_order:
            for task in by_status.get(status, []):
                symbol = status_symbols.get(status, "?")
                lines.append(f"  {symbol} [{task['id']}] {task['title']}")
        self._add_panel("Tasks", "\n".join(lines), "yellow", kind="system")


def _truncate(text: str, max_len: int) -> str:
    """Collapse whitespace and truncate for summary display."""
    flat = " ".join(text.split())
    if len(flat) <= max_len:
        return flat
    return flat[: max_len - 1] + "\u2026"

"""Model turn runner."""

from __future__ import annotations

import asyncio
import re
import textwrap
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, ClassVar

from loguru import logger
from republic import Tool, ToolAutoResult

from bub.core.prompt_builder import PromptBuilder
from bub.core.router import AssistantRouteResult, InputRouter
from bub.observability import current_tracer
from bub.skills.loader import SkillMetadata
from bub.skills.view import render_compact_skills
from bub.tape.service import TapeService
from bub.tools.progressive import ProgressiveToolView
from bub.tools.view import render_tool_compact_block, render_tool_expanded_block

HINT_RE = re.compile(r"\$([A-Za-z0-9_.-]+)")
TOOL_CONTINUE_PROMPT = "Continue the task."

# Type for live output callback: (event_type, data)
LiveCallback = Callable[[str, dict[str, Any]], None]


@dataclass(frozen=True)
class ModelTurnResult:
    """Result of one model turn loop."""

    visible_text: str
    exit_requested: bool
    steps: int
    error: str | None = None
    command_followups: int = 0


@dataclass
class _PromptState:
    prompt: str
    step: int = 0
    followups: int = 0
    visible_parts: list[str] = field(default_factory=list)
    error: str | None = None
    exit_requested: bool = False


class ModelRunner:
    """Runs assistant loop over tape with command-aware follow-up handling."""

    DEFAULT_HEADERS: ClassVar[dict[str, str]] = {"HTTP-Referer": "https://bub.build/", "X-Title": "Bub"}

    def __init__(
        self,
        *,
        tape: TapeService,
        router: InputRouter,
        tool_view: ProgressiveToolView,
        tools: list[Tool],
        list_skills: Callable[[], list[SkillMetadata]],
        model: str,
        max_steps: int,
        max_tokens: int,
        model_timeout_seconds: int | None,
        base_system_prompt: str,
        get_workspace_system_prompt: Callable[[], str],
    ) -> None:
        self._tape = tape
        self._router = router
        self._tool_view = tool_view
        self._tools = tools
        self._list_skills = list_skills
        self._model = model
        self._max_steps = max_steps
        self._max_tokens = max_tokens
        self._model_timeout_seconds = model_timeout_seconds
        self._base_system_prompt = base_system_prompt.strip()
        self._get_workspace_system_prompt = get_workspace_system_prompt
        self._expanded_skills: set[str] = set()
        self._message_queue: asyncio.Queue[str] = asyncio.Queue()
        self._live_callback: LiveCallback | None = None
        self._stop_requested = False
        self._paused = False
        self._step_mode = False
        self._pause_event: asyncio.Event = asyncio.Event()
        self._pause_event.set()  # Start in running state

    def reset_context(self) -> None:
        """Clear volatile model-side context caches within one session."""
        self._expanded_skills.clear()

    def inject_message(self, text: str) -> None:
        """Queue a user message to be injected into the next loop step."""
        self._message_queue.put_nowait(text)

    def request_stop(self) -> None:
        """Request graceful stop after the current step finishes."""
        self._stop_requested = True

    def request_pause(self) -> None:
        """Pause the agent loop after the current step finishes."""
        self._paused = True
        self._pause_event.clear()

    def request_resume(self) -> None:
        """Resume a paused agent loop."""
        self._paused = False
        self._step_mode = False
        self._pause_event.set()

    def request_step(self) -> None:
        """Execute one step then pause again."""
        self._step_mode = True
        self._paused = False
        self._pause_event.set()

    def set_live_callback(self, callback: LiveCallback | None) -> None:
        """Set a callback for live output events (sub-agent progress, etc.)."""
        self._live_callback = callback
        # Propagate to the tool registry so tool.start/end events flow through.
        self._router._registry.set_live_callback(callback)

    def _emit_live(self, event: str, data: dict[str, Any] | None = None) -> None:
        if self._live_callback:
            self._live_callback(event, data or {})

    async def run(self, prompt: str) -> ModelTurnResult:
        tracer = current_tracer()
        state = _PromptState(prompt=prompt)
        self._stop_requested = False
        self._activate_hints(prompt)

        while state.step < self._max_steps and not state.exit_requested and not self._stop_requested:
            # Wait if paused (non-blocking when running).
            await self._pause_event.wait()

            state.step += 1

            # Drain queued user messages and prepend to prompt.
            injected = self._drain_message_queue()
            if injected:
                inject_block = "\n".join(f"[user interjection]: {m}" for m in injected)
                state.prompt = f"{inject_block}\n\n{state.prompt}"
                self._emit_live("user.injected", {"messages": injected, "step": state.step})

            logger.info("model.runner.step step={} model={}", state.step, self._model)
            self._emit_live("step.start", {"step": state.step, "model": self._model})
            await self._tape.append_event(
                "loop.step.start",
                {
                    "step": state.step,
                    "model": self._model,
                },
            )
            self._emit_live("think.start", {"step": state.step, "model": self._model})
            with tracer.span(f"loop.step.{state.step}", metadata={"model": self._model, "step": state.step}):
                response = await self._chat(state.prompt)
            self._emit_live(
                "think.end",
                {
                    "step": state.step,
                    "has_tool_calls": response.followup_prompt is not None,
                },
            )
            if response.error is not None:
                state.error = response.error
                await self._tape.append_event(
                    "loop.step.error",
                    {
                        "step": state.step,
                        "error": response.error,
                    },
                )
                break

            if response.followup_prompt:
                await self._tape.append_event(
                    "loop.step.finish",
                    {
                        "step": state.step,
                        "visible_text": False,
                        "followup": True,
                        "exit_requested": False,
                    },
                )
                state.prompt = response.followup_prompt
                state.followups += 1
                continue

            assistant_text = response.text
            if not assistant_text.strip():
                await self._tape.append_event("loop.step.empty", {"step": state.step})
                break

            self._activate_hints(assistant_text)
            route = await self._router.route_assistant(assistant_text)
            await self._consume_route(state, route)
            if not route.next_prompt:
                break
            state.prompt = route.next_prompt
            state.followups += 1

            # In single-step mode, pause after completing the step.
            if self._step_mode:
                self._paused = True
                self._pause_event.clear()
                self._emit_live("step.paused", {"step": state.step, "reason": "single-step mode"})

        if state.step >= self._max_steps and not state.error:
            state.error = f"max_steps_reached={self._max_steps}"
            await self._tape.append_event("loop.max_steps", {"max_steps": self._max_steps})

        return ModelTurnResult(
            visible_text="\n\n".join(part for part in state.visible_parts if part).strip(),
            exit_requested=state.exit_requested,
            steps=state.step,
            error=state.error,
            command_followups=state.followups,
        )

    async def _consume_route(self, state: _PromptState, route: AssistantRouteResult) -> None:
        if route.visible_text:
            state.visible_parts.append(route.visible_text)
        if route.exit_requested:
            state.exit_requested = True
        await self._tape.append_event(
            "loop.step.finish",
            {
                "step": state.step,
                "visible_text": bool(route.visible_text),
                "followup": bool(route.next_prompt),
                "exit_requested": route.exit_requested,
            },
        )

    async def _chat(self, prompt: str) -> _ChatResult:
        tracer = current_tracer()
        builder = self._build_system_prompt()
        system_prompt = builder.render()
        gen_span = tracer.generation(
            "llm.chat",
            model=self._model,
            input_messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ],
            metadata={"max_tokens": self._max_tokens, "prompt_blocks": builder.debug_info()},
        )
        try:
            async with asyncio.timeout(self._model_timeout_seconds):
                provider, _, _ = self._model.partition(":")
                if provider.casefold() == "vertexai":
                    output = await self._tape.tape.run_tools_async(
                        prompt=prompt,
                        system_prompt=system_prompt,
                        max_tokens=self._max_tokens,
                        tools=self._tools,
                        http_options={"headers": self.DEFAULT_HEADERS},
                    )
                else:
                    output = await self._tape.tape.run_tools_async(
                        prompt=prompt,
                        system_prompt=system_prompt,
                        max_tokens=self._max_tokens,
                        tools=self._tools,
                        extra_headers=self.DEFAULT_HEADERS,
                    )
                result = _ChatResult.from_tool_auto(output)
                usage = getattr(output, "usage", None)
                usage_dict = None
                if usage and isinstance(usage, dict):
                    usage_dict = usage

                # Build detailed output including tool calls and results.
                gen_output: dict[str, object] = {}
                if result.text:
                    gen_output["text"] = result.text[:4096]
                if result.error:
                    gen_output["error"] = result.error
                if output.tool_calls:
                    gen_output["tool_calls"] = output.tool_calls
                if output.tool_results:
                    gen_output["tool_results"] = [str(r)[:4096] if r is not None else None for r in output.tool_results]

                gen_span.end(
                    output=gen_output or result.text or result.error,
                    usage=usage_dict,
                    level="ERROR" if result.error else "DEFAULT",
                )
                return result
        except TimeoutError:
            gen_span.end(output="model_timeout", level="ERROR")
            return _ChatResult(
                text="",
                error=f"model_timeout: no response within {self._model_timeout_seconds}s",
            )
        except Exception as exc:
            logger.exception("model.call.error")
            gen_span.end(output=str(exc), level="ERROR")
            return _ChatResult(text="", error=f"model_call_error: {exc!s}")

    def _build_system_prompt(self) -> PromptBuilder:
        builder = PromptBuilder()
        builder.add("base", self._base_system_prompt or "", priority=10)
        if workspace_prompt := self._get_workspace_system_prompt():
            builder.add("workspace_agents", workspace_prompt, priority=20)
        builder.add("tools", render_tool_compact_block(self._tool_view), priority=30)
        compact_skills = render_compact_skills(self._list_skills(), self._expanded_skills)
        if compact_skills:
            builder.add("skills", compact_skills, priority=40, mutable=True)
        builder.add("runtime_contract", _runtime_contract(), priority=90)
        expanded = render_tool_expanded_block(self._tool_view)
        if expanded:
            builder.add("tool_details", expanded, priority=95, mutable=True)
        return builder

    def _render_system_prompt(self) -> str:
        return self._build_system_prompt().render()

    def _drain_message_queue(self) -> list[str]:
        """Drain all pending injected messages from the queue."""
        messages: list[str] = []
        while not self._message_queue.empty():
            try:
                messages.append(self._message_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return messages

    def _activate_hints(self, text: str) -> None:
        skill_index = self._build_skill_index()
        for match in HINT_RE.finditer(text):
            hint = match.group(1)
            self._tool_view.note_hint(hint)

            skill = skill_index.get(hint.casefold())
            if skill is None:
                continue
            self._expanded_skills.add(skill.name)

    def _build_skill_index(self) -> dict[str, SkillMetadata]:
        return {skill.name.casefold(): skill for skill in self._list_skills()}


@dataclass(frozen=True)
class _ChatResult:
    text: str
    error: str | None = None
    followup_prompt: str | None = None

    @classmethod
    def from_tool_auto(cls, output: ToolAutoResult) -> _ChatResult:
        if output.kind == "text":
            return cls(text=output.text or "")
        if output.kind == "tools":
            return cls(text="", followup_prompt=TOOL_CONTINUE_PROMPT)

        if output.tool_calls or output.tool_results:
            return cls(text="", followup_prompt=TOOL_CONTINUE_PROMPT)

        if output.error is None:
            return cls(text="", error="tool_auto_error: unknown")
        return cls(text="", error=f"{output.error.kind.value}: {output.error.message}")


def _runtime_contract() -> str:
    return textwrap.dedent("""\
        <runtime_contract>
        1. Use tool calls for all actions (file ops, shell, web, tape, skills).
        2. Do not emit comma-prefixed commands in normal flow; use tool calls instead.
        3. If a compatibility fallback is required, runtime can still parse comma commands.
        4. Never emit '<command ...>' blocks yourself; those are runtime-generated.
        5. When enough evidence is collected, return plain natural language answer.
        </runtime_contract>
        <tool_discovery>
        The <tool_view> above lists all available tools with short descriptions.
        Some tools show full schema in <tool_details> (always-expanded core tools).
        For OTHER tools (especially MCP tools prefixed with mcp__), you must discover their arguments BEFORE calling them:
        - Write '$tool_name' in your response to expand its full schema, guidance, and examples.
        - Example: writing '$mcp__feishu__search_doc' will reveal its parameters in the next turn.
        - You can also call tool.describe with the tool name for immediate details.
        - NEVER guess arguments for unfamiliar tools — always discover first, then call.
        - You may expand multiple tools at once: '$web_search $schedule_add'.
        </tool_discovery>
        <tool_description>
        IMPORTANT: Every tool call MUST include a non-empty 'description' parameter.
        This is the ONLY text shown to the user for each action — without it, the user sees nothing.
        The description must be a brief, human-readable explanation of what this specific call does and why.
        Examples:
        - bash: description="Run pytest to verify the auth module changes"
        - fs.read: description="Read the config file to check database settings"
        - fs.edit: description="Fix the off-by-one error in pagination logic"
        - fs.grep: description="Find all usages of deprecated API endpoint"
        - agent: description="Search codebase for authentication patterns"
        - web.search: description="Find documentation for FastAPI middleware"
        Rules:
        - NEVER leave description empty — always explain the intent
        - Keep it concise (under 80 chars), action-oriented, specific to the current task
        - Do NOT write generic descriptions like "run command" or "read file" — explain WHY
        </tool_description>
        <tool_preference>
        Always prefer dedicated tools over bash for file operations. This is CRITICAL:
        - Read files: use fs.read (NOT bash cat/head/tail/sed)
        - Write files: use fs.write (NOT bash echo/cat heredoc)
        - Edit files: use fs.edit (NOT bash sed/awk)
        - Search file contents: use fs.grep (NOT bash grep/rg)
        - Find files by name: use fs.glob (NOT bash find/ls)
        Reserve bash for: git, make, docker, package managers, running tests, and commands with no dedicated tool.
        When reading multiple files or searching in parallel, issue multiple tool calls simultaneously.
        </tool_preference>
        <context_contract>
        You work within a finite context window. Manage it proactively:
        1. If you have made >30 tool calls since the last anchor, use tape.handoff to checkpoint.
        2. If a model call fails with context length error, immediately use tape.handoff.
        3. Write SELF-CONTAINED handoff summaries: include what was done, key decisions, files modified, and clear next steps.
        4. After a handoff, re-read key files rather than relying on memory of their contents.
        5. Use tape.info to check current context size when uncertain.
        </context_contract>
        <response_instruct>
        You MUST send message to the corresponding channel before finish when you want to respond.
        Route your response to the same channel the message came from.
        There is a skill named `{channel}` for each channel that you need to figure out how to send a response to that channel.
        ## Before finishing ANY response to a channel message:
        1. Identify the source channel from the user message metadata
        2. Prepare your response text
        3. Call the corresponding channel skill to deliver the message
        4. ONLY THEN end your turn
        </response_instruct>""")

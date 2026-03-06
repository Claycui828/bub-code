"""Agent delegation tool with predefined agent types."""

from __future__ import annotations

import asyncio
import json
import textwrap
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from loguru import logger
from pydantic import BaseModel, Field
from republic import ToolContext

from bub.tools.registry import ToolGuidance, ToolRegistry

if TYPE_CHECKING:
    from bub.app.runtime import AppRuntime


# ---------------------------------------------------------------------------
# Agent type definitions (aligned with Claude Code's sub-agent architecture)
# ---------------------------------------------------------------------------

# Tools that are read-only safe for explore/plan agents.
_READ_ONLY_TOOLS = frozenset({
    "fs.read", "fs.grep", "fs.glob", "bash",
    "tape.info", "tape.search", "tape.anchors",
    "tools", "tool.describe", "help",
})

EXPLORE_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a codebase search specialist — a fast, read-only agent for finding files, \
    searching code, and analyzing project structure.

    === CRITICAL: READ-ONLY MODE — NO FILE MODIFICATIONS ===

    You are STRICTLY PROHIBITED from:
    - Creating new files (no fs.write, no file creation of any kind)
    - Modifying existing files (no fs.edit)
    - Running commands that change state (no git add, git commit, npm install, pip install, mkdir, rm, mv, cp)
    - Using bash redirect operators (>, >>) or heredocs to write to files

    Your strengths:
    - Rapidly finding files using glob patterns (fs.glob)
    - Searching code with regex patterns (fs.grep)
    - Reading and analyzing file contents (fs.read)
    - Running read-only bash commands (git status, git log, git diff, ls, find, cat, head, tail)

    Guidelines:
    - Use fs.glob for broad file pattern matching
    - Use fs.grep for searching file contents with regex
    - Use fs.read when you know the specific file path
    - Use bash ONLY for read-only operations
    - Issue multiple parallel tool calls wherever possible for speed
    - Return file paths as absolute paths in your findings
    - Communicate findings directly as text — do NOT create files

    You are meant to be FAST. Make efficient use of tools and parallelize searches.""")

PLAN_SYSTEM_PROMPT = textwrap.dedent("""\
    You are a software architect and planning specialist. Your role is to explore \
    codebases and design implementation strategies — operating exclusively in read-only mode.

    === CRITICAL: READ-ONLY MODE — NO FILE MODIFICATIONS ===

    You are STRICTLY PROHIBITED from:
    - Creating new files (no fs.write)
    - Modifying existing files (no fs.edit)
    - Running state-changing commands (no git add, git commit, npm install, pip install, mkdir, rm)

    Process:
    1. Understand Requirements — clarify the goal and constraints
    2. Explore Thoroughly — read files, find patterns with fs.glob/fs.grep/fs.read, \
    understand architecture
    3. Design Solution — create approaches considering trade-offs and existing patterns
    4. Detail the Plan — step-by-step strategy with dependencies and challenges

    Output format:
    End your response with a "Critical Files for Implementation" section listing 3-5 key files \
    with brief reasons (e.g., "Core logic to modify", "Interfaces to implement").

    You can ONLY explore and plan. You CANNOT and MUST NOT write, edit, or modify any files.""")


@dataclass(frozen=True)
class AgentTypeConfig:
    """Configuration for a predefined agent type."""

    description: str
    system_prompt: str | None = None  # None = inherit parent's
    allowed_tools: frozenset[str] | None = None  # None = all tools (minus agent)
    read_only: bool = False


BUILTIN_AGENT_TYPES: dict[str, AgentTypeConfig] = {
    "explore": AgentTypeConfig(
        description="Fast read-only codebase search and analysis. Use for file discovery, "
        "code search, and understanding project structure without making changes.",
        system_prompt=EXPLORE_SYSTEM_PROMPT,
        allowed_tools=_READ_ONLY_TOOLS,
        read_only=True,
    ),
    "plan": AgentTypeConfig(
        description="Architecture research and implementation planning. Explores the codebase "
        "and designs step-by-step implementation strategies in read-only mode.",
        system_prompt=PLAN_SYSTEM_PROMPT,
        allowed_tools=_READ_ONLY_TOOLS,
        read_only=True,
    ),
    "general": AgentTypeConfig(
        description="Complex multi-step tasks requiring both exploration and modification. "
        "Has access to all tools. Use for tasks that need code changes, running tests, etc.",
        system_prompt=None,
        allowed_tools=None,
        read_only=False,
    ),
}

# Tools that sub-agents must NEVER have (prevents nesting).
_DENIED_SUBAGENT_TOOLS = frozenset({"agent", "agent.status", "agent.list"})


def _resolve_agent_config(
    params: AgentInput,
) -> tuple[str | None, str | None, set[str] | None]:
    """Resolve (model, system_prompt, allowed_tools) from agent_type + explicit overrides.

    Explicit params always win over agent_type defaults.
    The 'agent' tool is always removed from sub-agent tool sets.
    """
    agent_type_cfg: AgentTypeConfig | None = None
    if params.agent_type:
        agent_type_cfg = BUILTIN_AGENT_TYPES.get(params.agent_type)
        if agent_type_cfg is None:
            available = ", ".join(sorted(BUILTIN_AGENT_TYPES))
            raise RuntimeError(
                f"unknown agent_type: {params.agent_type!r}. Available: {available}"
            )

    # Model: explicit > agent_type default (none = inherit)
    model = params.model

    # System prompt: explicit > agent_type default
    system_prompt = params.system_prompt
    if system_prompt is None and agent_type_cfg and agent_type_cfg.system_prompt:
        system_prompt = agent_type_cfg.system_prompt

    # Tools: explicit > agent_type default. Always remove agent tools.
    if params.allowed_tools is not None:
        tool_set = set(params.allowed_tools) - _DENIED_SUBAGENT_TOOLS
    elif agent_type_cfg and agent_type_cfg.allowed_tools is not None:
        tool_set = set(agent_type_cfg.allowed_tools) - _DENIED_SUBAGENT_TOOLS
    else:
        # None means "all tools" — nesting prevention happens at registration time.
        tool_set = None

    return model, system_prompt, tool_set


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class AgentInput(BaseModel):
    agent_type: str | None = Field(
        default=None,
        description=(
            "Predefined agent type that sets tools, system prompt, and behavior. "
            "Available types: 'explore' (fast read-only search), 'plan' (architecture planning, read-only), "
            "'general' (full access, complex tasks). "
            "Omit to use general-purpose agent. Explicit model/system_prompt/allowed_tools override type defaults."
        ),
    )
    prompt: str = Field(
        ...,
        description=(
            "Complete task description for the sub-agent. Must be self-contained — include ALL necessary "
            "context, file paths, requirements, and constraints. The sub-agent has NO access to the parent "
            "conversation history."
        ),
    )
    description: str = Field(
        default="",
        description="Short label for this delegation (3-5 words), shown in agent.list output",
    )
    model: str | None = Field(
        default=None,
        description="Override model. Defaults to parent's model (or agent_type default if set).",
    )
    system_prompt: str | None = Field(
        default=None,
        description="Override system prompt. Defaults to agent_type's prompt or parent's prompt.",
    )
    allowed_tools: list[str] | None = Field(
        default=None,
        description=(
            "Override tool access. E.g. ['bash', 'fs.read', 'fs.write']. "
            "Defaults to agent_type's tool set or all tools."
        ),
    )
    run_in_background: bool = Field(
        default=False,
        description="If true, return immediately with an agent_id. Check progress with agent.status.",
    )
    resume: str | None = Field(
        default=None,
        description="Agent ID from a previous invocation to resume. The agent keeps its full conversation history.",
    )


@dataclass
class AgentRecord:
    """Tracks one sub-agent invocation."""

    agent_id: str
    session_id: str
    description: str
    agent_type: str = "general"
    status: str = "running"  # running | completed | error
    result: str = ""
    error: str | None = None
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None


class AgentManager:
    """Manages sub-agent lifecycle and background tasks."""

    def __init__(self) -> None:
        self._records: dict[str, AgentRecord] = {}
        self._background_tasks: dict[str, asyncio.Task[None]] = {}
        self._counter = 0

    def next_id(self) -> str:
        self._counter += 1
        return f"agent-{self._counter}"

    def get(self, agent_id: str) -> AgentRecord | None:
        return self._records.get(agent_id)

    def register(self, record: AgentRecord) -> None:
        self._records[record.agent_id] = record

    def set_background_task(self, agent_id: str, task: asyncio.Task[None]) -> None:
        self._background_tasks[agent_id] = task

    def list_agents(self) -> list[AgentRecord]:
        return sorted(self._records.values(), key=lambda r: r.started_at, reverse=True)


# Module-level singleton; reset-friendly for tests.
_manager = AgentManager()


def get_agent_manager() -> AgentManager:
    return _manager


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register_agent_tools(
    registry: ToolRegistry,
    *,
    runtime: AppRuntime,
) -> None:
    """Register agent delegation tools."""

    manager = get_agent_manager()
    register = registry.register

    # Build agent type description for the tool guidance.
    type_descriptions = "\n".join(
        f"  - {name}: {cfg.description}" for name, cfg in sorted(BUILTIN_AGENT_TYPES.items())
    )

    @register(
        name="agent",
        short_description="Delegate a task to an isolated sub-agent and return its result",
        model=AgentInput,
        context=True,
        always_expand=True,
        guidance=ToolGuidance(
            when_to_use=(
                "Complex subtasks that benefit from isolation, parallel work via run_in_background, "
                "or tasks requiring focused tool access. Use 'explore' type for fast codebase search, "
                "'plan' type for architecture design, 'general' for tasks needing code changes."
            ),
            when_not_to=(
                "Simple queries or tasks that need the parent conversation context. "
                "Sub-agents start with a clean tape and cannot see the parent's history."
            ),
            examples=(
                "agent agent_type='explore' prompt='Find all files that import FastAPI' | "
                "agent agent_type='plan' prompt='Design the auth module for this project' | "
                "agent prompt='Refactor the database layer' run_in_background=true"
            ),
            constraints=(
                f"Available agent types:\n{type_descriptions}\n"
                "Sub-agents CANNOT spawn other sub-agents (single-level only). "
                "Provide all necessary context in the prompt — the sub-agent has no access to parent history."
            ),
        ),
    )
    async def agent_delegate(params: AgentInput, context: ToolContext) -> str:
        """Spawn an isolated sub-agent to handle a task autonomously.

        The sub-agent runs in a completely isolated session with a fresh conversation tape.
        It has NO access to the parent's conversation history.

        Agent types:
        - explore: Fast read-only search. Uses fs.glob/fs.grep/fs.read/bash(read-only).
          Best for: finding files, searching code, understanding project structure.
        - plan: Architecture research in read-only mode. Explores codebase and returns
          step-by-step implementation plans with critical file lists.
        - general (default): Full tool access for complex tasks. Can read, write, edit
          files, run commands, and perform multi-step operations.

        Sub-agents CANNOT spawn other sub-agents (single-level architecture).
        The result includes a structured action summary: tools called, files modified, commands run.
        """
        parent_session_id = context.state.get("session_id", "")

        # Emit live events to parent session for CLI visibility.
        parent_session = runtime._sessions.get(parent_session_id)
        emit = parent_session.model_runner._emit_live if parent_session else lambda *a, **k: None

        # Resolve agent_type + explicit overrides into final config.
        model, system_prompt, tool_set = _resolve_agent_config(params)
        effective_type = params.agent_type or "general"

        # --- Resume existing agent ---
        if params.resume:
            return await _handle_resume(
                runtime, manager, params, model, system_prompt, tool_set,
            )

        # --- New agent ---
        agent_id = manager.next_id()
        sub_session_id = f"{parent_session_id}:sub:{agent_id}"
        record = AgentRecord(
            agent_id=agent_id,
            session_id=sub_session_id,
            description=params.description or f"{effective_type} sub-agent",
            agent_type=effective_type,
        )
        manager.register(record)

        if params.run_in_background:
            task = asyncio.create_task(
                _run_background(runtime, manager, record, params.prompt, model, system_prompt, tool_set)
            )
            manager.set_background_task(agent_id, task)
            logger.info(
                "agent.background.start agent_id={} type={} description={}",
                agent_id, effective_type, params.description,
            )
            return (
                f"agent started in background: {agent_id} (type={effective_type})\n"
                f"Use agent.status with agent_id={agent_id} to check progress."
            )

        # --- Foreground execution ---
        emit("sub_agent.start", {
            "agent_id": agent_id,
            "agent_type": effective_type,
            "description": record.description,
            "prompt": params.prompt,
            "model": model or "",
        })

        result = await runtime.handle_input(
            sub_session_id,
            params.prompt,
            model=model,
            system_prompt=system_prompt,
            allowed_tools=tool_set,
        )
        record.status = "completed" if not result.error else "error"
        record.result = result.assistant_output.strip() if result.assistant_output else ""
        record.error = result.error
        record.finished_at = time.time()

        # Capture tape name and action summary before removing session.
        tape_name = _get_session_tape_name(runtime, sub_session_id)
        action_summary = _extract_action_summary(runtime, sub_session_id)

        emit("sub_agent.end", {
            "agent_id": agent_id,
            "agent_type": effective_type,
            "status": record.status,
            "result": record.result,
            "error": record.error,
        })

        # Clean up sub-session in memory; tape file stays on disk for resume.
        runtime.remove_session(sub_session_id)

        return _format_result(record, tape_name=tape_name, action_summary=action_summary)

    # --- agent.status tool ---

    class AgentStatusInput(BaseModel):
        agent_id: str = Field(..., description="Agent ID to check (e.g. 'agent-1')")

    @register(name="agent.status", short_description="Check sub-agent status and result", model=AgentStatusInput, always_expand=True)
    def agent_status(params: AgentStatusInput) -> str:
        """Check the status and result of a sub-agent by its ID."""
        record = manager.get(params.agent_id)
        if record is None:
            raise RuntimeError(f"agent not found: {params.agent_id}")
        lines = [
            f"agent_id: {record.agent_id}",
            f"type: {record.agent_type}",
            f"description: {record.description}",
            f"status: {record.status}",
        ]
        if record.finished_at:
            elapsed = record.finished_at - record.started_at
            lines.append(f"elapsed: {elapsed:.1f}s")
        if record.error:
            lines.append(f"error: {record.error}")
        if record.result:
            lines.append(f"result:\n{record.result}")
        elif record.status == "completed":
            lines.append("result: (no output)")
        return "\n".join(lines)

    # --- agent.list tool ---

    class AgentListInput(BaseModel):
        pass

    @register(name="agent.list", short_description="List all sub-agents with status", model=AgentListInput, always_expand=True)
    def agent_list(_params: AgentListInput) -> str:
        """List all sub-agent invocations with their type and status."""
        records = manager.list_agents()
        if not records:
            return "(no agents)"
        rows: list[str] = []
        for rec in records:
            elapsed = ""
            if rec.finished_at:
                elapsed = f" ({rec.finished_at - rec.started_at:.1f}s)"
            rows.append(f"{rec.agent_id} [{rec.agent_type}:{rec.status}]{elapsed} {rec.description}")
        return "\n".join(rows)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

async def _handle_resume(
    runtime: AppRuntime,
    manager: AgentManager,
    params: AgentInput,
    model: str | None,
    system_prompt: str | None,
    tool_set: set[str] | None,
) -> str:
    """Handle resume of an existing agent."""
    record = manager.get(params.resume)  # type: ignore[arg-type]
    if record is None:
        raise RuntimeError(f"agent not found: {params.resume}")
    if record.status == "running":
        raise RuntimeError(f"agent {params.resume} is still running")

    result = await runtime.handle_input(
        record.session_id,
        params.prompt,
        model=model,
        system_prompt=system_prompt,
        allowed_tools=tool_set,
    )
    record.status = "completed" if not result.error else "error"
    record.result = result.assistant_output.strip() if result.assistant_output else ""
    record.error = result.error
    record.finished_at = time.time()
    tape_name = _get_session_tape_name(runtime, record.session_id)
    action_summary = _extract_action_summary(runtime, record.session_id)
    return _format_result(record, tape_name=tape_name, action_summary=action_summary)


def _get_session_tape_name(runtime: AppRuntime, session_id: str) -> str | None:
    """Get the tape name for a session, if it exists."""
    session = runtime._sessions.get(session_id)
    if session is None:
        return None
    return session.tape._tape.name


async def _run_background(
    runtime: AppRuntime,
    manager: AgentManager,
    record: AgentRecord,
    prompt: str,
    model: str | None,
    system_prompt: str | None,
    allowed_tools: set[str] | None,
) -> None:
    """Execute a sub-agent in the background and update its record on completion."""
    try:
        result = await runtime.handle_input(
            record.session_id,
            prompt,
            model=model,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
        )
        record.status = "completed" if not result.error else "error"
        record.result = result.assistant_output.strip() if result.assistant_output else ""
        record.error = result.error
    except Exception as exc:
        logger.exception("agent.background.error agent_id={}", record.agent_id)
        record.status = "error"
        record.error = str(exc)
    finally:
        record.finished_at = time.time()
        tape_name = _get_session_tape_name(runtime, record.session_id)
        if tape_name:
            record.result = f"tape: {tape_name}\n{record.result}" if record.result else f"tape: {tape_name}"
        logger.info(
            "agent.background.done agent_id={} status={} elapsed={:.1f}s",
            record.agent_id,
            record.status,
            record.finished_at - record.started_at,
        )


def _parse_tool_call(call: object) -> tuple[str, dict[str, object]] | None:
    """Extract (name, args) from a single tool call dict."""
    if not isinstance(call, dict):
        return None
    func = call.get("function")
    if not isinstance(func, dict):
        return None
    name = func.get("name", "")
    args_raw = func.get("arguments", "")
    try:
        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
    except (json.JSONDecodeError, TypeError):
        args = {}
    return (name, args) if isinstance(args, dict) else (name, {})


def _collect_tool_calls(entries: list[object]) -> list[tuple[str, dict[str, object]]]:
    """Collect parsed (name, args) pairs from tape entries."""
    from republic.tape import TapeEntry as _TE

    results: list[tuple[str, dict[str, object]]] = []
    for entry in entries:
        if not isinstance(entry, _TE) or entry.kind != "tool_call":
            continue
        calls = entry.payload.get("calls")
        if not isinstance(calls, list):
            continue
        for raw_call in calls:
            parsed = _parse_tool_call(raw_call)
            if parsed is not None:
                results.append(parsed)
    return results


def _extract_action_summary(runtime: AppRuntime, session_id: str) -> str | None:
    """Extract a structured summary of actions from a sub-agent's tape."""
    session = runtime._sessions.get(session_id)
    if session is None:
        return None
    entries = session.tape._store.read(session.tape._tape.name)
    if not entries:
        return None

    tool_counts: dict[str, int] = {}
    files_modified: list[str] = []
    commands_run: list[str] = []

    for name, args in _collect_tool_calls(entries):
        tool_counts[name] = tool_counts.get(name, 0) + 1
        if name == "bash" and "cmd" in args:
            commands_run.append(str(args["cmd"])[:80])
        elif name in ("fs_write", "fs_edit") and "path" in args:
            path = str(args["path"])
            if path not in files_modified:
                files_modified.append(path)

    if not tool_counts:
        return None

    lines = [f"tools: {', '.join(f'{n}({c})' for n, c in sorted(tool_counts.items()))}"]
    if files_modified:
        lines.append(f"files: {', '.join(files_modified)}")
    if commands_run:
        lines.append(f"commands: {'; '.join(commands_run[:5])}")
    return "\n".join(lines)


def _format_result(record: AgentRecord, *, tape_name: str | None = None, action_summary: str | None = None) -> str:
    """Format agent result for tool output."""
    parts: list[str] = [f"agent_id: {record.agent_id}", f"type: {record.agent_type}"]
    if tape_name:
        parts.append(f"tape: {tape_name}")
    if action_summary:
        parts.append(action_summary)
    if record.result:
        parts.append(record.result)
    if record.error:
        parts.append(f"(agent error: {record.error})")
    if not record.result and not record.error:
        parts.append("(agent returned no output)")
    return "\n".join(parts)

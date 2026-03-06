import asyncio
import inspect
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
from republic import ToolContext

from bub.tools.agent import (
    BUILTIN_AGENT_TYPES,
    _DENIED_SUBAGENT_TOOLS,
    _READ_ONLY_TOOLS,
    get_agent_manager,
    register_agent_tools,
)
from bub.tools.registry import ToolRegistry


@dataclass
class _FakeLoopResult:
    assistant_output: str = ""
    error: str | None = None


class _FakeRuntime:
    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}
        self.handle_input = AsyncMock(return_value=_FakeLoopResult(assistant_output="sub-agent done"))

    def remove_session(self, session_id: str, *, keep_tape: bool = True) -> None:
        self._sessions.pop(session_id, None)


def _build_registry(runtime: _FakeRuntime) -> ToolRegistry:
    registry = ToolRegistry()
    register_agent_tools(registry, runtime=runtime)  # type: ignore[arg-type]
    return registry


def _ctx(session_id: str = "sess1") -> ToolContext:
    return ToolContext("test", "test", state={"session_id": session_id})


async def _run(registry: ToolRegistry, name: str, context: ToolContext | None = None, **kwargs: object) -> str:
    descriptor = registry.get(name)
    assert descriptor is not None, f"tool {name} not found"
    if descriptor.tool.context:
        result = descriptor.tool.run(context=context, **kwargs)
    else:
        result = descriptor.tool.run(**kwargs)
    if inspect.isawaitable(result):
        result = await result
    return result


@pytest.fixture(autouse=True)
def _reset_agent_manager() -> None:
    """Reset the global agent manager between tests."""
    mgr = get_agent_manager()
    mgr._records.clear()
    mgr._background_tasks.clear()
    mgr._counter = 0


class TestAgentDelegation:
    @pytest.mark.asyncio
    async def test_foreground_returns_output(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        result = await _run(registry, "agent", context=_ctx(), prompt="find TODOs", description="find TODOs")

        assert "sub-agent done" in result
        assert "agent_id: agent-1" in result
        runtime.handle_input.assert_called_once()
        args = runtime.handle_input.call_args
        assert "sess1:sub:agent-1" in args[0][0]
        assert args[0][1] == "find TODOs"

    @pytest.mark.asyncio
    async def test_foreground_with_error(self) -> None:
        runtime = _FakeRuntime()
        runtime.handle_input = AsyncMock(return_value=_FakeLoopResult(assistant_output="", error="timeout"))
        registry = _build_registry(runtime)

        result = await _run(registry, "agent", context=_ctx(), prompt="slow task", description="slow")

        assert "agent error: timeout" in result

    @pytest.mark.asyncio
    async def test_foreground_partial_output_with_error(self) -> None:
        runtime = _FakeRuntime()
        runtime.handle_input = AsyncMock(
            return_value=_FakeLoopResult(assistant_output="partial", error="max_steps")
        )
        registry = _build_registry(runtime)

        result = await _run(registry, "agent", context=_ctx(), prompt="big task", description="big")

        assert "partial" in result
        assert "max_steps" in result

    @pytest.mark.asyncio
    async def test_foreground_empty_output(self) -> None:
        runtime = _FakeRuntime()
        runtime.handle_input = AsyncMock(return_value=_FakeLoopResult(assistant_output=""))
        registry = _build_registry(runtime)

        result = await _run(registry, "agent", context=_ctx(), prompt="quiet", description="quiet")

        assert "no output" in result


class TestAgentTypes:
    @pytest.mark.asyncio
    async def test_explore_type_sets_read_only_tools(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="find all Python files", description="search", agent_type="explore",
        )

        kwargs = runtime.handle_input.call_args.kwargs
        allowed = kwargs["allowed_tools"]
        assert allowed is not None
        assert "fs.read" in allowed
        assert "fs.grep" in allowed
        assert "fs.glob" in allowed
        assert "bash" in allowed
        # Write/edit tools must NOT be present.
        assert "fs.write" not in allowed
        assert "fs.edit" not in allowed
        # Agent tool must NOT be present (nesting prevention).
        assert "agent" not in allowed

    @pytest.mark.asyncio
    async def test_explore_type_uses_dedicated_system_prompt(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="find auth files", description="search", agent_type="explore",
        )

        kwargs = runtime.handle_input.call_args.kwargs
        assert kwargs["system_prompt"] is not None
        assert "READ-ONLY" in kwargs["system_prompt"]
        assert "search specialist" in kwargs["system_prompt"]

    @pytest.mark.asyncio
    async def test_plan_type_uses_plan_prompt(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="design auth module", description="plan", agent_type="plan",
        )

        kwargs = runtime.handle_input.call_args.kwargs
        assert kwargs["system_prompt"] is not None
        assert "software architect" in kwargs["system_prompt"]
        assert "Critical Files" in kwargs["system_prompt"]

    @pytest.mark.asyncio
    async def test_general_type_gets_all_tools(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="refactor database layer", description="refactor", agent_type="general",
        )

        kwargs = runtime.handle_input.call_args.kwargs
        # general type: tool_set is None (inherits all) but agent tools removed
        # at registration level; handle_input receives None for allowed_tools.
        assert kwargs.get("allowed_tools") is None

    @pytest.mark.asyncio
    async def test_unknown_type_raises_error(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        with pytest.raises(RuntimeError, match="unknown agent_type"):
            await _run(
                registry, "agent", context=_ctx(),
                prompt="task", description="test", agent_type="nonexistent",
            )

    @pytest.mark.asyncio
    async def test_explicit_tools_override_type(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="task", description="test",
            agent_type="explore",
            allowed_tools=["bash", "fs.read", "fs.write"],
        )

        kwargs = runtime.handle_input.call_args.kwargs
        allowed = kwargs["allowed_tools"]
        # Explicit tools win, but agent is still denied.
        assert "fs.write" in allowed
        assert "agent" not in allowed

    @pytest.mark.asyncio
    async def test_explicit_system_prompt_overrides_type(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="task", description="test",
            agent_type="explore",
            system_prompt="Custom prompt for this agent.",
        )

        kwargs = runtime.handle_input.call_args.kwargs
        assert kwargs["system_prompt"] == "Custom prompt for this agent."

    @pytest.mark.asyncio
    async def test_default_type_is_general(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        result = await _run(
            registry, "agent", context=_ctx(),
            prompt="some task", description="test",
        )

        assert "type: general" in result

    @pytest.mark.asyncio
    async def test_agent_type_in_status(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="search", description="search", agent_type="explore",
        )

        status = await _run(registry, "agent.status", agent_id="agent-1")
        assert "type: explore" in status

    @pytest.mark.asyncio
    async def test_agent_type_in_list(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="search", description="search", agent_type="explore",
        )
        await _run(
            registry, "agent", context=_ctx(),
            prompt="plan", description="plan", agent_type="plan",
        )

        listing = await _run(registry, "agent.list")
        assert "explore:" in listing
        assert "plan:" in listing


class TestNestingPrevention:
    def test_denied_tools_defined(self) -> None:
        assert "agent" in _DENIED_SUBAGENT_TOOLS
        assert "agent.status" in _DENIED_SUBAGENT_TOOLS
        assert "agent.list" in _DENIED_SUBAGENT_TOOLS

    @pytest.mark.asyncio
    async def test_explore_cannot_use_agent(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="search", description="search", agent_type="explore",
        )

        kwargs = runtime.handle_input.call_args.kwargs
        allowed = kwargs["allowed_tools"]
        for tool in _DENIED_SUBAGENT_TOOLS:
            assert tool not in allowed


class TestAgentModelOverride:
    @pytest.mark.asyncio
    async def test_model_passed_through(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="analyze code", description="analyze", model="openrouter:anthropic/claude-sonnet-4",
        )

        kwargs = runtime.handle_input.call_args.kwargs
        assert kwargs["model"] == "openrouter:anthropic/claude-sonnet-4"

    @pytest.mark.asyncio
    async def test_system_prompt_passed_through(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="task", description="test", system_prompt="You are a code reviewer.",
        )

        kwargs = runtime.handle_input.call_args.kwargs
        assert kwargs["system_prompt"] == "You are a code reviewer."

    @pytest.mark.asyncio
    async def test_allowed_tools_passed_through(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="task", description="test", allowed_tools=["fs.read", "bash"],
        )

        kwargs = runtime.handle_input.call_args.kwargs
        assert "fs.read" in kwargs["allowed_tools"]
        assert "bash" in kwargs["allowed_tools"]


class TestAgentBackground:
    @pytest.mark.asyncio
    async def test_background_returns_id_immediately(self) -> None:
        runtime = _FakeRuntime()
        async def slow_input(*args: Any, **kwargs: Any) -> _FakeLoopResult:
            await asyncio.sleep(0.5)
            return _FakeLoopResult(assistant_output="bg done")

        runtime.handle_input = AsyncMock(side_effect=slow_input)
        registry = _build_registry(runtime)

        result = await _run(
            registry, "agent", context=_ctx(),
            prompt="slow task", description="bg test", run_in_background=True,
        )

        assert "agent-1" in result
        assert "background" in result

        mgr = get_agent_manager()
        bg_task = mgr._background_tasks.get("agent-1")
        assert bg_task is not None
        await bg_task

        record = mgr.get("agent-1")
        assert record is not None
        assert record.status == "completed"
        assert record.result == "bg done"

    @pytest.mark.asyncio
    async def test_background_error_captured(self) -> None:
        runtime = _FakeRuntime()
        runtime.handle_input = AsyncMock(return_value=_FakeLoopResult(assistant_output="", error="crash"))
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="crash task", description="crash", run_in_background=True,
        )

        mgr = get_agent_manager()
        bg_task = mgr._background_tasks.get("agent-1")
        await bg_task

        record = mgr.get("agent-1")
        assert record.status == "error"
        assert record.error == "crash"

    @pytest.mark.asyncio
    async def test_background_with_agent_type(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        result = await _run(
            registry, "agent", context=_ctx(),
            prompt="search", description="search",
            agent_type="explore", run_in_background=True,
        )

        assert "type=explore" in result


class TestAgentResume:
    @pytest.mark.asyncio
    async def test_resume_continues_session(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        result1 = await _run(
            registry, "agent", context=_ctx(),
            prompt="start research", description="research",
        )
        assert "agent-1" in result1

        runtime.handle_input = AsyncMock(return_value=_FakeLoopResult(assistant_output="continued result"))
        result2 = await _run(
            registry, "agent", context=_ctx(),
            prompt="now summarize", description="summarize", resume="agent-1",
        )

        assert "continued result" in result2
        session_id_used = runtime.handle_input.call_args[0][0]
        assert session_id_used == "sess1:sub:agent-1"

    @pytest.mark.asyncio
    async def test_resume_not_found(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        with pytest.raises(RuntimeError, match="agent not found"):
            await _run(
                registry, "agent", context=_ctx(),
                prompt="continue", description="test", resume="nonexistent",
            )

    @pytest.mark.asyncio
    async def test_resume_still_running(self) -> None:
        runtime = _FakeRuntime()
        async def slow(*args: Any, **kwargs: Any) -> _FakeLoopResult:
            await asyncio.sleep(10)
            return _FakeLoopResult(assistant_output="done")

        runtime.handle_input = AsyncMock(side_effect=slow)
        registry = _build_registry(runtime)

        await _run(
            registry, "agent", context=_ctx(),
            prompt="long task", description="long", run_in_background=True,
        )

        with pytest.raises(RuntimeError, match="still running"):
            await _run(
                registry, "agent", context=_ctx(),
                prompt="continue", description="test", resume="agent-1",
            )

        mgr = get_agent_manager()
        bg_task = mgr._background_tasks.get("agent-1")
        bg_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await bg_task


class TestAgentStatusAndList:
    @pytest.mark.asyncio
    async def test_status_shows_completed(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(registry, "agent", context=_ctx(), prompt="task", description="test task")

        status = await _run(registry, "agent.status", agent_id="agent-1")
        assert "completed" in status
        assert "sub-agent done" in status
        assert "test task" in status
        assert "type: general" in status

    @pytest.mark.asyncio
    async def test_status_not_found(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        with pytest.raises(RuntimeError, match="agent not found"):
            await _run(registry, "agent.status", agent_id="nonexistent")

    @pytest.mark.asyncio
    async def test_list_agents(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        await _run(registry, "agent", context=_ctx(), prompt="task1", description="first")
        await _run(registry, "agent", context=_ctx(), prompt="task2", description="second")

        listing = await _run(registry, "agent.list")
        assert "agent-1" in listing
        assert "agent-2" in listing
        assert "first" in listing
        assert "second" in listing

    @pytest.mark.asyncio
    async def test_list_empty(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)

        listing = await _run(registry, "agent.list")
        assert listing == "(no agents)"


class TestBuiltinAgentTypes:
    def test_explore_is_read_only(self) -> None:
        cfg = BUILTIN_AGENT_TYPES["explore"]
        assert cfg.read_only is True
        assert cfg.allowed_tools is not None
        assert "fs.write" not in cfg.allowed_tools
        assert "fs.edit" not in cfg.allowed_tools

    def test_plan_is_read_only(self) -> None:
        cfg = BUILTIN_AGENT_TYPES["plan"]
        assert cfg.read_only is True

    def test_general_has_full_access(self) -> None:
        cfg = BUILTIN_AGENT_TYPES["general"]
        assert cfg.read_only is False
        assert cfg.allowed_tools is None

    def test_read_only_tools_are_safe(self) -> None:
        """Verify read-only tool set contains no write tools."""
        write_tools = {"fs.write", "fs.edit", "agent", "task.create", "task.update", "task.delete"}
        assert not _READ_ONLY_TOOLS & write_tools

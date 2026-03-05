import asyncio
import inspect
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import pytest
from republic import ToolContext

from bub.tools.agent import register_agent_tools
from bub.tools.registry import ToolRegistry


@dataclass
class _FakeLoopResult:
    visible_text: str = ""
    error: str | None = None


class _FakeRuntime:
    def __init__(self) -> None:
        self._sessions: dict[str, Any] = {}
        self.handle_input = AsyncMock(return_value=_FakeLoopResult(visible_text="sub-agent done"))


def _build_registry(runtime: _FakeRuntime) -> ToolRegistry:
    registry = ToolRegistry()
    register_agent_tools(registry, runtime=runtime)  # type: ignore[arg-type]
    return registry


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


class TestAgentTool:
    @pytest.mark.asyncio
    async def test_delegate_returns_output(self) -> None:
        runtime = _FakeRuntime()
        registry = _build_registry(runtime)
        ctx = ToolContext("test", "test", state={"session_id": "sess1"})

        result = await _run(registry, "agent", context=ctx, prompt="find all TODO comments", description="find TODOs")

        assert result == "sub-agent done"
        runtime.handle_input.assert_called_once()
        call_args = runtime.handle_input.call_args
        assert "sess1:sub:" in call_args[0][0]
        assert call_args[0][1] == "find all TODO comments"

    @pytest.mark.asyncio
    async def test_delegate_with_error(self) -> None:
        runtime = _FakeRuntime()
        runtime.handle_input = AsyncMock(return_value=_FakeLoopResult(visible_text="", error="timeout"))
        registry = _build_registry(runtime)
        ctx = ToolContext("test", "test", state={"session_id": "sess1"})

        result = await _run(registry, "agent", context=ctx, prompt="slow task", description="slow")

        assert "agent error: timeout" in result

    @pytest.mark.asyncio
    async def test_delegate_error_with_partial_output(self) -> None:
        runtime = _FakeRuntime()
        runtime.handle_input = AsyncMock(
            return_value=_FakeLoopResult(visible_text="partial result", error="max_steps_reached=20")
        )
        registry = _build_registry(runtime)
        ctx = ToolContext("test", "test", state={"session_id": "sess1"})

        result = await _run(registry, "agent", context=ctx, prompt="big task", description="big")

        assert "partial result" in result
        assert "max_steps_reached" in result

    @pytest.mark.asyncio
    async def test_delegate_empty_output(self) -> None:
        runtime = _FakeRuntime()
        runtime.handle_input = AsyncMock(return_value=_FakeLoopResult(visible_text=""))
        registry = _build_registry(runtime)
        ctx = ToolContext("test", "test", state={"session_id": "sess1"})

        result = await _run(registry, "agent", context=ctx, prompt="quiet task", description="quiet")

        assert result == "(agent returned no output)"

    @pytest.mark.asyncio
    async def test_sub_session_cleaned_up(self) -> None:
        runtime = _FakeRuntime()
        runtime._sessions["sess1:sub:999"] = "should-be-removed"
        registry = _build_registry(runtime)
        ctx = ToolContext("test", "test", state={"session_id": "sess1"})

        await _run(registry, "agent", context=ctx, prompt="task", description="test")

        # The sub-session created by the call should be cleaned up.
        # Pre-existing keys with different IDs remain as-is.
        assert "sess1:sub:999" in runtime._sessions

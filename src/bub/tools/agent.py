"""Agent delegation tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, Field
from republic import ToolContext

from bub.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from bub.app.runtime import AppRuntime


class AgentInput(BaseModel):
    prompt: str = Field(..., description="Task description for the sub-agent")
    description: str = Field(default="", description="Short label for this delegation (3-5 words)")


def register_agent_tools(
    registry: ToolRegistry,
    *,
    runtime: AppRuntime,
) -> None:
    """Register agent delegation tool."""

    register = registry.register

    @register(name="agent", short_description="Delegate a task to a sub-agent and return result", model=AgentInput, context=True)
    async def agent_delegate(params: AgentInput, context: ToolContext) -> str:
        """Spawn a sub-agent session to handle a complex task autonomously.

        The sub-agent runs with the same model and tools but in an isolated session.
        It receives the prompt, executes until completion, and returns the final
        visible output. Use this for multi-step research, code exploration, or any
        task that benefits from independent execution without polluting the current
        conversation context.
        """
        parent_session_id = context.state.get("session_id", "")
        sub_session_id = f"{parent_session_id}:sub:{_next_sub_id()}"

        result = await runtime.handle_input(sub_session_id, params.prompt)

        # Clean up the sub-session after completion to free memory.
        runtime._sessions.pop(sub_session_id, None)

        output = result.visible_text.strip() if result.visible_text else ""
        if result.error:
            if output:
                return f"{output}\n\n(agent error: {result.error})"
            return f"agent error: {result.error}"
        return output or "(agent returned no output)"


_sub_counter = 0


def _next_sub_id() -> str:
    global _sub_counter
    _sub_counter += 1
    return str(_sub_counter)

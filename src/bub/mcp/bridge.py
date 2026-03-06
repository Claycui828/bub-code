"""Bridge MCP tools into bub's ToolRegistry."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from bub.mcp.client import McpClientManager, McpToolInfo
from bub.tools.registry import ToolGuidance, ToolRegistry


def _build_pydantic_model(qualified_name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """Dynamically create a Pydantic model from a JSON schema for tool input."""
    properties = schema.get("properties", {})
    required = set(schema.get("required", []))

    field_definitions: dict[str, Any] = {}
    annotations: dict[str, Any] = {}
    for prop_name, prop_schema in properties.items():
        prop_type = prop_schema.get("type", "string")
        description = prop_schema.get("description", "")
        default = prop_schema.get("default")

        # Map JSON schema types to Python types
        type_map: dict[str, type] = {
            "string": str,
            "integer": int,
            "number": float,
            "boolean": bool,
            "array": list,
            "object": dict,
        }
        python_type = type_map.get(prop_type, str)

        if prop_name in required:
            field_definitions[prop_name] = Field(..., description=description)
            annotations[prop_name] = python_type
        else:
            field_definitions[prop_name] = Field(default=default, description=description)
            annotations[prop_name] = python_type | None

    # Create model class dynamically
    model_name = qualified_name.replace(".", "_").replace("-", "_")
    namespace = {"__annotations__": annotations, **field_definitions}
    model = type(model_name, (BaseModel,), namespace)
    return model


def register_mcp_tools(
    registry: ToolRegistry,
    manager: McpClientManager,
) -> int:
    """Register all discovered MCP tools into the ToolRegistry.

    Returns the number of tools registered.
    """
    count = 0
    for qualified_name, info in manager.all_tools():
        _register_one(registry, manager, qualified_name, info)
        count += 1
    return count


def _register_one(
    registry: ToolRegistry,
    manager: McpClientManager,
    qualified_name: str,
    info: McpToolInfo,
) -> None:
    """Register a single MCP tool."""
    # Build a Pydantic model from the tool's input schema
    model = _build_pydantic_model(qualified_name, info.input_schema)

    # Capture info in closure
    server_name = info.server_name
    tool_name = info.tool_name

    @registry.register(
        name=qualified_name,
        short_description=info.description[:120] if info.description else f"MCP tool from {server_name}",
        detail=info.description,
        model=model,
        source=f"mcp:{server_name}",
        always_expand=False,
        guidance=ToolGuidance(
            constraints=f"Provided by MCP server '{server_name}'. Arguments are forwarded as-is.",
        ),
    )
    async def mcp_tool_handler(params: BaseModel, _sn: str = server_name, _tn: str = tool_name) -> str:
        arguments = params.model_dump(exclude_none=True)
        return await manager.call_tool(_sn, _tn, arguments)

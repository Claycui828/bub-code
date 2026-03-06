"""Unified tool registry."""

from __future__ import annotations

import builtins
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from functools import wraps
from typing import Any, cast

from loguru import logger
from pydantic import BaseModel, Field
from republic import Tool, ToolContext, tool_from_model

# Type for live output callback: (event_type, data)
LiveCallback = Callable[[str, dict[str, Any]], None]


def _shorten_text(text: str, width: int = 30, placeholder: str = "...") -> str:
    """Shorten text to width characters, cutting in the middle of words if needed.

    Unlike textwrap.shorten, this function can cut in the middle of a word,
    ensuring long strings without spaces are still truncated properly.
    """
    if len(text) <= width:
        return text

    # Reserve space for placeholder
    available = width - len(placeholder)
    if available <= 0:
        return placeholder

    return text[:available] + placeholder


def _ensure_description_field(model: type[BaseModel]) -> type[BaseModel]:
    """Ensure the model has a 'description' field for operation explanation.

    If the model already has a 'description' field, return it unchanged.
    Otherwise, dynamically create a subclass with the field added.
    """
    if "description" in model.model_fields:
        return model
    ns = {
        "__annotations__": {"description": str},
        "description": Field(default="", description="Brief explanation of what this specific tool call does and why"),
    }
    return type(f"{model.__name__}_", (model,), ns)


def _output_preview(result: Any) -> str:
    """Build a short preview string from a tool result."""
    output_str = str(result) if result else ""
    lines = output_str.splitlines()
    if len(lines) > 1:
        return f"{len(lines)} lines"
    if len(output_str) > 80:
        return f"{len(output_str)} chars"
    return _shorten_text(output_str, width=60)


@dataclass(frozen=True)
class ToolGuidance:
    """Structured usage guidance for a tool."""

    when_to_use: str = ""
    when_not_to: str = ""
    examples: str = ""
    constraints: str = ""


@dataclass(frozen=True)
class ToolDescriptor:
    """Tool metadata and runtime handle."""

    name: str
    short_description: str
    detail: str
    tool: Tool
    source: str = "builtin"
    always_expand: bool = False
    guidance: ToolGuidance | None = None


class ToolRegistry:
    """Registry for built-in tools, internal commands, and skill-backed tools."""

    def __init__(self, allowed_tools: set[str] | None = None) -> None:
        self._tools: dict[str, ToolDescriptor] = {}
        self._allowed_tools = allowed_tools
        self._live_callback: LiveCallback | None = None

    def set_live_callback(self, callback: LiveCallback | None) -> None:
        """Set callback for tool execution live events."""
        self._live_callback = callback

    def _emit_live(self, event: str, data: dict[str, Any]) -> None:
        if self._live_callback:
            self._live_callback(event, data)

    def _wrap_handler[**P, T](
        self,
        func: Callable[P, T | Awaitable[T]],
        *,
        name: str,
        context: bool,
    ) -> Callable[..., Awaitable[T]]:
        """Wrap a tool handler with logging and description stripping."""

        @wraps(func)
        async def handler(*args: P.args, **kwargs: P.kwargs) -> T:
            context_arg = kwargs.get("context") if context else None
            call_kwargs = {key: value for key, value in kwargs.items() if key != "context"}
            if args and isinstance(args[0], BaseModel):
                call_kwargs.update(args[0].model_dump())
            call_kwargs.pop("description", None)
            if not self._live_callback:
                self._log_tool_call(name, call_kwargs, cast("ToolContext | None", context_arg))

            start = time.monotonic()
            try:
                result = func(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                if not self._live_callback:
                    logger.exception("tool.call.error name={}", name)
                raise
            else:
                return result
            finally:
                duration = time.monotonic() - start
                if not self._live_callback:
                    logger.info("tool.call.end name={} duration={:.3f}ms", name, duration * 1000)

        return handler

    def register(
        self,
        *,
        name: str,
        short_description: str,
        detail: str | None = None,
        model: type[BaseModel] | None = None,
        context: bool = False,
        source: str = "builtin",
        always_expand: bool = False,
        guidance: ToolGuidance | None = None,
    ) -> Callable[[Callable], ToolDescriptor | None]:
        def decorator[**P, T](func: Callable[P, T | Awaitable[T]]) -> ToolDescriptor | None:
            tool_detail = detail or func.__doc__ or ""
            if (
                self._allowed_tools is not None
                and name.casefold() not in self._allowed_tools
                and self.to_model_name(name).casefold() not in self._allowed_tools
            ):
                return None

            handler = self._wrap_handler(func, name=name, context=context)

            if model is not None:
                extended_model = _ensure_description_field(model)
                tool = tool_from_model(
                    extended_model, handler, name=name, description=short_description, context=context
                )
            else:
                tool = Tool.from_callable(handler, name=name, description=short_description, context=context)
            tool_desc = ToolDescriptor(
                name=name,
                short_description=short_description,
                detail=tool_detail,
                tool=tool,
                source=source,
                always_expand=always_expand,
                guidance=guidance,
            )
            self._tools[name] = tool_desc
            return tool_desc

        return decorator

    def has(self, name: str) -> bool:
        return name in self._tools

    def get(self, name: str) -> ToolDescriptor | None:
        return self._tools.get(name)

    def descriptors(self) -> builtins.list[ToolDescriptor]:
        return sorted(self._tools.values(), key=lambda item: item.name)

    @staticmethod
    def to_model_name(name: str) -> str:
        return name.replace(".", "_")

    def compact_rows(self, *, for_model: bool = False) -> builtins.list[str]:
        rows: builtins.list[str] = []
        for descriptor in self.descriptors():
            display_name = self.to_model_name(descriptor.name) if for_model else descriptor.name
            if for_model and display_name != descriptor.name:
                rows.append(f"{display_name} (command: {descriptor.name}): {descriptor.short_description}")
            else:
                rows.append(f"{display_name}: {descriptor.short_description}")
        return rows

    def detail(self, name: str, *, for_model: bool = False) -> str:
        descriptor = self.get(name)
        if descriptor is None:
            raise KeyError(name)

        schema = descriptor.tool.schema()
        display_name = descriptor.name
        command_name_line = ""
        if for_model:
            schema = deepcopy(schema)
            display_name = self.to_model_name(descriptor.name)
            function = schema.get("function")
            if isinstance(function, dict):
                function["name"] = display_name
            if display_name != descriptor.name:
                command_name_line = f"command_name: {descriptor.name}\n"

        lines = [
            f"name: {display_name}",
            f"{command_name_line}source: {descriptor.source}" if command_name_line else f"source: {descriptor.source}",
            f"description: {descriptor.short_description}",
            f"detail: {descriptor.detail}",
        ]
        if descriptor.guidance:
            g = descriptor.guidance
            if g.when_to_use:
                lines.append(f"when_to_use: {g.when_to_use}")
            if g.when_not_to:
                lines.append(f"when_not_to: {g.when_not_to}")
            if g.examples:
                lines.append(f"examples: {g.examples}")
            if g.constraints:
                lines.append(f"constraints: {g.constraints}")
        lines.append(f"schema: {schema}")
        return "\n".join(lines)

    def _make_live_handler(self, tool_name: str, original_handler: Any) -> Any:
        """Wrap a tool handler to emit live events during Republic's automatic dispatch."""

        @wraps(original_handler)
        async def _live_handler(*args: Any, **kwargs: Any) -> Any:
            desc = ""
            if args and isinstance(args[0], BaseModel):
                desc = getattr(args[0], "description", "") or ""
            elif "description" in kwargs:
                desc = str(kwargs.get("description", ""))

            self._emit_live("tool.start", {"name": tool_name, "description": desc})
            start = time.monotonic()
            try:
                result = original_handler(*args, **kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                elapsed_ms = (time.monotonic() - start) * 1000
                self._emit_live(
                    "tool.error",
                    {
                        "name": tool_name,
                        "error": _shorten_text(str(exc), width=80),
                        "elapsed_ms": elapsed_ms,
                    },
                )
                raise
            else:
                elapsed_ms = (time.monotonic() - start) * 1000
                preview = _output_preview(result)
                self._emit_live(
                    "tool.end",
                    {
                        "name": tool_name,
                        "status": "ok",
                        "elapsed_ms": elapsed_ms,
                        "output_preview": preview,
                    },
                )
                return result

        return _live_handler

    def model_tools(self) -> builtins.list[Tool]:
        tools: builtins.list[Tool] = []
        seen_names: set[str] = set()
        for descriptor in self.descriptors():
            model_name = self.to_model_name(descriptor.name)
            if model_name in seen_names:
                raise ValueError(f"Duplicate model tool name after conversion: {model_name}")
            seen_names.add(model_name)

            base = descriptor.tool
            live_handler = self._make_live_handler(descriptor.name, base.handler)

            tools.append(
                Tool(
                    name=model_name,
                    description=base.description,
                    parameters=base.parameters,
                    handler=live_handler,
                    context=base.context,
                )
            )
        return tools

    def _log_tool_call(self, name: str, kwargs: dict[str, Any], context: ToolContext | None) -> None:
        params: list[str] = []
        for key, value in kwargs.items():
            try:
                rendered = json.dumps(value, ensure_ascii=False)
            except TypeError:
                rendered = repr(value)
            value = _shorten_text(rendered, width=60, placeholder="...")
            if value.startswith('"') and not value.endswith('"'):
                value = value + '"'
            if value.startswith("{") and not value.endswith("}"):
                value = value + "}"
            if value.startswith("[") and not value.endswith("]"):
                value = value + "]"
            params.append(f"{key}={value}")
        params_str = ", ".join(params)
        logger.info("tool.call.start name={} {{ {} }}", name, params_str)

    async def execute(
        self,
        name: str,
        *,
        kwargs: dict[str, Any],
        context: ToolContext | None = None,
    ) -> Any:
        from bub.observability import current_tracer

        descriptor = self.get(name)
        if descriptor is None:
            raise KeyError(name)

        # Extract description for live display.
        call_kwargs = {key: value for key, value in kwargs.items() if key != "context"}
        description = str(call_kwargs.pop("description", "") or "")
        self._emit_live("tool.start", {"name": name, "description": description})

        tracer = current_tracer()
        start_time = time.monotonic()
        with tracer.span(f"tool.{name}", input=call_kwargs, metadata={"source": descriptor.source}) as span:
            if descriptor.tool.context:
                kwargs["context"] = context
            try:
                result = descriptor.tool.run(**kwargs)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:
                elapsed_ms = (time.monotonic() - start_time) * 1000
                span.end(output=str(exc), level="ERROR")
                self._emit_live(
                    "tool.error",
                    {
                        "name": name,
                        "error": _shorten_text(str(exc), width=80),
                        "elapsed_ms": elapsed_ms,
                    },
                )
                raise
            else:
                elapsed_ms = (time.monotonic() - start_time) * 1000
                span.end(output=str(result)[:4096] if result else None)

                preview = _output_preview(result)
                self._emit_live(
                    "tool.end",
                    {
                        "name": name,
                        "status": "ok",
                        "elapsed_ms": elapsed_ms,
                        "output_preview": preview,
                    },
                )
                return result

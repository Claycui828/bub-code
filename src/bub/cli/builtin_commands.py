"""Built-in slash commands for the CLI channel."""

from __future__ import annotations

from bub.cli.commands import CommandContext, CommandRegistry, SlashCommand


def register_builtin_commands(registry: CommandRegistry) -> None:
    """Register all built-in slash commands."""
    _register_fold_commands(registry)
    _register_panel_commands(registry)
    _register_control_commands(registry)
    _register_agent_control_commands(registry)
    _register_context_commands(registry)
    _register_task_commands(registry)


# ---------------------------------------------------------------------------
# Display: fold/unfold
# ---------------------------------------------------------------------------


def _register_fold_commands(registry: CommandRegistry) -> None:
    async def _handle_fold(args: str, ctx: CommandContext) -> str | None:
        if args.strip():
            try:
                ctx.renderer.fold_at(int(args.strip()))
            except (ValueError, IndexError):
                ctx.renderer.info(f"Invalid panel index: {args.strip()}")
        else:
            ctx.renderer.fold_last()
        return None

    async def _handle_unfold(args: str, ctx: CommandContext) -> str | None:
        if args.strip():
            try:
                ctx.renderer.unfold_at(int(args.strip()))
            except (ValueError, IndexError):
                ctx.renderer.info(f"Invalid panel index: {args.strip()}")
        else:
            ctx.renderer.unfold_last()
        return None

    registry.register(SlashCommand(
        name="fold", description="Fold output panel", handler=_handle_fold,
        aliases=["f"], category="display",
    ))
    registry.register(SlashCommand(
        name="unfold", description="Unfold output panel", handler=_handle_unfold,
        aliases=["u"], category="display",
    ))


# ---------------------------------------------------------------------------
# Display: panels, clear, search
# ---------------------------------------------------------------------------


def _register_panel_commands(registry: CommandRegistry) -> None:
    async def _handle_panels(args: str, ctx: CommandContext) -> str | None:
        ctx.renderer.list_panels()
        return None

    async def _handle_clear(args: str, ctx: CommandContext) -> str | None:
        ctx.renderer.clear()
        return None

    async def _handle_search(args: str, ctx: CommandContext) -> str | None:
        if not args.strip():
            ctx.renderer.info("Usage: /search <query>")
            return None
        ctx.renderer.search_panels(args.strip())
        return None

    registry.register(SlashCommand(
        name="panels", description="List all output panels", handler=_handle_panels,
        aliases=["p"], category="display",
    ))
    registry.register(SlashCommand(
        name="clear", description="Clear screen and panels", handler=_handle_clear,
        category="display",
    ))
    registry.register(SlashCommand(
        name="search", description="Search in panel output", handler=_handle_search,
        category="display",
    ))


# ---------------------------------------------------------------------------
# Control: stop, inject
# ---------------------------------------------------------------------------


def _register_control_commands(registry: CommandRegistry) -> None:
    async def _handle_stop(args: str, ctx: CommandContext) -> str | None:
        if ctx.agent_running:
            ctx.channel.force_cancel()
        else:
            ctx.renderer.info("No agent running.")
        return None

    async def _handle_inject(args: str, ctx: CommandContext) -> str | None:
        if not args.strip():
            ctx.renderer.info("Usage: /inject <message>")
            return None
        if ctx.session:
            ctx.session.inject_message(args.strip())
            ctx.renderer.info(f"Injected: {args.strip()}")
        return None

    registry.register(SlashCommand(
        name="stop", description="Force-cancel running agent", handler=_handle_stop,
        category="control",
    ))
    registry.register(SlashCommand(
        name="inject", description="Inject message into agent context", handler=_handle_inject,
        category="control",
    ))


# ---------------------------------------------------------------------------
# Control: pause, resume, step
# ---------------------------------------------------------------------------


def _register_agent_control_commands(registry: CommandRegistry) -> None:
    async def _handle_pause(args: str, ctx: CommandContext) -> str | None:
        if not ctx.agent_running:
            ctx.renderer.info("No agent running.")
            return None
        if ctx.session:
            ctx.session.model_runner.request_pause()
            ctx.renderer.info("Agent will pause after current step.")
        return None

    async def _handle_resume(args: str, ctx: CommandContext) -> str | None:
        if ctx.session:
            ctx.session.model_runner.request_resume()
            ctx.renderer.info("Agent resumed.")
        return None

    async def _handle_step(args: str, ctx: CommandContext) -> str | None:
        if ctx.session:
            ctx.session.model_runner.request_step()
            ctx.renderer.info("Single-step mode: agent will pause after next step.")
        return None

    registry.register(SlashCommand(
        name="pause", description="Pause agent after current step", handler=_handle_pause,
        category="control",
    ))
    registry.register(SlashCommand(
        name="resume", description="Resume paused agent", handler=_handle_resume,
        category="control",
    ))
    registry.register(SlashCommand(
        name="step", description="Single-step mode (pause after each step)", handler=_handle_step,
        category="control",
    ))


# ---------------------------------------------------------------------------
# Context / info commands
# ---------------------------------------------------------------------------


def _register_context_commands(registry: CommandRegistry) -> None:
    async def _handle_status(args: str, ctx: CommandContext) -> str | None:
        ctx.renderer.render_status(
            agent_running=ctx.agent_running,
            session=ctx.session,
            model=ctx.channel.runtime.settings.model,
        )
        return None

    async def _handle_help(args: str, ctx: CommandContext) -> str | None:
        help_text = ctx.channel.command_registry.help_text()
        ctx.renderer.info(f"Available commands:{help_text}")
        return None

    async def _handle_context(args: str, ctx: CommandContext) -> str | None:
        if ctx.session:
            await ctx.renderer.render_context(ctx.session)
        else:
            ctx.renderer.info("No active session.")
        return None

    registry.register(SlashCommand(
        name="status", description="Show agent status", handler=_handle_status,
        aliases=["s"], category="context",
    ))
    registry.register(SlashCommand(
        name="help", description="Show available commands", handler=_handle_help,
        aliases=["h", "?"], category="context",
    ))
    registry.register(SlashCommand(
        name="context", description="Show context chain and tape info", handler=_handle_context,
        aliases=["ctx"], category="context",
    ))


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def _register_task_commands(registry: CommandRegistry) -> None:
    async def _handle_tasks(args: str, ctx: CommandContext) -> str | None:
        ctx.renderer.render_tasks(ctx.channel.runtime.workspace)
        return None

    registry.register(SlashCommand(
        name="tasks", description="Show task list", handler=_handle_tasks,
        aliases=["t"], category="context",
    ))

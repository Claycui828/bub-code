# Bub

[![Release](https://img.shields.io/github/v/release/bubbuild/bub)](https://github.com/bubbuild/bub/releases)
[![Build status](https://img.shields.io/github/actions/workflow/status/bubbuild/bub/main.yml?branch=main)](https://github.com/bubbuild/bub/actions/workflows/main.yml?query=branch%3Amain)
[![Commit activity](https://img.shields.io/github/commit-activity/m/bubbuild/bub)](https://github.com/bubbuild/bub/graphs/commit-activity)
[![License](https://img.shields.io/github/license/bubbuild/bub)](LICENSE)

> Bub it. Build it.

Bub is a collaborative coding agent for shared delivery workflows, with sub-agent delegation, context management, and multi-channel support.
It is designed for shared environments where work must be inspectable, handoff-friendly, and operationally reliable.

> Documentation: <https://bub.build>

Built on [Republic](https://github.com/bubbuild/republic), Bub treats context as explicit assembly from verifiable interaction history, rather than opaque inherited state.

## What Bub Provides

- **Agent loop** with think-act cycle, tool dispatch, and automatic context management.
- **Sub-agent delegation** — spawn isolated agents (explore, plan, general) with live streaming display.
- **Progressive tool discovery** — compact tool list first, expanded schema on demand via `$hint`.
- **MCP integration** — connect external tool servers via `mcp_servers.yaml`.
- **Tape system** — append-only history with `anchor`/`handoff` for context checkpoints.
- **Task tracking** — create and track multi-step work plans.
- **Multi-channel** — CLI, Telegram, Discord with unified behavior.
- **Observability** — Langfuse and OpenTelemetry tracing backends.

## Quick Start

```bash
git clone https://github.com/bubbuild/bub.git
cd bub
uv sync
cp env.example .env
```

Minimal `.env`:

```bash
BUB_MODEL=openrouter:minimax/minimax-m2.5
LLM_API_KEY=your_key_here
```

Start interactive CLI:

```bash
uv run bub
```

## Configuration

Bub reads configuration from three sources (highest priority wins):

1. **Environment variables** (`BUB_` prefix) and `.env` file
2. **`bub.yaml`** in workspace root (alternative to `.env`)
3. Built-in defaults

Example `bub.yaml`:

```yaml
model: "openrouter:minimax/minimax-m2.5"
max_tokens: 16384
max_steps: 100
model_timeout_seconds: 300
tape_name: "bub"
```

See `env.example` for all available settings.

## CLI Interaction

### Input modes

| Input | Effect |
|-------|--------|
| `hello` | Natural language → agent loop |
| `,help` | Internal command |
| `,git status` | Shell command via bash tool |
| `@src/main.py` | Inline file content into prompt |
| `@src/` | Inline directory listing into prompt |

Press `Ctrl-X` to toggle shell mode (auto-prefixes `,` to every input).

### Slash commands

Slash commands control the CLI display and agent — they are separate from agent tools.

```text
/help, /h, /?     Show available commands
/status, /s       Show agent status (running/idle, model, mode)
/context, /ctx    Show tape context chain and anchor info
/tasks, /t        Show task list with status
/fold [N], /f     Fold output panel (last or by index)
/unfold [N], /u   Unfold output panel
/panels, /p       List all output panels
/clear            Clear screen and reset panels
/search <query>   Search across panel output
/stop             Force-cancel running agent
/pause            Pause agent after current step
/resume           Resume paused agent
/step             Single-step mode (pause after each step)
/inject <msg>     Inject message into agent context
```

### Agent commands (comma prefix)

These are tools the agent can also call. Use `,` prefix to call them directly:

```text
,help                                   Show command help
,tools                                  List available tools
,tool.describe name=fs.read             Show tool schema and guidance
,tape.handoff name=phase-1 summary="bootstrap done"
,tape.info                              Show context size
,tape.anchors                           List anchors
,tape.search query=error                Search conversation history
,skills.list                            List discovered skills
,schedule.add cron='*/5 * * * *' message='check status'
,quit                                   Exit
```

## Built-in Tools

| Tool | Description |
|------|-------------|
| `bash` | Execute shell commands |
| `fs.read` | Read file content with optional line range |
| `fs.write` | Create or overwrite files |
| `fs.edit` | Find-and-replace text in files |
| `fs.grep` | Search file contents by regex (uses ripgrep) |
| `fs.glob` | Find files by glob pattern |
| `web.fetch` | Fetch URL content as text |
| `web.search` | Web search (Exa / Brave / Ollama backends) |
| `agent` | Delegate task to isolated sub-agent |
| `agent.status` | Check sub-agent progress |
| `agent.list` | List all sub-agents |
| `task.create` | Create a trackable task |
| `task.update` | Update task status |
| `task.list` | List tasks |
| `tape.handoff` | Context checkpoint — reset conversation window |
| `tape.info` | Show context size and anchor status |
| `schedule.add` | Schedule a future or recurring reminder |
| `tools` | List all tools |
| `tool.describe` | Show full tool schema and guidance |
| `skills.list` | List discovered skills |

### Sub-agent types

The `agent` tool supports predefined agent types:

- **`explore`** — Fast read-only codebase search. Uses `fs.glob`/`fs.grep`/`fs.read`/`bash`(read-only). Best for finding files and understanding project structure.
- **`plan`** — Architecture research and planning in read-only mode. Returns step-by-step implementation plans.
- **`general`** — Full tool access for complex multi-step tasks. Can read, write, edit files and run commands.

Sub-agents run in isolated sessions with their own tape. Multiple agents can run in parallel via `run_in_background=true`.

## MCP Integration

Connect external tool servers via MCP (Model Context Protocol). Create `mcp_servers.yaml`:

- Global: `~/.bub/mcp_servers.yaml`
- Project: `<workspace>/.bub/mcp_servers.yaml`

```yaml
filesystem:
  command: npx
  args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]

github:
  command: uvx
  args: ["mcp-server-github"]
  env:
    GITHUB_TOKEN: "ghp_..."

remote-server:
  url: "http://localhost:8000/mcp"
```

MCP tools appear with `mcp__<server>__<tool>` naming. Use `$mcp__server__tool` in conversation to discover their schema before calling.

## Channel Runtime

### Telegram

```bash
BUB_TELEGRAM_ENABLED=true
BUB_TELEGRAM_TOKEN=123456:token
BUB_TELEGRAM_ALLOW_FROM='["123456789","your_username"]'
uv run bub message
```

### Discord

```bash
BUB_DISCORD_ENABLED=true
BUB_DISCORD_TOKEN=discord_bot_token
BUB_DISCORD_ALLOW_FROM='["123456789012345678","your_discord_name"]'
BUB_DISCORD_ALLOW_CHANNELS='["123456789012345678"]'
uv run bub message
```

## Observability

Enable tracing to inspect agent execution:

```yaml
# bub.yaml
trace_enabled: true
trace_backend: langfuse  # langfuse | otel
```

**Langfuse**: Set `BUB_LANGFUSE_PUBLIC_KEY`, `BUB_LANGFUSE_SECRET_KEY`, `BUB_LANGFUSE_HOST`.

**OpenTelemetry**: Set `BUB_OTEL_ENDPOINT` (OTLP gRPC). Default service name: `bub`.

## Development

```bash
uv sync                    # Install dependencies
uv run pytest              # Run tests
uv run ruff check src/     # Lint
uv run ruff format src/    # Format
uv run mypy                # Type check
```

## License

[Apache 2.0](./LICENSE)

# Claude Code Sub-Agent 架构调研

> 调研来源：Claude Code v2.1.69 (2026-03-04) system prompt、官方文档、社区逆向工程项目

## 1. 整体架构

Claude Code 的 sub-agent 系统是一个**单层委派模型**：

```
Main Agent (Opus/Sonnet)
  ├── Explore Agent (Haiku) — 只读，快速搜索
  ├── Plan Agent (inherit) — 只读，架构设计
  ├── General-purpose Agent (inherit) — 全能，读写均可
  ├── Claude Code Guide (Haiku) — 回答 Claude Code 使用问题
  ├── Statusline Setup (Sonnet) — 配置状态栏
  └── Custom Agents (用户定义) — .claude/agents/*.md
```

**关键约束：sub-agent 不能再产生 sub-agent**，防止无限嵌套。

### 委派入口：Agent Tool (原 Task Tool)

主 agent 通过一个叫 `Agent` 的 tool（v2.1.63 前叫 `Task`）来产生 sub-agent：

```xml
<invoke name="Agent">
  <parameter name="subagent_type">Explore</parameter>
  <parameter name="description">Search auth module</parameter>
  <parameter name="prompt">Find all authentication-related files...</parameter>
  <parameter name="run_in_background">false</parameter>
  <parameter name="model">haiku</parameter>
  <parameter name="resume">agent-123</parameter>
</invoke>
```

| 参数 | 必填 | 说明 |
|------|------|------|
| `subagent_type` | 否 | 选择 agent 类型，省略则用 general-purpose |
| `description` | 是 | 3-5 词任务摘要 |
| `prompt` | 是 | 完整任务描述（sub-agent 看不到父对话） |
| `run_in_background` | 否 | true=异步执行，完成后通知 |
| `model` | 否 | sonnet/opus/haiku，默认 inherit |
| `resume` | 否 | 传入 agent_id 恢复之前的 agent |
| `isolation` | 否 | "worktree" 在 git worktree 中隔离运行 |

---

## 2. 内置 Sub-Agent 详解

### 2.1 Explore Agent

**定位**：快速、只读的代码库搜索专家。用 Haiku 模型降低成本和延迟。

**模型**：Haiku（固定，不继承父 agent）

**工具集**：只读工具
- Glob — 文件名模式匹配
- Grep — 文件内容正则搜索
- Read — 读取文件内容
- Bash — **仅限只读命令**（ls, git status, git log, git diff, find, cat, head, tail）
- ~~Write, Edit, NotebookEdit, Agent, ExitPlanMode~~ — 明确禁止

**System Prompt 核心内容**：

```
You are a file search specialist for Claude Code.

=== CRITICAL: READ-ONLY MODE - NO FILE MODIFICATIONS ===

This is a READ-ONLY exploration task. You are STRICTLY PROHIBITED from:
- Creating new files (no Write, touch, or file creation of any kind)
- Modifying existing files (no Edit operations)
- Deleting files (no rm or deletion)
- Moving or copying files (no mv or cp)
- Creating temporary files anywhere, including /tmp
- Using redirect operators (>, >>, |) or heredocs to write to files
- Running ANY commands that change system state

Your strengths:
- Rapidly finding files using glob patterns
- Searching code and text with powerful regex patterns
- Reading and analyzing file contents

Guidelines:
- Use Glob for broad file pattern matching
- Use Grep for searching file contents with regex
- Use Read when you know the specific file path
- Use Bash ONLY for read-only operations
- NEVER use Bash for: mkdir, touch, rm, cp, mv, git add, git commit, npm install, pip install
- Adapt search approach based on thoroughness level (quick/medium/very thorough)
- Return file paths as absolute paths
- Communicate findings as regular messages — do NOT create files

NOTE: You are meant to be a fast agent. Make efficient use of tools.
Wherever possible spawn multiple parallel tool calls for grepping and reading files.
```

**触发时机**（主 agent 何时委派给 Explore）：
- 需要搜索或理解代码库但不需要修改
- 搜索关键词/文件但不确定能几次找到
- 主 agent 指定 thoroughness level：quick / medium / very thorough

**设计亮点**：
1. **双重约束**：system prompt 用自然语言禁止 + 工具层面不提供 Write/Edit
2. **成本优化**：固定用 Haiku，搜索类任务不需要强推理
3. **并行搜索**：prompt 中明确要求尽可能并行调用多个工具
4. **上下文隔离**：搜索结果留在 sub-agent 上下文中，只有摘要返回主对话

### 2.2 Plan Agent

**定位**：Plan mode 下的代码库研究 agent，为规划收集上下文。

**模型**：继承主对话模型

**工具集**：与 Explore 相同的只读工具
- Glob, Grep, Read, Bash（只读）
- ~~Write, Edit, NotebookEdit, Agent, ExitPlanMode~~ — 禁止

**System Prompt 核心内容**：

```
You are a software architect and planning specialist.

=== CRITICAL: READ-ONLY MODE — NO FILE MODIFICATIONS ===
[同 Explore 的禁止列表]

Operational Process:
1. Understand Requirements: Apply assigned perspective throughout design work.
2. Explore Thoroughly:
   - Read provided files
   - Find existing patterns and conventions using Glob, Grep, Read
   - Understand architecture and identify similar features
   - Use Bash ONLY for read-only operations
3. Design Solution: Create approaches based on perspective, considering trade-offs.
4. Detail the Plan: Step-by-step strategy with dependencies and anticipated challenges.

Required Output Format:
Conclude with "Critical Files for Implementation" section listing 3-5 key files
with brief reasons (e.g., "Core logic to modify", "Interfaces to implement").

You can ONLY explore and plan. You CANNOT and MUST NOT write, edit, or modify any files.
```

**与 Explore 的区别**：
- Explore 是**搜索工具**，返回搜索结果
- Plan 是**架构师**，返回结构化实施方案
- Plan 用继承模型（更强推理），Explore 用 Haiku（更快更便宜）

### 2.3 General-purpose Agent

**定位**：全能型 agent，处理需要读写的复杂多步任务。

**模型**：继承主对话模型

**工具集**：**所有工具**（除 Agent，因为不能嵌套）
- Read, Write, Edit, NotebookEdit
- Glob, Grep
- Bash（无限制）
- WebFetch, WebSearch
- 所有 MCP 工具

**System Prompt**：继承主 agent 的完整 system prompt（不是精简版）

**触发时机**：
- 任务需要同时探索和修改代码
- 需要复杂推理来解释结果
- 包含多个有依赖关系的步骤
- 搜索 + 修改的组合操作

**与主 agent 的区别**：
- 运行在独立上下文窗口（不污染主对话）
- 不能再产生 sub-agent
- 可以在后台运行

### 2.4 Claude Code Guide

**定位**：回答关于 Claude Code 功能、Agent SDK、Claude API 的问题。

**模型**：Haiku

**工具集**：只读 + 网络
- Glob, Grep, Read — 读取本地文档
- WebFetch, WebSearch — 查阅在线文档

**触发时机**：
- 用户问 "Can Claude...", "Does Claude...", "How do I..." 等关于 Claude Code 的问题
- 主 agent 会先检查是否有可以 resume 的之前的 guide agent 实例

### 2.5 其他内置 Agent

| Agent | 模型 | 工具 | 用途 |
|-------|------|------|------|
| Bash | inherit | Bash only | 在独立上下文中执行终端命令 |
| statusline-setup | Sonnet | Read, Edit | 用户运行 /statusline 时配置状态栏 |

---

## 3. 自定义 Sub-Agent 机制

### 3.1 定义方式

Sub-agent 通过 **Markdown + YAML frontmatter** 定义：

```markdown
---
name: code-reviewer
description: Expert code review specialist. Use proactively after code changes.
tools: Read, Grep, Glob, Bash
model: sonnet
permissionMode: default
maxTurns: 20
memory: user
---

You are a senior code reviewer. When invoked:
1. Run git diff to see recent changes
2. Focus on modified files
3. Begin review immediately

Review checklist:
- Code clarity and readability
- Error handling
- Security (no exposed secrets)
- Test coverage
```

### 3.2 配置字段完整列表

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 唯一标识，小写+连字符 |
| `description` | 是 | 主 agent 据此决定何时委派 |
| `tools` | 否 | 工具白名单，省略则继承所有工具 |
| `disallowedTools` | 否 | 工具黑名单，从继承列表中移除 |
| `model` | 否 | sonnet/opus/haiku/inherit，默认 inherit |
| `permissionMode` | 否 | default/acceptEdits/dontAsk/bypassPermissions/plan |
| `maxTurns` | 否 | 最大 agentic 循环次数 |
| `skills` | 否 | 启动时注入的 skill 内容（不是按需加载） |
| `mcpServers` | 否 | 可用的 MCP server |
| `hooks` | 否 | 生命周期钩子（PreToolUse/PostToolUse/Stop） |
| `memory` | 否 | 持久化记忆范围：user/project/local |
| `background` | 否 | true = 始终后台运行 |
| `isolation` | 否 | "worktree" = git worktree 隔离 |

### 3.3 存储位置与优先级

| 位置 | 范围 | 优先级 |
|------|------|--------|
| `--agents` CLI flag | 当前 session | 1（最高） |
| `.claude/agents/` | 当前项目 | 2 |
| `~/.claude/agents/` | 所有项目 | 3 |
| Plugin `agents/` | 安装了插件的项目 | 4（最低） |

### 3.4 工具限制语法

```yaml
# 白名单
tools: Read, Grep, Glob, Bash

# 限制可产生的 sub-agent 类型（仅主 agent 有效）
tools: Agent(worker, researcher), Read, Bash

# 允许所有 sub-agent
tools: Agent, Read, Bash

# 省略 Agent = 不能产生任何 sub-agent
tools: Read, Bash
```

### 3.5 持久化记忆

```yaml
memory: user    # ~/.claude/agent-memory/<name>/
memory: project # .claude/agent-memory/<name>/
memory: local   # .claude/agent-memory-local/<name>/
```

启用后：
- system prompt 自动注入 MEMORY.md 的前 200 行
- 自动开启 Read/Write/Edit 工具让 agent 管理记忆文件
- 跨 session 持久化知识

---

## 4. 运行机制

### 4.1 前台 vs 后台

| | 前台 | 后台 |
|---|------|------|
| 阻塞 | 是 | 否 |
| 权限提示 | 传递给用户 | 启动前预批准 |
| 适用场景 | 结果用于后续步骤 | 独立并行任务 |
| 中断后 | 结果丢失 | 可 resume |

### 4.2 上下文管理

- **隔离**：每个 sub-agent 独立上下文窗口，不共享父对话历史
- **Resume**：通过 agent_id 恢复，保留完整对话历史
- **Auto-compaction**：约 95% 容量时自动压缩（可通过环境变量调整）
- **Transcript 持久化**：保存在 `~/.claude/projects/{project}/{sessionId}/subagents/agent-{id}.jsonl`
- **清理**：默认 30 天后自动清理

### 4.3 模型选择策略

```
搜索/探索类任务 → Haiku（快、便宜）
规划/设计类任务 → inherit（需要推理能力）
执行/修改类任务 → inherit（需要完整能力）
简单辅助任务 → Sonnet（平衡能力和速度）
```

---

## 5. 与 Bub 现有设计的对比

### 5.1 当前 Bub 的 Agent 实现

Bub 当前只有一种 agent 类型（`src/bub/tools/agent.py`）：

```python
class AgentInput(BaseModel):
    prompt: str          # 任务描述
    description: str     # 短标签
    model: str | None    # 覆盖模型
    system_prompt: str   # 覆盖 system prompt
    allowed_tools: list[str] | None  # 工具白名单
    run_in_background: bool          # 后台运行
    resume: str | None               # 恢复 agent
```

通过 `runtime.handle_input(sub_session_id, prompt)` 产生新 session，结束后 `remove_session()` 清理。

### 5.2 对比矩阵

| 能力 | Claude Code | Bub 当前 |
|------|-------------|----------|
| 预定义 agent 类型 | 5+ 内置类型 | 无，只有通用 agent |
| Agent 类型选择 | `subagent_type` 参数自动路由 | 通过 `allowed_tools` 手动限制 |
| 工具限制 | 按 agent 类型预设 + allowlist/denylist | 仅 allowlist |
| System prompt | 每种类型独立 prompt | 继承父 agent 或手动覆盖 |
| 模型路由 | 按类型预设（Explore=Haiku） | 手动指定 |
| 嵌套防护 | 硬性禁止 sub-agent 产生 sub-agent | 无限制 |
| 后台运行 | 支持，预批准权限 | 支持，无权限预批准 |
| Resume | 支持，保留完整上下文 | 支持 |
| Git worktree 隔离 | 支持 | 不支持 |
| 持久化记忆 | 支持 (user/project/local scope) | 不支持 |
| 自定义 agent 定义 | Markdown + YAML frontmatter | 不支持 |
| Auto-compaction | 支持 | 不支持 |
| Hooks | PreToolUse/PostToolUse/Stop | 不支持 |

### 5.3 建议的改进路线

#### Phase 1：预定义 Agent 类型（高优先级）

在 `agent.py` 中引入 `AgentType` 注册表，预定义几种常用类型：

```python
BUILTIN_AGENT_TYPES = {
    "explore": AgentTypeConfig(
        description="Fast read-only codebase search and analysis",
        model_override="haiku",  # 或配置中的轻量模型
        allowed_tools={"fs.read", "fs.grep", "fs.glob", "bash"},
        system_prompt=EXPLORE_SYSTEM_PROMPT,
        read_only=True,
    ),
    "plan": AgentTypeConfig(
        description="Architecture research and implementation planning",
        model_override=None,  # inherit
        allowed_tools={"fs.read", "fs.grep", "fs.glob", "bash"},
        system_prompt=PLAN_SYSTEM_PROMPT,
        read_only=True,
    ),
    "general": AgentTypeConfig(
        description="Complex multi-step tasks requiring exploration and modification",
        model_override=None,  # inherit
        allowed_tools=None,  # all tools
        system_prompt=None,  # inherit
        read_only=False,
    ),
}
```

AgentInput 增加 `agent_type` 字段：
```python
class AgentInput(BaseModel):
    agent_type: str | None = Field(default=None, description="Predefined agent type: explore, plan, general")
    prompt: str = Field(...)
    # ... 其他字段作为覆盖
```

#### Phase 2：嵌套防护（高优先级）

检测 sub-agent session，从其工具集中移除 `agent` 工具：

```python
if is_sub_session(session_id):
    tool_set = tool_set - {"agent"}
```

#### Phase 3：自定义 Agent 定义（中优先级）

从 `.bub/agents/*.md` 加载 Markdown + YAML frontmatter 定义的自定义 agent：

```markdown
---
name: code-reviewer
description: Review code for quality and best practices
tools: [fs.read, fs.grep, fs.glob, bash]
model: null  # inherit
---
You are a senior code reviewer...
```

#### Phase 4：Agent 持久化记忆（低优先级）

为 agent 提供跨 session 的记忆目录，积累领域知识。

---

## 6. 关键设计原则总结

从 Claude Code 的实现中提炼出的核心设计原则：

1. **最小权限**：每种 agent 只给必要的工具。Explore 不需要写，就完全不给 Write/Edit。
2. **双重约束**：工具层面限制 + system prompt 自然语言强调。单靠一层不够可靠。
3. **成本感知路由**：搜索用便宜模型（Haiku），推理用贵模型（inherit Opus/Sonnet）。
4. **上下文隔离**：sub-agent 的详细输出留在自己的上下文中，只返回摘要给父 agent。
5. **单层架构**：禁止嵌套，保持执行层次简单可控。
6. **并行优先**：prompt 中明确鼓励多个搜索并行执行。
7. **可恢复性**：agent 可以 resume，避免重复工作。
8. **Description 驱动委派**：主 agent 根据 agent 的 description 字段自动决定何时委派。

---

## Sources

- [Claude Code 官方 Sub-Agent 文档](https://code.claude.com/docs/en/sub-agents)
- [Piebald-AI/claude-code-system-prompts](https://github.com/Piebald-AI/claude-code-system-prompts) — 逆向工程提取的完整 system prompt
- [Explore Agent System Prompt](https://github.com/Piebald-AI/claude-code-system-prompts/blob/main/system-prompts/agent-prompt-explore.md)
- [Plan Agent System Prompt](https://github.com/Piebald-AI/claude-code-system-prompts/blob/main/system-prompts/agent-prompt-plan-mode-enhanced.md)
- [Tracing Claude Code's LLM Traffic (George Sung)](https://medium.com/@georgesung/tracing-claude-codes-llm-traffic-agentic-loop-sub-agents-tool-use-prompts-7796941806f5)
- [The Task Tool: Claude Code's Agent Orchestration System](https://dev.to/bhaidar/the-task-tool-claude-codes-agent-orchestration-system-4bf2)
- [What Actually Is Claude Code's Plan Mode? (Armin Ronacher)](https://lucumr.pocoo.org/2025/12/17/what-is-plan-mode/)
- [How Sub-Agents Work in Claude Code](https://medium.com/@kinjal01radadiya/how-sub-agents-work-in-claude-code-a-complete-guide-bafc66bbaf70)

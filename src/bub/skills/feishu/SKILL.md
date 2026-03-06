---
name: feishu
description: |
  Feishu (Lark) Bot skill for sending, replying, and managing Feishu IM messages.
  Supports: text, interactive cards (Markdown), rich text (post), emoji reactions.
  Use when Bub needs to: (1) Send a message to a Feishu user or group chat,
  (2) Reply to a specific Feishu message, (3) React to a message with emoji,
  (4) Send rich cards with buttons or formatted content.
metadata:
  channel: feishu
---

# Feishu Skill

Agent-facing execution guide for Feishu outbound communication.

Assumption: `BUB_FEISHU_APP_ID` and `BUB_FEISHU_APP_SECRET` are already configured.

## Inbound Message Format

When receiving a Feishu message, the prompt contains JSON metadata:

```json
{
  "message": "user text content",
  "message_id": "om_xxx",
  "type": "text",
  "sender_id": "ou_xxx",
  "chat_id": "oc_xxx",
  "chat_type": "p2p",
  "create_time": 1234567890
}
```

- `chat_type`: `"p2p"` for private chat, `"group"` for group chat.
- `type`: message type — `text`, `post`, `image`, `file`, `audio`, `video`, `sticker`.
- `sender_id`: sender's open_id.
- `chat_id`: use this to send replies.
- `message_id`: use this for reply-to threading and reactions.

## Response Strategy

### Quick Questions (simple Q&A, greetings, short answers)

Send ONE reply — just a text message. Keep it fast.

### Medium Tasks (code review, explanation, search results)

1. React to the user's message with `THUMBSUP` immediately (shows you're on it)
2. Do the work
3. Send ONE structured reply — use **interactive card** for formatted output

### Complex / Long-Running Tasks (multi-step coding, refactoring, research)

1. React to the user's message with `EYES` immediately
2. Spawn a background sub-agent: `agent prompt="..." run_in_background=true`
3. Send a short text reply: "正在处理，完成后通知你"
4. When the sub-agent finishes, send a **card** with results

### CRITICAL RULES

- **ONE message per response turn.** Never send separate ack + completion text messages.
- Use **reactions** (not text) for quick acknowledgments.
- Use **cards** (interactive) when output has formatting, code, or structure.
- Use **plain text** only for short, simple replies.
- For group chats, ALWAYS use `--reply-to` to maintain thread context.

## Message Type Guide

| Type | When to Use | Markdown? |
|------|-------------|-----------|
| `text` | Short replies, plain answers | No |
| `interactive` | Formatted output, code, results, reports | Yes (lark_md) |
| `post` | Multi-section content with title | No |
| `react` | Acknowledgment, quick feedback | N/A |

### Markdown in Cards (interactive type)

Cards support **lark_md** format:
- `**bold**`, `*italic*`
- `` `inline code` ``
- `[link text](url)`
- `\n` for line breaks

Note: lark_md does NOT support multi-line code blocks (``` fences). For code, use `inline code` or send as plain text.

## Command Templates

Paths are relative to this skill directory.

```bash
# --- Text Messages ---

# Send plain text
uv run ./scripts/feishu_send.py send \
  --chat-id <CHAT_ID> \
  --message "<TEXT>"

# Reply to a specific message
uv run ./scripts/feishu_send.py send \
  --chat-id <CHAT_ID> \
  --message "<TEXT>" \
  --reply-to <MESSAGE_ID>

# --- Interactive Cards ---

# Send card with Markdown body
uv run ./scripts/feishu_send.py send \
  --chat-id <CHAT_ID> \
  --type interactive \
  --title "Card Title" \
  --message "**Bold**, *italic*, \`code\`"

# Card with action button
uv run ./scripts/feishu_send.py send \
  --chat-id <CHAT_ID> \
  --type interactive \
  --title "Deploy Complete" \
  --message "Version 1.2.3 deployed." \
  --button-text "View" \
  --button-url "https://example.com"

# --- Rich Text (Post) ---

uv run ./scripts/feishu_send.py send \
  --chat-id <CHAT_ID> \
  --type post \
  --title "Report" \
  --message "Paragraph one\nParagraph two"

# --- Reactions ---

# React to acknowledge
uv run ./scripts/feishu_send.py react \
  --message-id <MESSAGE_ID> \
  --emoji THUMBSUP
```

## Common Emoji Types

| Emoji | Meaning | Use When |
|-------|---------|----------|
| `THUMBSUP` | 👍 Got it | Acknowledging a simple request |
| `EYES` | 👀 Looking | Starting a complex task |
| `OK` | 👌 Done | Task completed successfully |
| `HEART` | ❤️ Thanks | Appreciating feedback |
| `FIRE` | 🔥 Great | Positive reaction |
| `JIAYI` | ➕ +1 | Agreement |

## Background Sub-Agent Pattern

For tasks that take more than a few seconds, use the `agent` tool to spawn a background worker:

```
agent prompt="<detailed task description>. When done, use the feishu skill to send results as a card to chat_id=<CHAT_ID>" run_in_background=true
```

The sub-agent inherits all tools including this feishu skill, so it can send results directly when finished. This keeps the main conversation responsive.

## Script Interface Reference

### `feishu_send.py send`

- `--chat-id`, `-c`: required, target chat ID
- `--message`, `-m`: required, message text (Markdown for interactive type)
- `--reply-to`, `-r`: optional, message_id to reply to
- `--type`, `-t`: optional, `text` (default), `post`, or `interactive`
- `--title`: optional, title for card or post messages
- `--button-text`: optional, button label (interactive only)
- `--button-url`: optional, button URL (interactive only)

### `feishu_send.py react`

- `--message-id`: required, message to react to
- `--emoji`: required, emoji type (e.g. THUMBSUP, HEART, EYES)

## Failure Handling

- On API errors, inspect the response code and message.
- Common errors: 230001 (bot not in chat), 230002 (no permission).
- If reply target is invalid, fall back to a normal send.

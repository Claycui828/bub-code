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

## How to Send Messages

The sending script is at `scripts/feishu_send.py` relative to this SKILL.md file.
To run it, use `cd` to the skill directory (parent of this SKILL.md), then call `uv run`:

```bash
cd <SKILL_DIR> && uv run scripts/feishu_send.py send --chat-id <CHAT_ID> --message "<TEXT>"
```

Where `<SKILL_DIR>` is the directory containing this SKILL.md (derive it from the skill location path shown in the prompt).

### Send plain text

```bash
cd <SKILL_DIR> && uv run scripts/feishu_send.py send \
  --chat-id <CHAT_ID> \
  --message "你好"
```

### Reply to a message (threading)

```bash
cd <SKILL_DIR> && uv run scripts/feishu_send.py send \
  --chat-id <CHAT_ID> \
  --message "回复内容" \
  --reply-to <MESSAGE_ID>
```

### Send interactive card (supports Markdown)

```bash
cd <SKILL_DIR> && uv run scripts/feishu_send.py send \
  --chat-id <CHAT_ID> \
  --type interactive \
  --title "标题" \
  --message "**加粗**, *斜体*, \`代码\`, [链接](url)"
```

### Send card with button

```bash
cd <SKILL_DIR> && uv run scripts/feishu_send.py send \
  --chat-id <CHAT_ID> \
  --type interactive \
  --title "部署完成" \
  --message "v1.2.3 已部署" \
  --button-text "查看" \
  --button-url "https://example.com"
```

### Send rich text (post)

```bash
cd <SKILL_DIR> && uv run scripts/feishu_send.py send \
  --chat-id <CHAT_ID> \
  --type post \
  --title "报告标题" \
  --message "第一段\n第二段"
```

### Add emoji reaction

```bash
cd <SKILL_DIR> && uv run scripts/feishu_send.py react \
  --message-id <MESSAGE_ID> \
  --emoji THUMBSUP
```

## Script Arguments Reference

### `feishu_send.py send`

| Arg | Required | Description |
|-----|----------|-------------|
| `--chat-id`, `-c` | Yes | Target chat ID (oc_xxx) |
| `--message`, `-m` | Yes | Message text. Markdown only works with `--type interactive` |
| `--type`, `-t` | No | `text` (default), `interactive`, or `post` |
| `--title` | No | Title for card or post |
| `--reply-to`, `-r` | No | Message ID to reply to (om_xxx) |
| `--button-text` | No | Button label (interactive only) |
| `--button-url` | No | Button URL (interactive only) |

### `feishu_send.py react`

| Arg | Required | Description |
|-----|----------|-------------|
| `--message-id` | Yes | Message to react to (om_xxx) |
| `--emoji` | Yes | Emoji type (see full list below) |

## Inbound Message Format

When you receive a Feishu message, the prompt contains JSON metadata:

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

Extract `chat_id` and `message_id` from this to send replies and reactions.

## Response Strategy

### Quick Questions (Q&A, greetings, lookups)

Just send ONE text reply. No reaction, no card. Be fast.

### Medium Tasks (code review, explanation, search)

1. React with `THUMBSUP` to the user's `message_id`
2. Do the work
3. Send ONE card (`--type interactive`) with the result

### Complex / Long-Running Tasks (coding, refactoring, research, multi-step)

**IMPORTANT: Always use `run_in_background=true` for agent tool calls in channel conversations.**

The Feishu channel is real-time — the user is waiting. You MUST stay responsive:

1. React with `EYES` to the user's `message_id`
2. Send a short text: "正在处理，完成后通知你"
3. Spawn background agent with ALL context it needs:

```
agent prompt="<FULL task description with all file paths and context>.

When finished, send the result to Feishu:
cd <SKILL_DIR> && uv run scripts/feishu_send.py send --chat-id <CHAT_ID> --type interactive --title '任务完成' --message '<result summary>'

If failed, send error:
cd <SKILL_DIR> && uv run scripts/feishu_send.py send --chat-id <CHAT_ID> --message '任务失败: <error>'" run_in_background=true
```

4. End your turn immediately — do NOT wait for the background agent

### When to use `run_in_background=true`

In Feishu channel conversations, **default to background** for any agent call:
- Any task requiring more than 2-3 tool calls → background
- File modifications, code generation → background
- Research, multi-file analysis → background
- Only use foreground agent for trivial, instant lookups (< 5 seconds)

The background agent has full tool access and can send results to Feishu when done.

## CRITICAL RULES

1. **ONE message per response turn.** Never send ack text + completion text separately.
2. **Reactions for ack, cards for results.** Don't waste a message on "收到".
3. **Cards for anything with formatting.** Code, tables, structured data → `--type interactive`.
4. **Plain text for short replies only.** Under ~50 chars, no formatting needed.
5. **Group chats: ALWAYS `--reply-to`** to maintain thread context.
6. **Background agents in channel.** Don't block the conversation with long-running foreground agents.
7. **Self-contained background prompts.** Include chat_id, skill_dir path, and full task context in the agent prompt — the sub-agent has NO access to your conversation.

## Message Type Quick Reference

| Type | Use For | Markdown? |
|------|---------|-----------|
| `text` | Short plain replies | No |
| `interactive` | Formatted results, code, reports | Yes (lark_md: **bold**, *italic*, \`code\`, [link](url)) |
| `post` | Multi-paragraph with title | No |
| `react` | Quick ack/feedback | N/A |

Note: lark_md does NOT support ``` code fences. Use \`inline code\` or send code as plain text.

## Available Emoji Types

### Recommended for Agent Use

| Emoji | Type | When to Use |
|-------|------|-------------|
| 👍 | `THUMBSUP` | Acknowledged, will do |
| 👌 | `OK` | Simple confirmation |
| 👀 | `GLANCE` | Looking into it |
| 💪 | `MUSCLE` | Working on it |
| ✅ | `DONE` | Task completed |
| ❌ | `CrossMark` | Failed / rejected |
| 🔥 | `Fire` | Great, impressive |
| 🎉 | `PARTY` | Celebration, success |
| 👏 | `CLAP` | Well done |
| 🤝 | `FISTBUMP` | Agreement, deal |
| ❤️ | `HEART` | Appreciation |
| 🚀 | `STRIVE` | Let's go, pushing forward |
| 💯 | `Hundred` | Perfect, 100% |
| 🏆 | `Trophy` | Achievement |
| ⏰ | `Alarm` | Reminder, time-sensitive |
| 📌 | `Pin` | Important, pinned |
| 🔔 | `Loudspeaker` | Announcement |
| ➕ | `JIAYI` | +1, agree |
| ➖ | `MinusOne` | -1, disagree |
| 💡 | `StatusFlashOfInspiration` | Idea, insight |
| 🤔 | `THINKING` | Considering |
| 😅 | `SWEAT` | Awkward, oops |
| 🙏 | `THANKS` | Thank you |
| 👋 | `WAVE` | Hello / goodbye |
| 🫡 | `SALUTE` | Roger that |

### Full List (all valid types)

**Gestures:** `THUMBSUP`, `ThumbsDown`, `OK`, `THANKS`, `MUSCLE`, `FINGERHEART`, `APPLAUSE`, `FISTBUMP`, `JIAYI`, `CLAP`, `PRAISE`, `WAVE`, `HIGHFIVE`, `SHAKE`, `SALUTE`, `SLIGHT`

**Faces — Positive:** `SMILE`, `BLUSH`, `LAUGH`, `SMIRK`, `LOL`, `LOVE`, `WINK`, `PROUD`, `JOYFUL`, `WOW`, `YEAH`, `KISS`, `SMOOCH`, `DROOL`, `OBSESSED`, `HUG`, `BeamingFace`, `Delighted`, `Partying`, `ThanksFace`, `SaluteFace`

**Faces — Neutral:** `THINKING`, `WITTY`, `SMART`, `FACEPALM`, `INNOCENTSMILE`, `CHUCKLE`, `SHY`, `DULL`, `EYESCLOSED`, `SILENT`, `Shrug`, `ClownFace`, `FullMoonFace`

**Faces — Negative:** `SCOWL`, `SOB`, `CRY`, `ANGRY`, `TEARS`, `EMBARRASSED`, `WHIMPER`, `WRONGED`, `WAIL`, `BLUBBER`, `FROWN`, `CRAZY`, `DIZZY`, `LOOKDOWN`, `SWEAT`, `SPEECHLESS`, `SICK`, `PUKE`, `TERROR`, `PETRIFIED`, `SHOCKED`, `SKULL`, `ColdSweat`

**Actions:** `TEASE`, `SHOWOFF`, `COMFORT`, `TRICK`, `ENOUGH`, `MONEY`, `NOSEPICK`, `HAUGHTY`, `SLAP`, `SPITBLOOD`, `TOASTED`, `GLANCE`, `SHHH`, `SMUG`, `HAMMER`, `BETRAYED`, `SLEEP`, `DROWSY`, `YAWN`, `STRIVE`, `XBLUSH`, `WHAT`, `RoarForYou`

**Objects:** `HEART`, `HEARTBROKEN`, `ROSE`, `LIPS`, `BEER`, `CAKE`, `GIFT`, `Coffee`, `BubbleTea`, `Drumstick`, `Pepper`, `CUCUMBER`, `CANDIEDHAWS`, `Lemon`, `BOMB`, `POOP`, `CLEAVER`, `Soccer`, `Basketball`

**Symbols:** `DONE`, `CrossMark`, `CheckMark`, `Yes`, `No`, `Hundred`, `MinusOne`, `OKR`, `LGTM`, `Pin`, `Alarm`, `Loudspeaker`, `Trophy`, `Fire`, `Music`, `FIREWORKS`, `REDPACKET`, `18X`

**Status:** `GeneralDoNotDisturb`, `GeneralInMeetingBusy`, `StatusReading`, `GeneralBusinessTrip`, `GeneralWorkFromHome`, `StatusEnjoyLife`, `GeneralSun`, `GeneralMoonRest`, `StatusFlashOfInspiration`

**Animals & Seasonal:** `HUSKY`, `CALF`, `BEAR`, `BULL`, `RAINBOWPUKE`, `MoonRabbit`, `Mooncake`, `HappyDragon`, `Snowman`, `XmasTree`, `XmasHat`, `Pumpkin`, `StickyRiceBalls`, `FIRECRACKER`, `FORTUNE`, `LUCK`

**Special:** `PARTY`, `HEADSET`, `VRHeadset`, `TV`, `Movie`, `EatingFood`, `MeMeMe`, `Sigh`, `Typing`, `Get`, `OnIt`, `OneSecond`, `YouAreTheBest`, `GoGoGo`, `UPPERLEFT`

## Error Handling

- API errors: check response code and message
- 230001: bot not in chat — ask user to add bot
- 230002: no permission — check app permissions
- Reply target invalid: script auto-falls back to normal send

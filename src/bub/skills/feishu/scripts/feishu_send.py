#!/usr/bin/env uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "requests>=2.31.0",
# ]
# ///

"""
Feishu Bot Message Sender

Send messages, cards, reactions, and rich text via Feishu Open API.
Supports: text, interactive (card), post (rich text), reaction.
"""

import argparse
import json
import os
import sys

import requests

BASE_URL = "https://open.feishu.cn/open-apis"


def get_tenant_token(app_id: str, app_secret: str) -> str:
    """Get tenant access token from Feishu API."""
    resp = requests.post(
        f"{BASE_URL}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        print(f"Failed to get token: {data.get('msg')}")
        sys.exit(1)
    return data["tenant_access_token"]


def make_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }


def send_message(token: str, chat_id: str, msg_type: str, content: str, reply_to: str | None = None) -> dict:
    """Send a message to a Feishu chat."""
    url = f"{BASE_URL}/im/v1/messages"
    params = {"receive_id_type": "chat_id"}
    payload = {
        "receive_id": chat_id,
        "msg_type": msg_type,
        "content": content,
    }
    headers = make_headers(token)

    if reply_to:
        url = f"{BASE_URL}/im/v1/messages/{reply_to}/reply"
        payload.pop("receive_id", None)
        params = {}

    resp = requests.post(url, params=params, json=payload, headers=headers, timeout=30)

    # If reply fails, fall back to normal send
    if reply_to and resp.status_code != 200:
        print(f"Reply failed ({resp.status_code}), falling back to normal send")
        url = f"{BASE_URL}/im/v1/messages"
        payload["receive_id"] = chat_id
        params = {"receive_id_type": "chat_id"}
        resp = requests.post(url, params=params, json=payload, headers=headers, timeout=30)

    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        print(f"API error: code={result.get('code')} msg={result.get('msg')}")
        sys.exit(1)
    return result


def add_reaction(token: str, message_id: str, emoji_type: str) -> dict:
    """Add an emoji reaction to a message."""
    url = f"{BASE_URL}/im/v1/messages/{message_id}/reactions"
    payload = {"reaction_type": {"emoji_type": emoji_type}}
    resp = requests.post(url, json=payload, headers=make_headers(token), timeout=10)
    resp.raise_for_status()
    result = resp.json()
    if result.get("code") != 0:
        print(f"API error: code={result.get('code')} msg={result.get('msg')}")
        sys.exit(1)
    return result


def build_text_content(text: str) -> str:
    return json.dumps({"text": text})


def build_post_content(title: str, text: str) -> str:
    """Build rich text (post) content. Splits text by newlines into paragraphs."""
    paragraphs = []
    for line in text.split("\n"):
        paragraphs.append([{"tag": "text", "text": line}])
    content = {"zh_cn": {"title": title, "content": paragraphs}}
    return json.dumps(content)


def build_card_content(title: str, text: str, button_text: str | None = None, button_url: str | None = None) -> str:
    """Build an interactive card message."""
    elements = [{"tag": "div", "text": {"tag": "lark_md", "content": text}}]
    if button_text and button_url:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": button_text},
                "type": "primary",
                "url": button_url,
            }],
        })
    card = {
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "blue"},
        "elements": elements,
    }
    return json.dumps(card)


def main():
    # Auto-insert "send" subcommand for backwards compatibility with old CLI format:
    # feishu_send.py --chat-id ... --message ... → feishu_send.py send --chat-id ... --message ...
    if len(sys.argv) > 1 and sys.argv[1].startswith("-"):
        sys.argv.insert(1, "send")

    parser = argparse.ArgumentParser(description="Send messages via Feishu Bot API")
    sub = parser.add_subparsers(dest="command", required=True)

    # --- send (text, post, card) ---
    p_send = sub.add_parser("send", help="Send a message")
    p_send.add_argument("--chat-id", "-c", required=True, help="Target chat ID (oc_xxx)")
    p_send.add_argument("--message", "-m", required=True, help="Message text")
    p_send.add_argument("--reply-to", "-r", help="Message ID to reply to (om_xxx)")
    p_send.add_argument(
        "--type", "-t", default="text", choices=["text", "post", "interactive"],
        help="Message type (default: text)",
    )
    p_send.add_argument("--title", help="Title for post or card messages")
    p_send.add_argument("--button-text", help="Button label (card only)")
    p_send.add_argument("--button-url", help="Button URL (card only)")

    # --- react ---
    p_react = sub.add_parser("react", help="Add emoji reaction to a message")
    p_react.add_argument("--message-id", required=True, help="Message ID to react to (om_xxx)")
    p_react.add_argument("--emoji", required=True, help="Emoji type, e.g. THUMBSUP, HEART, SMILE, OK, JIAYI")

    # --- common ---
    for p in [p_send, p_react]:
        p.add_argument("--app-id", help="Feishu App ID (defaults to BUB_FEISHU_APP_ID)")
        p.add_argument("--app-secret", help="Feishu App Secret (defaults to BUB_FEISHU_APP_SECRET)")

    args = parser.parse_args()

    app_id = args.app_id or os.environ.get("BUB_FEISHU_APP_ID")
    app_secret = args.app_secret or os.environ.get("BUB_FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        print("Error: App ID and App Secret required. Set BUB_FEISHU_APP_ID and BUB_FEISHU_APP_SECRET env vars.")
        sys.exit(1)

    token = get_tenant_token(app_id, app_secret)

    try:
        if args.command == "react":
            add_reaction(token, args.message_id, args.emoji)
            print(f"Reaction {args.emoji} added to {args.message_id}")

        elif args.command == "send":
            text = args.message.replace("\\n", "\n")
            msg_type = args.type
            if msg_type == "text":
                content = build_text_content(text)
            elif msg_type == "post":
                content = build_post_content(args.title or "", text)
            elif msg_type == "interactive":
                content = build_card_content(args.title or "Notice", text, args.button_text, args.button_url)
            else:
                print(f"Unknown type: {msg_type}")
                sys.exit(1)

            send_message(token, args.chat_id, msg_type, content, getattr(args, "reply_to", None))
            mode = "replied" if getattr(args, "reply_to", None) else "sent"
            print(f"{msg_type} message {mode} successfully to {args.chat_id}")

    except requests.HTTPError as e:
        print(f"HTTP Error: {e}")
        print(f"Response: {e.response.text}")
        sys.exit(1)


if __name__ == "__main__":
    main()

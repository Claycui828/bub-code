"""Feishu (Lark) channel adapter using WebSocket long connection."""

from __future__ import annotations

import asyncio
import json
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from loguru import logger

from bub.app.runtime import AppRuntime
from bub.channels.base import BaseChannel, exclude_none
from bub.core.agent_loop import LoopResult

NO_ACCESS_MESSAGE = "You are not allowed to chat with me. Please deploy your own instance of Bub."


@dataclass(frozen=True)
class FeishuConfig:
    """Feishu adapter config."""

    app_id: str
    app_secret: str
    allow_from: set[str]
    allow_chats: set[str]


class FeishuChannel(BaseChannel["_FeishuEvent"]):
    """Feishu adapter using WebSocket long connection via lark-oapi SDK."""

    name = "feishu"

    def __init__(self, runtime: AppRuntime) -> None:
        super().__init__(runtime)
        settings = runtime.settings
        assert settings.feishu_app_id is not None  # noqa: S101
        assert settings.feishu_app_secret is not None  # noqa: S101
        self._config = FeishuConfig(
            app_id=settings.feishu_app_id,
            app_secret=settings.feishu_app_secret,
            allow_from=set(settings.feishu_allow_from),
            allow_chats=set(settings.feishu_allow_chats),
        )
        self._api_client: Any = None
        self._ws_client: Any = None
        self._on_receive: Callable[[_FeishuEvent], Awaitable[None]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._bot_open_id: str | None = None

    def is_mentioned(self, message: _FeishuEvent) -> bool:
        chat_type = message.chat_type
        if chat_type == "p2p":
            return True
        # Group chat: check if bot is mentioned or "bub" in text
        if self._bot_open_id and message.mentions:
            for m in message.mentions:
                if m.get("open_id") == self._bot_open_id:
                    return True
        text = message.text_content.lower()
        return "bub" in text

    async def start(self, on_receive: Callable[[_FeishuEvent], Awaitable[None]]) -> None:
        import lark_oapi as lark

        # Ensure SSL certificates are available (fixes macOS SSL errors with lark-oapi WS)
        if "SSL_CERT_FILE" not in os.environ:
            try:
                import certifi

                os.environ["SSL_CERT_FILE"] = certifi.where()
            except ImportError:
                pass

        self._on_receive = on_receive
        self._loop = asyncio.get_running_loop()

        # Build API client for sending messages
        self._api_client = (
            lark.Client.builder()
            .app_id(self._config.app_id)
            .app_secret(self._config.app_secret)
            .build()
        )

        # Fetch bot info to get open_id
        await self._fetch_bot_info()

        # Build event handler
        handler = (
            lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(self._on_feishu_message)
            .build()
        )

        logger.info(
            "feishu.start allow_from_count={} allow_chats_count={}",
            len(self._config.allow_from),
            len(self._config.allow_chats),
        )

        # The lark-oapi WS client uses a module-level event loop with run_until_complete(),
        # so it must run in a dedicated thread with its own event loop.
        def _run_ws() -> None:
            import lark_oapi.ws.client as ws_module

            ws_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(ws_loop)
            ws_module.loop = ws_loop  # override the module-level loop for this thread

            ws_client = lark.ws.Client(
                app_id=self._config.app_id,
                app_secret=self._config.app_secret,
                event_handler=handler,
                log_level=lark.LogLevel.INFO,
            )
            self._ws_client = ws_client
            ws_client.start()

        ws_thread = threading.Thread(target=_run_ws, daemon=True, name="feishu-ws")
        ws_thread.start()

        try:
            await asyncio.Event().wait()
        finally:
            logger.info("feishu.stopped")

    async def get_session_prompt(self, message: _FeishuEvent) -> tuple[str, str]:
        session_id = f"{self.name}:{message.chat_id}"

        # Strip @mention placeholders from text
        text = message.text_content
        if message.mentions:
            for m in message.mentions:
                key = m.get("key", "")
                if key:
                    text = text.replace(key, "").strip()

        # Pass comma commands directly
        if text.strip().startswith(","):
            return session_id, text

        metadata: dict[str, Any] = exclude_none({
            "message_id": message.message_id,
            "type": message.message_type,
            "sender_id": message.sender_open_id,
            "chat_id": message.chat_id,
            "chat_type": message.chat_type,
            "create_time": message.create_time,
        })

        metadata_json = json.dumps({"message": text, **metadata}, ensure_ascii=False)
        return session_id, metadata_json

    async def process_output(self, session_id: str, output: LoopResult) -> None:
        # The feishu skill handles sending assistant_output proactively via feishu_send.py,
        # so we only send immediate_output (router responses) and errors here.
        send_back_text = [output.immediate_output] if output.immediate_output else []
        if output.error:
            send_back_text.append(f"Error: {output.error}")

        content = "\n\n".join(send_back_text).strip()
        if not content:
            return

        chat_id = session_id.split(":", 1)[1]
        logger.info("feishu.outbound session_id={} len={}", session_id, len(content))
        await self._send_text(chat_id, content)

    # ---- internal helpers ----

    async def _fetch_bot_info(self) -> None:
        """Fetch the bot's open_id for mention detection via /bot/v3/info."""
        import requests

        try:
            token_resp = await asyncio.to_thread(
                requests.post,
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._config.app_id, "app_secret": self._config.app_secret},
                timeout=10,
            )
            token = token_resp.json().get("tenant_access_token", "")
            if not token:
                logger.warning("feishu.bot_info failed to get tenant token")
                return
            bot_resp = await asyncio.to_thread(
                requests.get,
                "https://open.feishu.cn/open-apis/bot/v3/info",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            bot_data = bot_resp.json().get("bot", {})
            self._bot_open_id = bot_data.get("open_id")
            logger.info("feishu.bot_info open_id={} name={}", self._bot_open_id, bot_data.get("app_name"))
        except Exception:
            logger.opt(exception=True).warning("feishu.bot_info.error")

    def _on_feishu_message(self, event: Any) -> None:
        """Callback from lark-oapi SDK (runs in SDK thread)."""
        try:
            feishu_event = self._parse_event(event)
            if feishu_event is None:
                return
            # Fire-and-forget into asyncio loop — do NOT block the SDK thread,
            # otherwise the SDK thinks delivery failed and redelivers the event.
            if self._on_receive and self._loop:
                asyncio.run_coroutine_threadsafe(self._on_receive(feishu_event), self._loop)
        except Exception:
            logger.opt(exception=True).error("feishu.on_message.error")

    def _parse_event(self, event: Any) -> _FeishuEvent | None:
        """Parse a raw SDK event into a _FeishuEvent, or None if filtered."""
        data = event.event
        if data is None:
            return None

        msg = data.message
        sender = data.sender
        if msg is None or sender is None:
            return None

        chat_id = msg.chat_id or ""
        sender_open_id = sender.sender_id.open_id or "" if sender.sender_id else ""

        # Access control
        if self._config.allow_chats and chat_id not in self._config.allow_chats:
            logger.debug("feishu.filtered chat_id={}", chat_id)
            return None
        if self._config.allow_from and sender_open_id not in self._config.allow_from:
            logger.debug("feishu.filtered sender={}", sender_open_id)
            return None

        mentions = _parse_mentions(msg.mentions)
        text_content = _parse_content(msg.message_type or "text", msg.content or "{}")

        return _FeishuEvent(
            message_id=msg.message_id or "",
            message_type=msg.message_type or "unknown",
            chat_id=chat_id,
            chat_type=msg.chat_type or "",
            text_content=text_content,
            content_raw=msg.content or "{}",
            sender_open_id=sender_open_id,
            create_time=msg.create_time,
            mentions=mentions,
            thread_id=msg.thread_id or "",
        )

    async def _send_text(self, chat_id: str, text: str) -> None:
        """Send a text message to a chat."""
        from lark_oapi.api.im.v1 import (
            CreateMessageRequest,
            CreateMessageRequestBody,
        )

        body = (
            CreateMessageRequestBody.builder()
            .receive_id(chat_id)
            .msg_type("text")
            .content(json.dumps({"text": text}))
            .build()
        )
        request = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(body)
            .build()
        )

        try:
            response = await asyncio.to_thread(self._api_client.im.v1.message.create, request)
            if not response.success():
                logger.error("feishu.send failed code={} msg={}", response.code, response.msg)
        except Exception:
            logger.opt(exception=True).error("feishu.send.error chat_id={}", chat_id)


@dataclass
class _FeishuEvent:
    """Simplified Feishu message event for channel processing."""

    message_id: str
    message_type: str
    chat_id: str
    chat_type: str  # "p2p" or "group"
    text_content: str
    content_raw: str
    sender_open_id: str
    create_time: int | None
    mentions: list[dict[str, str]]
    thread_id: str = ""


def _parse_mentions(raw_mentions: Any) -> list[dict[str, str]]:
    """Extract mention info from SDK mention objects."""
    if not raw_mentions:
        return []
    result: list[dict[str, str]] = []
    for m in raw_mentions:
        info: dict[str, str] = {"key": m.key or "", "name": m.name or ""}
        if m.id and m.id.open_id:
            info["open_id"] = m.id.open_id
        result.append(info)
    return result


def _parse_content(msg_type: str, content_json: str) -> str:
    """Parse Feishu message content JSON to plain text."""
    try:
        data = json.loads(content_json)
    except (json.JSONDecodeError, TypeError):
        return content_json

    if msg_type == "text":
        return data.get("text", "")

    if msg_type == "post":
        return _flatten_post(data)

    if msg_type == "image":
        return "[Image message]"

    if msg_type == "file":
        file_name = data.get("file_name", "unknown")
        return f"[File: {file_name}]"

    if msg_type == "audio":
        duration = data.get("duration", "?")
        return f"[Audio: {duration}ms]"

    if msg_type == "video":
        return "[Video message]"

    if msg_type == "sticker":
        return "[Sticker]"

    if msg_type == "interactive":
        return "[Interactive card]"

    return f"[{msg_type} message]"


_POST_NODE_EXTRACTORS: dict[str, Callable[[dict[str, Any]], str]] = {
    "text": lambda n: n.get("text", ""),
    "a": lambda n: n.get("text", n.get("href", "")),
    "at": lambda n: n.get("user_name", "@someone"),
    "img": lambda _: "[image]",
    "media": lambda _: "[media]",
}


def _flatten_post(data: dict[str, Any]) -> str:
    """Flatten Feishu rich text (post) to plain text."""
    title = data.get("title", "")
    lines: list[str] = [title] if title else []
    for paragraph in data.get("content", []):
        if not isinstance(paragraph, list):
            continue
        parts = [
            _POST_NODE_EXTRACTORS[node.get("tag", "")](node)
            for node in paragraph
            if isinstance(node, dict) and node.get("tag", "") in _POST_NODE_EXTRACTORS
        ]
        if parts:
            lines.append("".join(parts))
    return "\n".join(lines)

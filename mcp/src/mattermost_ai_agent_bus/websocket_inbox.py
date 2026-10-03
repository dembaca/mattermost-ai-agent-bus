"""WebSocket inbox: classify posts and wait for relevant events."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect as ws_connect

from .client import MattermostClient, ws_url_from_http

log = logging.getLogger(__name__)


@dataclass
class ClassifiedEvent:
    kind: str  # mention | dm | watched_channel
    post: dict[str, Any]
    channel: dict[str, Any] | None = None
    raw: dict[str, Any] | None = None


def classify_post(
    post: dict[str, Any],
    *,
    bot_user_id: str,
    bot_username: str,
    channel_type: str | None = None,
    watched_channel_ids: set[str] | frozenset[str] | None = None,
) -> str | None:
    """Return event kind if the post is relevant for this bot, else None.

    Relevance (first match wins):
      - mention: message contains <@bot_user_id> or @bot_username
      - dm: channel type is D
      - watched_channel: post.channel_id is in watched_channel_ids
    Own posts are ignored.
    """
    if not post:
        return None
    if post.get("user_id") == bot_user_id:
        return None
    message = post.get("message") or ""
    uid = bot_user_id or ""
    uname = (bot_username or "").lstrip("@")
    if uid and f"<@{uid}>" in message:
        return "mention"
    if uname:
        pattern = rf"(?<![\w-])@{re.escape(uname)}(?![\w-])"
        if re.search(pattern, message, re.I):
            return "mention"
    ctype = (channel_type or "").upper()
    if ctype == "D":
        return "dm"
    watched = watched_channel_ids or set()
    cid = post.get("channel_id")
    if cid and cid in watched:
        return "watched_channel"
    return None


@dataclass
class WebSocketInbox:
    client: MattermostClient
    watched_channel_ids: set[str] = field(default_factory=set)
    _ws: Any = field(default=None, repr=False, init=False)
    _seq: int = field(default=1, repr=False, init=False)
    _queue: asyncio.Queue[ClassifiedEvent] = field(
        default_factory=asyncio.Queue, repr=False, init=False
    )
    _reader: asyncio.Task[None] | None = field(default=None, repr=False, init=False)
    _channel_cache: dict[str, dict[str, Any]] = field(
        default_factory=dict, repr=False, init=False
    )
    _bot_user_id: str = field(default="", repr=False, init=False)
    _bot_username: str = field(default="", repr=False, init=False)

    async def ensure_identity(self) -> None:
        me = await self.client.get_me()
        self._bot_user_id = me["id"]
        self._bot_username = me.get("username") or ""
        if not self.watched_channel_ids:
            try:
                cid = await self.client.get_channel_id()
                self.watched_channel_ids.add(cid)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not resolve default watched channel: %s", exc)

    async def _channel(self, channel_id: str) -> dict[str, Any]:
        if channel_id not in self._channel_cache:
            self._channel_cache[channel_id] = await self.client.get_channel(channel_id)
        return self._channel_cache[channel_id]

    async def connect(self) -> None:
        await self.ensure_identity()
        if not self.client.token:
            raise RuntimeError("bot token required for WebSocket")
        url = ws_url_from_http(self.client.base_url)
        self._ws = await ws_connect(url)
        auth = {
            "seq": self._seq,
            "action": "authentication_challenge",
            "data": {"token": self.client.token},
        }
        self._seq += 1
        await self._ws.send(json.dumps(auth))
        deadline = asyncio.get_event_loop().time() + 15
        while asyncio.get_event_loop().time() < deadline:
            raw = await asyncio.wait_for(self._ws.recv(), timeout=15)
            msg = json.loads(raw)
            event = msg.get("event")
            if event in ("hello", "authorized") or msg.get("status") == "OK":
                break
        self._reader = asyncio.create_task(self._read_loop())

    async def close(self) -> None:
        if self._reader:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def _read_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if msg.get("event") != "posted":
                    continue
                data = msg.get("data") or {}
                post_raw = data.get("post")
                if isinstance(post_raw, str):
                    try:
                        post = json.loads(post_raw)
                    except json.JSONDecodeError:
                        continue
                elif isinstance(post_raw, dict):
                    post = post_raw
                else:
                    continue
                channel_type = data.get("channel_type")
                channel = None
                cid = post.get("channel_id")
                if cid and not channel_type:
                    try:
                        channel = await self._channel(cid)
                        channel_type = channel.get("type")
                    except Exception:  # noqa: BLE001
                        channel = None
                kind = classify_post(
                    post,
                    bot_user_id=self._bot_user_id,
                    bot_username=self._bot_username,
                    channel_type=channel_type,
                    watched_channel_ids=self.watched_channel_ids,
                )
                if not kind:
                    continue
                await self._queue.put(
                    ClassifiedEvent(
                        kind=kind, post=post, channel=channel, raw=msg
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("websocket reader stopped: %s", exc)

    async def wait_for_events(
        self,
        *,
        timeout_sec: float = 60.0,
        max_events: int = 10,
    ) -> list[ClassifiedEvent]:
        """Block until at least one classified event or timeout."""
        if self._ws is None:
            await self.connect()
        events: list[ClassifiedEvent] = []
        try:
            first = await asyncio.wait_for(self._queue.get(), timeout=timeout_sec)
            events.append(first)
        except asyncio.TimeoutError:
            return []
        while len(events) < max_events:
            try:
                events.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return events

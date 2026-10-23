"""WebSocket inbox: classify posts and wait for relevant events."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect as ws_connect

from . import asks, links
from .client import MattermostClient, ws_url_from_http

log = logging.getLogger(__name__)


@dataclass
class ClassifiedEvent:
    kind: str  # mention | dm | watched_channel | reaction (to an open ask)
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
    # When the bot was created (ms). Nothing older can be for this session, so
    # backfill never reads before it, even where Mattermost reports a channel
    # as never viewed (last_viewed_at 0) right after the bot joined it.
    _since_floor: int = field(default=0, repr=False, init=False)
    # Events still queued after the last wait_for_events returned max_events.
    pending: int = field(default=0, repr=False, init=False)
    # Post ids already queued. Backfill and the live stream overlap around the
    # moment the socket opens, so without this a message can be delivered twice.
    _seen_ids: set[str] = field(default_factory=set, repr=False, init=False)
    # The queue read of the wait_for_events call in progress. A runtime that
    # moved an earlier call to the background (Claude Code does after ~120 s)
    # leaves it pending; the newest call takes over, so a forgotten listener
    # cannot swallow events the agent is waiting for in the foreground.
    _getter: asyncio.Future[ClassifiedEvent] | None = field(
        default=None, repr=False, init=False
    )
    # Typing indicator (RT-6.5): after a mention or DM is delivered, show the bot
    # as typing in that thread until it answers, waits again, or typing_sec ends.
    # Mattermost drops the indicator after a few seconds, so it is re-sent.
    typing_sec: float = 60.0
    typing_interval: float = 4.0
    _typing: asyncio.Task[None] | None = field(default=None, repr=False, init=False)

    async def ensure_identity(self) -> None:
        me = await self.client.get_me()
        self._bot_user_id = me["id"]
        self._bot_username = me.get("username") or ""
        self._since_floor = int(me.get("create_at") or 0)
        if not self.watched_channel_ids:
            # Watch the default channel (where the agent stays addressable) and the
            # project channel (where it works) — either may be where it is called.
            names = [self.client.channel_name]
            if self.client.project_channel_name:
                names.append(self.client.project_channel_name)
            for name in names:
                try:
                    cid = await self.client.get_channel_id(name)
                    self.watched_channel_ids.add(cid)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not resolve watched channel %r: %s", name, exc)

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
        await self._backfill()

    async def _backfill(self) -> None:
        """Queue anything that arrived before the socket existed.

        The WebSocket is connected lazily on the first wait_for_events and only
        streams from that moment, so a message sent earlier in the session was
        never seen by the reader — permanently invisible to the queue, while
        the REST counters still reported it. That mismatch is what made the Stop
        hook block on a message nothing could deliver.
        """
        try:
            pending = await self.client.unread_channels()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not look for missed messages: %s", exc)
            return
        for entry in pending:
            cid = entry["channel_id"]
            # A fresh bot's channels can report last_viewed_at 0: read from the
            # bot's creation then, not the channel's last 50 posts of history.
            since = max(entry.get("last_viewed_at") or 0, self._since_floor)
            try:
                posts = await self.client.list_recent(
                    channel_id=cid,
                    per_page=50,
                    since_ms=since or None,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("could not backfill channel %s: %s", cid, exc)
                continue
            for post in posts:
                kind = classify_post(
                    post,
                    bot_user_id=self._bot_user_id,
                    bot_username=self._bot_username,
                    channel_type=entry.get("type"),
                    watched_channel_ids=self.watched_channel_ids,
                )
                if not kind:
                    continue
                if post.get("id") in self._seen_ids:
                    continue
                self._seen_ids.add(post["id"])
                await self._queue.put(ClassifiedEvent(kind=kind, post=post))
        # Reactions to open asks given while nothing was listening.
        for pid in asks.open_asks(self._bot_user_id):
            try:
                found = await self.client.reactions(pid)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not read reactions to ask %s: %s", pid, exc)
                continue
            for reaction in found:
                await self._queue_reaction(reaction, "")

    def start_typing(self, channel_id: str, parent_id: str = "") -> None:
        """Show the bot as typing in channel_id (thread parent_id) for a while."""
        self.stop_typing()
        if self.typing_sec <= 0 or not channel_id or self._ws is None:
            return
        self._typing = asyncio.create_task(self._typing_loop(channel_id, parent_id))

    def stop_typing(self) -> None:
        if self._typing is not None and not self._typing.done():
            self._typing.cancel()
        self._typing = None

    async def _typing_loop(self, channel_id: str, parent_id: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.typing_sec
        # The webapp keys the indicator by channel + thread: the channel view
        # shows only parent "" and the thread view only its own root. Send both,
        # so the sender sees it wherever they are looking.
        parents = [parent_id, ""] if parent_id else [""]
        while loop.time() < deadline:
            ws = self._ws
            if ws is None:
                return
            try:
                for parent in parents:
                    data = {"channel_id": channel_id, "parent_id": parent}
                    msg = {"action": "user_typing", "seq": self._seq, "data": data}
                    self._seq += 1
                    await ws.send(json.dumps(msg))
            except Exception as exc:  # noqa: BLE001
                # Cosmetic: never let the indicator disturb delivery.
                log.debug("typing indicator stopped: %s", exc)
                return
            await asyncio.sleep(self.typing_interval)

    async def close(self) -> None:
        self.stop_typing()
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
                if msg.get("event") == "reaction_added":
                    await self._on_reaction_event(msg)
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
                links.plain_post(post)  # RT-6.15: read links as bare references
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
                pid = post.get("id")
                if pid:
                    if pid in self._seen_ids:
                        continue
                    self._seen_ids.add(pid)
                await self._queue.put(
                    ClassifiedEvent(
                        kind=kind, post=post, channel=channel, raw=msg
                    )
                )
        except asyncio.CancelledError:
            # close() cancels us and owns the socket from here on.
            raise
        except Exception as exc:  # noqa: BLE001
            log.error("websocket reader stopped: %s", exc)
        # Reached on a dropped connection or a clean server-side close. Drop the
        # dead socket so the next wait_for_events reconnects: otherwise _ws stays
        # set, nothing feeds the queue, and every later call times out silently —
        # the agent goes deaf with no error anywhere.
        self._ws = None
        self._reader = None

    async def _on_reaction_event(self, msg: dict[str, Any]) -> None:
        raw = (msg.get("data") or {}).get("reaction")
        try:
            reaction = json.loads(raw) if isinstance(raw, str) else (raw or {})
        except json.JSONDecodeError:
            return
        cid = (msg.get("broadcast") or {}).get("channel_id", "")
        await self._queue_reaction(reaction, cid)

    async def _queue_reaction(self, reaction: dict[str, Any], channel_id: str) -> None:
        """Queue a reaction to one of this bot's open asks (RT-6.11).

        Mattermost raises no unread counter for a reaction, and nothing but an
        ask makes one meaningful: a 👍 on any other post stays a remark.
        """
        pid = reaction.get("post_id") or ""
        user = reaction.get("user_id") or ""
        emoji = reaction.get("emoji_name") or ""
        if not pid or not emoji or user == self._bot_user_id:
            return
        if not asks.is_open(self._bot_user_id, pid):
            return
        key = asks.seen_key(pid, user, emoji)
        if key in self._seen_ids or asks.was_seen(self._bot_user_id, key):
            return
        self._seen_ids.add(key)
        post = {"id": key, "root_id": pid, "channel_id": channel_id, "user_id": user,
                "create_at": reaction.get("create_at") or 0, "message": f":{emoji}:"}
        await self._queue.put(ClassifiedEvent(
            kind="reaction", post=post, raw={"emoji": emoji, "ask": pid}))

    def holds(self, channel_id: str) -> bool | None:
        """Whether events of this channel wait in the queue; None while the
        socket is not connected, so nothing is known about what arrived."""
        if self._ws is None:
            return None
        return any(e.post.get("channel_id") == channel_id for e in list(self._queue._queue))

    async def wait_for_events(
        self,
        *,
        timeout_sec: float = 60.0,
        max_events: int = 10,
        agent: bool = True,
    ) -> list[ClassifiedEvent]:
        """Block until at least one classified event or timeout.

        A newer call supersedes one still waiting: the older returns [].
        A delivered mention or DM starts the typing indicator in its thread.
        agent=False is the channel pusher (RT-6.12): its next read says nothing
        about whether the agent has answered yet.
        """
        # Waiting again means the agent is done with what it was given.
        if agent:
            self.stop_typing()
        if self._ws is None:
            await self.connect()
        if self._getter is not None and not self._getter.done():
            self._getter.cancel()
        getter = asyncio.ensure_future(self._queue.get())
        self._getter = getter
        try:
            done, _ = await asyncio.wait({getter}, timeout=timeout_sec)
        finally:
            if not getter.done():
                getter.cancel()
        if getter not in done or getter.cancelled():
            # Timed out, or superseded by a newer call.
            return []
        queued: list[ClassifiedEvent] = [getter.result()]
        while True:
            try:
                queued.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        # Mentions and DMs first: a backlog of channel posts must never push the
        # one message that wants an answer past max_events.
        queued.sort(key=lambda e: e.kind not in ("mention", "dm", "reaction"))
        events, rest = queued[:max(1, max_events)], queued[max(1, max_events):]
        for e in rest:
            self._queue.put_nowait(e)
        self.pending = len(rest)
        # Handed over: the listener must not wake the agent for these again.
        for e in events:
            if e.kind == "reaction":
                asks.mark_seen(self._bot_user_id, e.post["id"])
        # A channel with events still queued stays unread, so its counters keep
        # telling the hooks and the listener that something waits.
        waiting = {e.post.get("channel_id") for e in rest}
        await self._mark_delivered_read(
            [e for e in events if e.post.get("channel_id") not in waiting]
        )
        # Posts from watched channels often need no answer, so only a mention or
        # DM, which does, promises one.
        addressed = next((e for e in events if e.kind in ("mention", "dm")), None)
        if addressed is not None:
            post = addressed.post
            self.start_typing(
                post.get("channel_id", ""), post.get("root_id") or post.get("id", "")
            )
        return events

    async def _mark_delivered_read(self, events: list[ClassifiedEvent]) -> None:
        """Clear Mattermost's unread counters for channels we just delivered.

        Handing an event to the agent is what "read" means here. Without this
        the counters keep climbing, and the Stop hook that reads them to decide
        "is anyone waiting on me?" would block every turn forever.
        """
        seen: set[str] = set()
        for event in events:
            cid = event.post.get("channel_id")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            try:
                await self.client.view_channel(cid)
            except Exception as exc:  # noqa: BLE001
                # Never fail delivery over bookkeeping.
                log.warning("could not mark channel %s read: %s", cid, exc)

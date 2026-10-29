"""WebSocket inbox: classify posts and wait for relevant events."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from websockets.asyncio.client import connect as ws_connect

from . import a2a, asks, departed, gate, lease, ownpost, unread, watermark
from .client import (MattermostAdapter, guest_ids, lobby_ids, post_of, reaction_of, read_links,
                     watched_channel_ids, with_links, ws_url_from_http)
from .model import Channel, Post, Reaction

log = logging.getLogger(__name__)


@dataclass
class ClassifiedEvent:
    kind: str  # mention | dm | watched_channel | reaction (to an open ask) | stall (RT-6.19)
    post: Post
    channel: Channel | None = None
    raw: dict[str, Any] | None = None
    # RT-6.16: why the post wakes nobody here; "" when it is not gated.
    gated: str = ""
    # RT-6.21: the departed agents it mentions, for the agent that answers.
    departed: str = ""
    # RT-6.22: the post is in a guest channel (not the home, not the lobby).
    guest: bool = False
    # RT-6.22 step 3: the post is in the lobby (MM_CHANNEL), which is not the home.
    lobby: bool = False


# WebSocket events that change the bot's channels (RT-6.22): the watched and
# guest channels are read again when they name the bot.
MEMBERSHIP = ("user_added", "user_removed")


def classify_post(
    post: Post | None,
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
    if post.user_id == bot_user_id:
        return None
    if unread.names_me(post.message, bot_user_id or "", bot_username or ""):
        return "mention"
    ctype = (channel_type or "").upper()
    if ctype == "D":
        return "dm"
    watched = watched_channel_ids or set()
    cid = post.channel_id
    if cid and cid in watched:
        return "watched_channel"
    return None


@dataclass
class WebSocketInbox:
    client: MattermostAdapter
    watched_channel_ids: set[str] = field(default_factory=set)
    # The guest channels among them (RT-6.22): only a mention or a reply in a
    # thread this agent took part in there is queued.
    guest_channel_ids: set[str] = field(default_factory=set)
    # The lobby among them while it is not the home (RT-6.22 step 3): only a
    # mention, a broadcast or a reply in a thread this agent posted in is queued.
    lobby_channel_ids: set[str] = field(default_factory=set)
    # The lists were read here (not handed in): they follow membership changes.
    _derived: bool = field(default=False, repr=False, init=False)
    _ws: Any = field(default=None, repr=False, init=False)
    _seq: int = field(default=1, repr=False, init=False)
    _queue: asyncio.Queue[ClassifiedEvent] = field(
        default_factory=asyncio.Queue, repr=False, init=False
    )
    _reader: asyncio.Task[None] | None = field(default=None, repr=False, init=False)
    _channel_cache: dict[str, Channel] = field(
        default_factory=dict, repr=False, init=False
    )
    _bot_user_id: str = field(default="", repr=False, init=False)
    _bot_username: str = field(default="", repr=False, init=False)
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
    # The wake gate (RT-6.16) asks the server whether a reply is in a thread
    # this bot posted in, so pusher and gate share one cache; None: never.
    in_my_thread: Callable[[dict[str, Any], str], Awaitable[bool | None]] | None = None
    _gate_tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False, init=False)

    async def ensure_identity(self) -> None:
        me = await self.client.get_me()
        self._bot_user_id = me.id
        self._bot_username = me.username
        if not self.watched_channel_ids or self._derived:
            # The work, project and default channel (where the agent works and
            # stays addressable) and the guest channels: the same list the
            # hook API counts from. Read again at every connect.
            await self.refresh_channels()

    async def refresh_channels(self) -> None:
        """Read the watched and guest channels again (RT-6.2 step 1, RT-6.22):
        at connect, and when a WebSocket event says the bot joined or left a
        channel. Unreadable memberships leave the guests out, the rest stays."""
        forget = getattr(self.client, "forget_guests", None)
        if forget is not None:
            forget()
        self.guest_channel_ids = await guest_ids(self.client)
        self.lobby_channel_ids = await lobby_ids(self.client)
        self.watched_channel_ids = await watched_channel_ids(self.client)
        self._derived = True

    async def _on_membership(self, msg: dict[str, Any]) -> None:
        """user_added / user_removed naming this bot: someone added it to a
        channel (a guest channel now) or removed it (RT-6.22)."""
        data, where = msg.get("data") or {}, msg.get("broadcast") or {}
        me = self._bot_user_id
        if not self._derived or not me or me not in (data.get("user_id"), where.get("user_id")):
            return
        try:
            await self.refresh_channels()
        except Exception as exc:  # noqa: BLE001 — the old lists stay
            log.warning("channels not read again after %s: %s", msg.get("event"), exc)

    def _guest_drop(self, post: Post, kind: str) -> bool:
        """RT-6.22: a post in a guest channel that neither names this agent nor
        answers in a thread it took part in there (threads-<uid>.env, the list
        the count reads): no event, no digest. Its mark moves on."""
        if kind != "watched_channel" or post.channel_id not in self.guest_channel_ids:
            return False
        if post.root_id and post.root_id in ownpost.guest_threads(self._bot_user_id):
            return False
        self.handled(post)
        return True

    async def _lobby_drop(self, post: Post, kind: str) -> bool:
        """RT-6.22 step 3: a post in the lobby (MM_CHANNEL, not the home) that
        neither names this agent, nor names everyone (@channel, @all, @here, or
        to all agents), nor answers in a thread it posted in: no event, no
        digest. Its mark moves on. Unsure about the thread: kept."""
        if kind != "watched_channel" or post.channel_id not in self.lobby_channel_ids:
            return False
        if post.to == a2a.ALL or unread.everyone(post.message):
            return False
        if post.root_id and (self.in_my_thread is None or await self.in_my_thread(
                {"post": {"root_id": post.root_id}}, self._bot_user_id) is not False):
            return False
        self.handled(post)
        return True

    async def _drop(self, post: Post, kind: str) -> bool:
        """Not for this agent where it is a guest or in the lobby (RT-6.22)."""
        return self._guest_drop(post, kind) or await self._lobby_drop(post, kind)

    async def _channel(self, channel_id: str) -> Channel:
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
        the pending count still reported it. That mismatch is what made the Stop
        hook block on a message nothing could deliver. Reads what unread.after
        finds past the read watermark, the same posts the count comes from.
        """
        try:
            unhandled = await unread.after(self.client)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not look for missed messages: %s", exc)
            return
        # Every channel whose newest post is past the read watermark (RT-6.2),
        # read from the mark: never before the bot existed, at most a day back.
        for entry, posts in unhandled:
            for unlinked in posts:
                post = with_links(unlinked, getattr(self.client, "permalink_base", ""))
                kind = classify_post(
                    post,
                    bot_user_id=self._bot_user_id,
                    bot_username=self._bot_username,
                    channel_type=entry.channel_type,
                    watched_channel_ids=self.watched_channel_ids,
                )
                if not kind or await self._drop(post, kind):
                    continue
                if post.id in self._seen_ids:
                    continue
                self._seen_ids.add(post.id)
                event = ClassifiedEvent(kind=kind, post=post,
                                        guest=post.channel_id in self.guest_channel_ids,
                                        lobby=post.channel_id in self.lobby_channel_ids)
                await self._gate(event, entry.channel_type)
                await self._departed(event)
                await self._put(event)
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
                if msg.get("event") in MEMBERSHIP:
                    await self._on_membership(msg)
                    continue
                if msg.get("event") != "posted":
                    continue
                data = msg.get("data") or {}
                post_raw = data.get("post")
                if isinstance(post_raw, str):
                    try:
                        found = json.loads(post_raw)
                    except json.JSONDecodeError:
                        continue
                elif isinstance(post_raw, dict):
                    found = post_raw
                else:
                    continue
                if not isinstance(found, dict) or not found:
                    continue
                # RT-6.15: the bus's permalinks as bare ids, odd links written out.
                post = post_of(read_links(found, getattr(self.client, "permalink_base", "")))
                channel_type = data.get("channel_type")
                channel = None
                cid = post.channel_id
                if cid and not channel_type:
                    try:
                        channel = await self._channel(cid)
                        channel_type = channel.type or None
                    except Exception:  # noqa: BLE001
                        channel = None
                kind = classify_post(
                    post,
                    bot_user_id=self._bot_user_id,
                    bot_username=self._bot_username,
                    channel_type=channel_type,
                    watched_channel_ids=self.watched_channel_ids,
                )
                if not kind or await self._drop(post, kind):
                    continue
                pid = post.id
                if pid:
                    if pid in self._seen_ids:
                        continue
                    self._seen_ids.add(pid)
                event = ClassifiedEvent(kind=kind, post=post, channel=channel, raw=msg,
                                        guest=cid in self.guest_channel_ids,
                                        lobby=cid in self.lobby_channel_ids)
                await self._gate(event, channel_type)
                await self._departed(event)
                await self._put(event)
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
        if isinstance(reaction, dict):
            await self._queue_reaction(reaction_of(reaction), cid)

    async def _queue_reaction(self, reaction: Reaction, channel_id: str) -> None:
        """Queue a reaction to one of this bot's open asks (RT-6.11).

        Mattermost raises no unread counter for a reaction, and nothing but an
        ask makes one meaningful: a 👍 on any other post stays a remark.
        """
        pid, user, emoji = reaction.post_id, reaction.user_id, reaction.emoji_name
        if not pid or not emoji or user == self._bot_user_id:
            return
        if not asks.is_open(self._bot_user_id, pid):
            return
        key = asks.seen_key(pid, user, emoji)
        if key in self._seen_ids or asks.was_seen(self._bot_user_id, key):
            return
        self._seen_ids.add(key)
        post = post_of({"id": key, "root_id": pid, "channel_id": channel_id, "user_id": user,
                        "create_at": reaction.create_at, "message": f":{emoji}:"})
        event = ClassifiedEvent(kind="reaction", post=post, raw={"emoji": emoji, "ask": pid})
        await self._gate_reaction(event)
        await self._queue.put(event)

    async def _gate_reaction(self, event: ClassifiedEvent) -> None:
        """RT-6.16 step 7: with trusted humans configured, a reaction by anyone
        else wakes no worker, as their post would not: it approves nothing
        (RT-6.11). Queued as data and marked GATED for the listener; no answer,
        a reaction asks nothing. The coordinator is never gated; errors fail open."""
        me = self._bot_user_id
        try:
            if (not me or await lease.coordinates_in(self.client, me, event.post.channel_id)
                    or not await self.client.trusted_humans()):
                return
            if (await gate.author_classes(self.client, [event.post]))[0] != gate.OTHER:
                return
            who = await gate.author_name(self.client, event.post)
            gate.mark_gated(me, event.post.id)
        except Exception as exc:  # noqa: BLE001 — fail open
            log.info("wake gate open for reaction %s: %s", event.post.id, exc)
            return
        event.gated = f"from {who}, not trusted by me: approves nothing"

    async def _gate(self, event: ClassifiedEvent, channel_type: str | None) -> None:
        """RT-6.16: with trusted humans configured, a post by anyone else that
        would wake this worker is gated: queued as data only, and answered in
        its thread once (_after_gate). The coordinator is never gated. Any
        error leaves the post as it was: it wakes as before."""
        post, me = event.post, self._bot_user_id
        root = post.thread
        try:
            # The coordinator is never gated where it routes (RT-6.16 step 2).
            if not me or await lease.coordinates_in(self.client, me, post.channel_id):
                return
            if not await self.client.trusted_humans():
                return
            cls = (await gate.author_classes(self.client, [post]))[0]
            if cls not in (gate.OTHER, gate.FOREIGN):
                return
            mine: bool | None = False
            if event.kind not in ("mention", "dm") and post.root_id and self.in_my_thread:
                mine = await self.in_my_thread({"post": {"root_id": post.root_id}}, me)
                if mine is None:
                    return  # unsure: as before
            why = gate.wake_reason(event.kind, post, bool(mine))
            # A foreign agent (ADR-001) wakes the worker as a request when it
            # names it or answers in its thread; only its broadcasts are gated.
            if not why or (cls == gate.FOREIGN and why != "broadcast"):
                return
            who = await gate.author_name(self.client, post)
            gate.mark_gated(me, post.id)
            # One @channel would get an answer from every worker: none for that.
            # Nor for a post that already names the coordinator (not in a DM,
            # which it cannot see): it was told; nor for one that names another
            # agent that trusts its author: that one answers.
            dm = (channel_type or "").upper() in ("D", "G")
            # RT-6.22: in a guest channel or the lobby, that channel's own coordinator.
            guest = post.channel_id if event.guest or event.lobby else ""
            answer = why != "broadcast" and not (
                not dm and await gate.names_coordinator(self.client, post, guest)
            ) and not await gate.mentioned_agent_trusts(
                self.client, post, who, me) and gate.first_reply(me, root)
        except Exception as exc:  # noqa: BLE001 — fail open
            log.info("wake gate open for post %s: %s", post.id or None, exc)
            return
        event.gated = (f"from {who}, an agent of another owner: its broadcast is no task"
                       if cls == gate.FOREIGN else
                       f"from {who}, not trusted by me: the coordinator answers")
        task = asyncio.create_task(self._after_gate(
            event, who, root if answer else "", dm=dm, guest=guest))
        self._gate_tasks.add(task)
        task.add_done_callback(self._gate_tasks.discard)

    async def _after_gate(self, event: ClassifiedEvent, who: str, root: str, *, dm: bool,
                          guest: str = "") -> None:
        """Answer a gated post in thread `root` ('' for no answer), without the
        model. The gated post and the answer are handled (RT-6.2): the read
        watermark passes them, unless a post before them there still waits."""
        cid = event.post.channel_id
        if root:
            try:
                text = await gate.reply_text(self.client, who, dm=dm, guest=guest)
                self.handled(await self.client.post(text, channel_id=cid or None, root_id=root))
            except Exception as exc:  # noqa: BLE001 — the post is gated all the same
                log.info("no answer to the gated post in thread %s: %s", root, exc)
                with contextlib.suppress(OSError):
                    gate.forget_reply(self._bot_user_id, root)
        if event.kind != "reaction":  # a reaction is no post: it moves no mark
            self.handled(event.post)
        await self.view(cid)

    async def _departed(self, event: ClassifiedEvent) -> None:
        """RT-6.21: a mention of an agent that is gone. The one server that
        answers (the coordinator's) marks the event, which wakes its agent,
        and answers in the thread. Any error leaves the post as it was."""
        me, post = self._bot_user_id, event.post
        try:
            found, tell = await departed.check(self.client, post, me, self._bot_username)
        except Exception as exc:  # noqa: BLE001 — fail open
            log.info("departed agents in post %s not checked: %s", post.id or None, exc)
            return
        if not found:
            return
        event.departed = ", ".join(d.username for d in found)
        if tell:
            task = asyncio.create_task(departed.tell(self.client, post, me, tell))
            self._gate_tasks.add(task)
            task.add_done_callback(self._gate_tasks.discard)

    def notify(self, kind: str, message: str) -> None:
        """Queue an event the server raises itself, with no post behind it
        (RT-6.19: a stall); delivered like any other."""
        self._queue.put_nowait(ClassifiedEvent(kind=kind, post=post_of({
            "id": "", "message": message, "create_at": int(time.time() * 1000)})))

    def waits(self, kind: str) -> bool:
        """Whether an event of that kind waits in the queue."""
        # asyncio.Queue keeps its items in a private deque that typeshed omits.
        return any(e.kind == kind for e in list(self._queue._queue))  # type: ignore[attr-defined]

    def holds(self, channel_id: str) -> bool | None:
        """Whether events of this channel wait in the queue; None while the
        socket is not connected, so nothing is known about what arrived.
        A gated post (RT-6.16) does not count: it keeps no counter up."""
        if self._ws is None:
            return None
        return any(e.post.channel_id == channel_id and not e.gated
                   for e in list(self._queue._queue))  # type: ignore[attr-defined]  # see waits()

    def _first_waiting(self, channel_id: str) -> int | None:
        """create_at of the oldest post of the channel that waits in the queue
        unhandled (not gated), None for none."""
        ats = [e.post.create_at for e in list(self._queue._queue)  # type: ignore[attr-defined]
               if e.post.channel_id == channel_id and not e.gated and e.kind != "reaction"]
        return min(ats) if ats else None

    def handled(self, post: Post) -> None:
        """RT-6.2: the post is handled (handed over, gated, or the bot's own):
        the channel's read watermark passes it, unless an older post there
        still waits in the queue. Unconnected, nothing moves: the backfill
        still has to read from the mark."""
        cid, pid, created = post.channel_id, post.id, post.create_at
        if self._ws is None or not (self._bot_user_id and cid and pid and created):
            return
        first = self._first_waiting(cid)
        if first is not None and first <= created:
            return
        try:
            watermark.advance(self._bot_user_id, cid, pid, created)
        except Exception as exc:  # noqa: BLE001 — never fail delivery over bookkeeping
            log.warning("read watermark of %s not moved: %s", cid, exc)

    async def view(self, channel_id: str) -> None:
        """Best-effort: clear Mattermost's unread display of the channel for
        its UI, unless an event there still waits. The bus counts nothing by it."""
        if not channel_id or self.holds(channel_id) is not False:
            return
        try:
            await self.client.view_channel(channel_id)
        except Exception as exc:  # noqa: BLE001 — cosmetic
            log.info("could not mark %s read: %s", channel_id, exc)

    async def _put(self, event: ClassifiedEvent) -> None:
        """Queue an event. A post queued behind the read watermark (it was on
        its way while the bot posted there) holds the mark back: it counts as
        pending until it is handed over (RT-6.2)."""
        post = event.post
        if not event.gated and event.kind != "reaction" and post.channel_id:
            try:
                watermark.hold(self._bot_user_id, post.channel_id, post.create_at)
            except Exception as exc:  # noqa: BLE001
                log.warning("read watermark not held: %s", exc)
        await self._queue.put(event)

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
        # one message that wants an answer past max_events. Gated posts, data
        # only, come last (RT-6.16).
        queued.sort(key=lambda e: (bool(e.gated), e.kind not in ("mention", "dm", "reaction", "stall")))
        events, rest = queued[:max(1, max_events)], queued[max(1, max_events):]
        for e in rest:
            self._queue.put_nowait(e)
        self.pending = len(rest)
        # Handed over: the listener must not wake the agent for these again.
        for e in events:
            if e.kind == "reaction":
                asks.mark_seen(self._bot_user_id, e.post.id)
        # Handed over: the read watermark passes them, up to the oldest post
        # still queued in that channel, which keeps counting as pending.
        await self._hand_over(events)
        # Posts from watched channels often need no answer, so only a mention or
        # DM, which does, promises one; a gated one promises nothing (RT-6.16).
        addressed = next((e for e in events if e.kind in ("mention", "dm") and not e.gated), None)
        if addressed is not None:
            post = addressed.post
            self.start_typing(post.channel_id, post.thread)
        return events

    async def _hand_over(self, events: list[ClassifiedEvent]) -> None:
        """Handing an event to the agent is what "handled" means (RT-6.2): the
        read watermark passes its post, which the pending count of the Stop
        hook and the listener then leaves out. Mattermost's display is cleaned
        up too, for its UI (view)."""
        for event in sorted(events, key=lambda e: e.post.create_at):
            if event.kind not in ("reaction", "stall"):
                self.handled(event.post)
        for cid in dict.fromkeys(e.post.channel_id for e in events):
            await self.view(cid)

"""The inbox's three risky behaviours, pinned through its public surface only
(ensure_connected, wait_for_events, take/ack, connected, close), before the
Mattermost WebSocket moves out of websocket_inbox into an event source:

1. backfill and live stream: what came before the socket opened (past the
   read watermark, RT-6.2) and what comes live are delivered, in today's order;
2. dedup: a post, an ask's reaction (RT-6.11) or an edit (RT-6.12 step 3)
   seen twice wakes once;
3. reconnect after a drop: the next read dials again, authenticates, runs
   the backfill, and delivers each post exactly once.

The one seam is the socket dial (DIAL): a fake socket that speaks
Mattermost's frames. Everything else is the shared chat fake.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError

from fakes import BOT, WORK, FakeChat
from mattermost_ai_agent_bus import asks, watermark
from mattermost_ai_agent_bus.model import Post, UnreadChannel
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent, WebSocketInbox

# Where the inbox dials Mattermost's WebSocket: the one line the event source changes.
DIAL = "mattermost_ai_agent_bus.websocket_inbox.ws_connect"

HUMAN = "h" * 26
ASK = "a" * 26
END, ERROR = object(), object()   # the socket ends cleanly / drops with an error


def pid(name: str) -> str:
    return name.ljust(26, "0")


class FakeSocket:
    """Mattermost's WebSocket: answers the authentication challenge, then
    yields the frames fed to it, and waits for more until it is dropped."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._replies: list[str] = []
        self._frames: asyncio.Queue[object] = asyncio.Queue()
        self._idle = asyncio.Event()

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("action") == "authentication_challenge":
            self._replies.append(json.dumps(
                {"event": "hello", "data": {"server_version": "10"}} if self.reply == "hello"
                else {"status": "OK", "seq_reply": msg.get("seq")}))

    async def recv(self) -> str:
        return self._replies.pop(0)

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        if self._frames.empty():
            self._idle.set()
        item = await self._frames.get()
        if item is END:
            raise StopAsyncIteration
        if item is ERROR:
            raise ConnectionClosedError(None, None)
        assert isinstance(item, str)
        return item

    async def close(self) -> None:
        self.closed = True
        self._frames.put_nowait(END)

    def feed(self, *frames: dict[str, Any]) -> None:
        self._idle.clear()
        for f in frames:
            self._frames.put_nowait(json.dumps(f))

    def drop(self, how: object) -> None:
        self._frames.put_nowait(how)

    async def settled(self) -> None:
        """Until the reader has taken every frame fed and waits for the next."""
        await asyncio.wait_for(self._idle.wait(), 5)

    @property
    def authenticated(self) -> bool:
        return any(m.get("action") == "authentication_challenge"
                   and m.get("data", {}).get("token") == Chat.token for m in self.sent)


class Dialer:
    """The fake dial: a new socket per connect. Frames in `on_open` are on the
    next socket the moment it opens (they arrived live around the open)."""

    def __init__(self) -> None:
        self.sockets: list[FakeSocket] = []
        self.on_open: list[dict[str, Any]] = []
        self.replies = ["hello", "ok"]

    async def __call__(self, url: str, *args: Any, **kwargs: Any) -> FakeSocket:
        assert url.startswith("wss://mm.example")
        sock = FakeSocket(self.replies[len(self.sockets) % len(self.replies)])
        sock.feed(*self.on_open)
        self.on_open = []
        self.sockets.append(sock)
        return sock

    @property
    def now(self) -> FakeSocket:
        return self.sockets[-1]


class Chat(FakeChat):
    """FakeChat with the adapter's base URL and one channel, the home, whose
    newest post is what `posts` holds. `during_read` runs while the backfill
    reads the channel (a REST read takes its time: the socket goes on)."""

    base_url = "https://mm.example"

    def __init__(self) -> None:
        super().__init__(username="agent-me")
        self.during_read: Any = None

    async def member_channels(self) -> list[UnreadChannel]:
        at = max((p.create_at for p in self._posts()), default=0)
        return [UnreadChannel(WORK, "O", last_post_at=at)]

    async def channel_posts(self, channel_id: str, *, per_page: int | None = None,
                            since_ms: int | None = None) -> list[Post]:
        got = await super().channel_posts(channel_id, per_page=per_page, since_ms=since_ms)
        if self.during_read is not None:
            during, self.during_read = self.during_read, None
            await during()
        return got

    def add(self, post: dict[str, Any]) -> None:
        self.posts.insert(0, post)


_clock = iter(range(int(time.time() * 1000) - 600_000, 1 << 62, 1000))


def post(name: str, message: str = "status update", **kw: Any) -> dict[str, Any]:
    return {"id": pid(name), "user_id": HUMAN, "channel_id": WORK, "root_id": "",
            "message": message, "create_at": next(_clock)} | kw


def posted(p: dict[str, Any]) -> dict[str, Any]:
    return {"event": "posted", "broadcast": {"channel_id": p["channel_id"]},
            "data": {"channel_type": "O", "post": json.dumps(p)}}


def edited(p: dict[str, Any], message: str) -> dict[str, Any]:
    p = p | {"message": message, "edit_at": int(time.time() * 1000)}
    return {"event": "post_edited", "broadcast": {"channel_id": p["channel_id"]},
            "data": {"post": json.dumps(p)}}


def reaction(emoji: str = "+1", at: int = 1) -> dict[str, Any]:
    return {"user_id": HUMAN, "post_id": ASK, "emoji_name": emoji, "create_at": at}


def reaction_added(r: dict[str, Any]) -> dict[str, Any]:
    return {"event": "reaction_added", "broadcast": {"channel_id": WORK},
            "data": {"reaction": json.dumps(r)}}


@pytest.fixture
def chat() -> Chat:
    return Chat()


@pytest.fixture
def dial(monkeypatch: pytest.MonkeyPatch) -> Dialer:
    d = Dialer()
    monkeypatch.setattr(DIAL, d)
    return d


@pytest.fixture
async def inbox(chat: Chat, dial: Dialer) -> Any:
    box = WebSocketInbox(client=chat, watched_channel_ids={WORK})
    yield box
    await box.close()


async def drain(inbox: WebSocketInbox, *, take: bool = False) -> list[ClassifiedEvent]:
    """Everything the agent gets, batch after batch, until nothing more comes."""
    got: list[ClassifiedEvent] = []
    while True:
        if take:
            batch = await inbox.take(timeout_sec=0.2)
            await inbox.ack(batch)
        else:
            batch = await inbox.wait_for_events(timeout_sec=0.2)
        if not batch:
            return got
        got += batch


def ids(events: list[ClassifiedEvent]) -> list[str]:
    return [e.post.id.rstrip("0") for e in events]


async def until_disconnected(inbox: WebSocketInbox) -> None:
    for _ in range(200):
        if not inbox.connected:
            return
        await asyncio.sleep(0)
    raise AssertionError("the inbox did not notice the drop")


# 1. Backfill and live stream ------------------------------------------------


async def test_backfill_then_live_in_that_order(chat, dial, inbox):
    """Risk 1, RT-6.2: the posts past the read watermark that came before the
    socket opened are delivered (oldest first), the one behind the mark is
    not, and posts arriving live after the open follow them."""
    old = post("old")
    chat.add(old)
    watermark.advance(BOT, WORK, old["id"], old["create_at"])
    chat.add(post("b1"))
    chat.add(post("b2"))
    await inbox.ensure_connected()
    assert inbox.connected and dial.now.authenticated
    dial.now.feed(posted(post("l1")), posted(post("l2")))
    assert ids(await drain(inbox)) == ["b1", "b2", "l1", "l2"]


async def test_a_post_both_backfilled_and_live_at_the_open_comes_once_in_backfill_order(
        chat, dial, inbox):
    """Risks 1 and 2, RT-6.2: a post both in the backfill and on the socket as
    it opens (the overlap around the open) is delivered once, in the
    backfill's place; the live posts already on the socket follow the backfill."""
    b1, both = post("b1"), post("both")
    chat.add(b1)
    chat.add(both)
    dial.on_open = [posted(both), posted(post("l1"))]
    events = await drain(inbox)
    assert ids(events) == ["b1", "both", "l1"]
    assert dial.now.authenticated


async def test_live_posts_during_the_backfill_read_come_before_the_older_backfill(
        chat, dial, inbox):
    """Risks 1 and 2, RT-6.2, today's order: the socket is read while the
    backfill reads the channel, so a post that arrives live meanwhile is
    queued before the older posts the backfill finds; one that is both live
    and in the backfill comes once, in its live place."""
    b1, both = post("b1"), post("both")
    chat.add(b1)
    chat.add(both)

    async def arrive() -> None:
        dial.now.feed(posted(both), posted(post("l1")))
        await dial.now.settled()

    chat.during_read = arrive
    assert ids(await drain(inbox)) == ["both", "l1", "b1"]


# 2. Dedup -------------------------------------------------------------------


async def test_a_reaction_to_an_open_ask_seen_by_backfill_and_live_comes_once(
        chat, dial, inbox):
    """Risk 2, RT-6.11: a reaction to an open ask that the backfill reads and
    that also arrives as reaction_added is delivered once; after the drop
    and reconnect the backfill reads it again and delivers nothing."""
    asks.add(BOT, ASK)
    chat.reacts[ASK] = [reaction()]
    dial.on_open = [reaction_added(reaction())]
    events = await drain(inbox)
    assert [(e.kind, e.raw["emoji"], e.raw["ask"]) for e in events] == [("reaction", "+1", ASK)]
    dial.now.feed(reaction_added(reaction()))
    assert await drain(inbox) == []
    dial.now.drop(ERROR)
    await until_disconnected(inbox)
    assert await drain(inbox) == [] and len(dial.sockets) == 2


@pytest.mark.parametrize("delivered_first", [False, True])
async def test_an_edit_of_a_post_queued_as_a_mention_does_not_wake_again(
        chat, dial, inbox, delivered_first):
    """Risk 2, RT-6.12 step 3: an edit of a post queued as a mention (still
    waiting, or already handed over) wakes nobody again, a typo fix
    included; the edit that first makes a plain post name the agent does wake,
    once, marked edited."""
    await inbox.ensure_connected()
    named, plain = post("named", "@agent-me please look"), post("plain", "deploy is done")
    dial.now.feed(posted(named), posted(plain))
    if delivered_first:
        first = await drain(inbox)
        assert [(e.post.id.rstrip("0"), e.kind) for e in first] == [
            ("named", "mention"), ("plain", "watched_channel")]
    dial.now.feed(edited(named, "@agent-me please look again"),
                  edited(plain, "@agent-me deploy is done"))
    events = await drain(inbox)
    got = [(e.post.id.rstrip("0"), e.kind, e.edited, e.post.message) for e in events]
    edit = ("plain", "mention", True, "@agent-me deploy is done")
    if delivered_first:
        assert got == [edit]
    else:
        assert got == [("named", "mention", False, "@agent-me please look"), edit,
                       ("plain", "watched_channel", False, "deploy is done")]


async def test_an_edit_of_a_backfilled_mention_does_not_wake_again(chat, dial, inbox):
    """Risk 2, RT-6.12 step 3: a mention the backfill queued counts as queued
    as a mention: its edit arriving live wakes nobody."""
    named = post("named", "@agent-me please look")
    chat.add(named)
    dial.on_open = [edited(named, "@agent-me please look, typo fixed")]
    events = await drain(inbox)
    assert [(e.post.id.rstrip("0"), e.kind, e.edited) for e in events] == [
        ("named", "mention", False)]


# 3. Reconnect after a drop --------------------------------------------------


@pytest.mark.parametrize("read", ["wait_for_events", "take"])
@pytest.mark.parametrize("how", [ERROR, END], ids=["error", "closed"])
async def test_after_a_drop_the_next_read_reconnects_and_delivers_each_post_once(
        chat, dial, inbox, how, read):
    """Risk 3, RT-6.2: the socket drops mid-stream (an error, or the server
    closes it); the inbox reads as not connected. The next read dials again,
    authenticates, runs the backfill: the post that arrived while nobody
    listened comes once, the one delivered before the drop does not come
    again, and one queued live but not yet read before the drop comes once."""
    take = read == "take"
    await inbox.ensure_connected()
    first = post("before")
    chat.add(first)
    dial.now.feed(posted(first))
    assert ids(await drain(inbox, take=take)) == ["before"]

    queued = post("queued")
    chat.add(queued)
    dial.now.feed(posted(queued))
    await dial.now.settled()
    dial.now.drop(how)
    await until_disconnected(inbox)
    assert len(dial.sockets) == 1

    missed = post("missed")
    chat.add(missed)   # sent while nothing listened: on no socket
    later = post("later")
    chat.add(later)
    dial.on_open = [posted(later)]
    assert ids(await drain(inbox, take=take)) == ["queued", "missed", "later"]
    assert inbox.connected and len(dial.sockets) == 2
    assert dial.now.authenticated and dial.now.reply == "ok"
    assert watermark.read(BOT)[WORK].post_id == later["id"]
    await inbox.close()
    assert not inbox.connected and dial.now.closed


async def test_a_drop_before_anything_was_read_loses_nothing(chat, dial, inbox):
    """Risk 3, RT-6.2: a post that arrived live and a post that came while the
    socket was down, read only after the reconnect, each come once."""
    await inbox.ensure_connected()
    live = post("live")
    chat.add(live)
    dial.now.feed(posted(live))
    await dial.now.settled()
    dial.now.drop(ERROR)
    await until_disconnected(inbox)
    chat.add(post("gap"))
    await inbox.ensure_connected()
    assert inbox.connected and dial.now.authenticated
    assert ids(await drain(inbox)) == ["live", "gap"]

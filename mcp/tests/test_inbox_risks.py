"""The inbox's three risky behaviours, pinned through its public surface only
(ensure_connected, wait_for_events, take/ack, connected, close), before the
Mattermost WebSocket moves out of websocket_inbox into an event source:

1. backfill and live stream: what came before the socket opened (past the
   read mark, RT-6.2 step 1) and what comes live are both delivered;
2. dedup: a post, an ask's reaction (RT-6.11) or an edit (RT-6.12 step 3)
   seen twice wakes once;
3. reconnect after a drop: the next read dials again, authenticates, runs
   the backfill, and delivers each post exactly once.

RT-6.2 promises once-only delivery, mentions and DMs first within a batch,
and the read mark at the newest post handed over; it promises no order
between the backfill and the live stream, so the tests do not pin one.

The one seam is the socket dial (DIAL): a fake socket that speaks
Mattermost's frames. Everything else is the shared chat fake, whose REST
reads suspend as a real client's do.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from fakes import BOT, WORK, FakeChat
from mattermost_ai_agent_bus import asks, unread, watermark
from mattermost_ai_agent_bus.model import Post, UnreadChannel
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent, WebSocketInbox

# Where the inbox dials Mattermost's WebSocket: the one line the event source changes.
DIAL = "mattermost_ai_agent_bus.mattermost_ws.ws_connect"

HUMAN = "h" * 26
HUMAN2 = "k" * 26
ASK = "a" * 26
END, ERROR = object(), object()   # the socket ends cleanly / drops with an error


def pid(name: str) -> str:
    return name.ljust(26, "0")


class FakeSocket:
    """Mattermost's WebSocket: answers the authentication challenge, then
    yields the frames fed to it, and waits for more until it is dropped.
    Read by iteration or by recv() alike: the handshake replies come first,
    then the frames."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.sent: list[dict[str, Any]] = []
        self.closed = False
        self._replies: list[str] = []
        self._frames: asyncio.Queue[object] = asyncio.Queue()

    async def send(self, raw: str) -> None:
        msg = json.loads(raw)
        self.sent.append(msg)
        if msg.get("action") == "authentication_challenge":
            self._replies.append(json.dumps(
                {"event": "hello", "data": {"server_version": "10"}} if self.reply == "hello"
                else {"status": "OK", "seq_reply": msg.get("seq")}))

    async def _next(self) -> object:
        if self._replies:
            return self._replies.pop(0)
        item = await self._frames.get()
        if item is ERROR:
            raise ConnectionClosedError(None, None)
        return item

    async def recv(self) -> str:
        item = await self._next()
        if item is END:
            raise ConnectionClosedOK(None, None)
        assert isinstance(item, str)
        return item

    def __aiter__(self) -> FakeSocket:
        return self

    async def __anext__(self) -> str:
        item = await self._next()
        if item is END:
            raise StopAsyncIteration
        assert isinstance(item, str)
        return item

    async def close(self) -> None:
        self.closed = True
        self._frames.put_nowait(END)

    def feed(self, *frames: dict[str, Any] | object) -> None:
        """Frames as they arrive; END or ERROR drops the socket there."""
        for f in frames:
            self._frames.put_nowait(f if f in (END, ERROR) else json.dumps(f))

    @property
    def authenticated(self) -> bool:
        return any(m.get("action") == "authentication_challenge"
                   and m.get("data", {}).get("token") == Chat.token for m in self.sent)


class Dialer:
    """The fake dial: a new socket per connect. Frames in `on_open` are on the
    next socket the moment it opens (they arrived live around the open);
    `fail` holds errors the next dials raise, one per dial."""

    def __init__(self) -> None:
        self.sockets: list[FakeSocket] = []
        self.on_open: list[dict[str, Any] | object] = []
        self.fail: list[BaseException] = []
        self.replies = ["hello", "ok"]
        self.dials = 0

    async def __call__(self, url: str, *args: Any, **kwargs: Any) -> FakeSocket:
        assert url.startswith("wss://mm.example")
        self.dials += 1
        if self.fail:
            raise self.fail.pop(0)
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
    newest post is what `posts` holds. A REST read suspends, as a real
    client's HTTP call does: the socket is read meanwhile."""

    base_url = "https://mm.example"

    def __init__(self) -> None:
        super().__init__(username="agent-me")

    async def member_channels(self) -> list[UnreadChannel]:
        await asyncio.sleep(0)   # a REST read suspends
        at = max((p.create_at for p in self._posts()), default=0)
        return [UnreadChannel(WORK, "O", last_post_at=at)]

    async def channel_posts(self, channel_id: str, *, per_page: int | None = None,
                            since_ms: int | None = None) -> list[Post]:
        await asyncio.sleep(0)   # a REST read suspends
        return await super().channel_posts(channel_id, per_page=per_page, since_ms=since_ms)

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


def reaction(emoji: str = "+1", user: str = HUMAN) -> dict[str, Any]:
    return {"user_id": user, "post_id": ASK, "emoji_name": emoji, "create_at": 1}


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


def reactions(events: list[ClassifiedEvent]) -> list[tuple[str, str, str, str]]:
    return sorted((e.kind, e.raw["emoji"], e.raw["ask"], e.post.user_id) for e in events)


async def until_disconnected(inbox: WebSocketInbox) -> None:
    for _ in range(200):
        if not inbox.connected:
            return
        await asyncio.sleep(0)
    raise AssertionError("the inbox did not notice the drop")


# 1. Backfill and live stream ------------------------------------------------


async def test_backfill_and_live_posts_are_each_delivered_once(chat, dial, inbox):
    """Risk 1, RT-6.2 step 1: the posts past the read mark that came before
    the socket opened are delivered, the one behind the mark is not, and the
    posts arriving live after the open are delivered too, each once. Today
    the backfill (oldest first) comes before posts fed after the open: current
    behaviour, not a contract."""
    old = post("old")
    chat.add(old)
    watermark.advance(BOT, WORK, old["id"], old["create_at"])
    chat.add(post("b1"))
    chat.add(post("b2"))
    await inbox.ensure_connected()
    assert inbox.connected and dial.now.authenticated
    dial.now.feed(posted(post("l1")), posted(post("l2")))
    assert sorted(ids(await drain(inbox))) == ["b1", "b2", "l1", "l2"]
    assert watermark.read(BOT)[WORK].post_id == pid("l2")


async def test_a_post_both_live_at_the_open_and_in_the_backfill_comes_once(chat, dial, inbox):
    """Risks 1 and 2, RT-6.2 step 1: a post both on the socket as it opens
    and in the backfill (the overlap around the open) is delivered once,
    the others too. Today the socket is read while the backfill's REST
    read is under way, so the live posts come before the older backfilled
    one (both, l1, b1): current behaviour, not a contract."""
    both = post("both")
    chat.add(post("b1"))
    chat.add(both)
    dial.on_open = [posted(both), posted(post("l1"))]
    assert sorted(ids(await drain(inbox))) == ["b1", "both", "l1"]
    assert dial.now.authenticated


async def test_a_live_post_already_backfilled_comes_once(chat, dial, inbox):
    """Risk 2, RT-6.2 step 1: the overlap from the other side: the backfill
    queued the post, then it arrives live as well; delivered once."""
    both = post("both")
    chat.add(both)
    await inbox.ensure_connected()
    dial.now.feed(posted(both))
    assert ids(await drain(inbox)) == ["both"]


# 2. Dedup -------------------------------------------------------------------


async def test_a_reaction_to_an_open_ask_seen_by_backfill_and_live_comes_once(
        chat, dial, inbox):
    """Risk 2, RT-6.11: a reaction to an open ask that the backfill reads and
    that also arrives as reaction_added is delivered once; after a drop and
    the reconnect, the backfill reads it again and delivers nothing, while a
    new reaction (another emoji, or another person's) still comes, once."""
    asks.add(BOT, ASK)
    chat.reacts[ASK] = [reaction()]
    dial.on_open = [reaction_added(reaction())]
    assert reactions(await drain(inbox)) == [("reaction", "+1", ASK, HUMAN)]
    dial.now.feed(reaction_added(reaction()))
    assert await drain(inbox) == []
    dial.now.feed(ERROR)
    await until_disconnected(inbox)
    assert await drain(inbox) == [] and len(dial.sockets) == 2
    dial.now.feed(reaction_added(reaction("-1")), reaction_added(reaction("+1", HUMAN2)),
                  reaction_added(reaction("-1")))
    assert reactions(await drain(inbox)) == [("reaction", "+1", ASK, HUMAN2),
                                             ("reaction", "-1", ASK, HUMAN)]


async def test_a_reaction_handed_over_stays_seen_for_the_next_server(chat, dial, inbox):
    """Risk 2, RT-6.11: what makes a reaction seen outlives the inbox (the
    asks file, not its memory): a new server of the same bot (a second inbox)
    reads it again in its backfill and live, and delivers nothing; a new
    reaction still comes, once."""
    asks.add(BOT, ASK)
    chat.reacts[ASK] = [reaction()]
    assert reactions(await drain(inbox)) == [("reaction", "+1", ASK, HUMAN)]
    await inbox.close()
    again = WebSocketInbox(client=chat, watched_channel_ids={WORK})
    try:
        dial.on_open = [reaction_added(reaction())]
        assert await drain(again) == [] and len(dial.sockets) == 2
        dial.now.feed(reaction_added(reaction("-1")))
        assert reactions(await drain(again)) == [("reaction", "-1", ASK, HUMAN)]
    finally:
        await again.close()


@pytest.mark.parametrize("delivered_first", [False, True])
async def test_an_edit_of_a_post_queued_as_a_mention_does_not_wake_again(
        chat, dial, inbox, delivered_first):
    """Risk 2, RT-6.12 step 3: an edit of a post queued as a mention (still
    waiting, or already handed over) wakes nobody again, a typo fix
    included; the edit that first makes a plain post name the agent does wake,
    once, marked edited (mentions first within a batch)."""
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
    as a mention: its edit arriving live afterwards wakes nobody."""
    named = post("named", "@agent-me please look")
    chat.add(named)
    await inbox.ensure_connected()
    dial.now.feed(edited(named, "@agent-me please look, typo fixed"))
    events = await drain(inbox)
    assert [(e.post.id.rstrip("0"), e.kind, e.edited) for e in events] == [
        ("named", "mention", False)]


# 3. Reconnect after a drop --------------------------------------------------


@pytest.mark.parametrize("read", ["wait_for_events", "take"])
@pytest.mark.parametrize("how", [ERROR, END], ids=["error", "closed"])
async def test_after_a_drop_the_next_read_reconnects_and_delivers_each_post_once(
        chat, dial, inbox, how, read):
    """Risk 3, the read mark (RT-6.2 step 1): the socket drops mid-stream (an
    error, or the server closes it); the inbox reads as not connected. The
    next read dials again, authenticates, runs the backfill: the post that
    arrived while nobody listened comes once, the one delivered before the
    drop does not come again, one queued live but not yet read before the
    drop comes once (first: it was queued first), and the mark ends at the
    newest post."""
    take = read == "take"
    await inbox.ensure_connected()
    first = post("before")
    chat.add(first)
    dial.now.feed(posted(first))
    assert ids(await drain(inbox, take=take)) == ["before"]

    queued = post("queued")
    chat.add(queued)
    dial.now.feed(posted(queued), how)
    await until_disconnected(inbox)
    assert len(dial.sockets) == 1

    missed = post("missed")
    chat.add(missed)   # sent while nothing listened: on no socket
    later = post("later")
    chat.add(later)
    dial.on_open = [posted(later)]
    got = ids(await drain(inbox, take=take))
    assert got[0] == "queued" and sorted(got[1:]) == ["later", "missed"]
    # The second socket's handshake answered {"status": "OK"}, the first {"event": "hello"}.
    assert inbox.connected and len(dial.sockets) == 2 and dial.now.authenticated
    assert watermark.read(BOT)[WORK].post_id == later["id"]
    await inbox.close()
    assert not inbox.connected and dial.now.closed


async def test_a_drop_before_anything_was_read_loses_nothing(chat, dial, inbox):
    """Risk 3, the read mark (RT-6.2 step 1): a post that arrived live and a
    post that came while the socket was down, read only after the
    reconnect, each come once."""
    await inbox.ensure_connected()
    live = post("live")
    chat.add(live)
    dial.now.feed(posted(live), ERROR)
    await until_disconnected(inbox)
    chat.add(post("gap"))
    await inbox.ensure_connected()
    assert inbox.connected and dial.now.authenticated
    assert ids(await drain(inbox)) == ["live", "gap"]


async def test_a_drop_during_the_backfill_doubles_nothing(chat, dial, inbox):
    """Risk 3, the read mark (RT-6.2 step 1): the socket drops while the
    backfill reads; the backfilled posts come once, the next read connects
    again and its backfill does not hand them over a second time, and a post
    after that comes once."""
    chat.add(post("b1"))
    chat.add(post("b2"))
    dial.on_open = [ERROR]
    assert sorted(ids(await drain(inbox))) == ["b1", "b2"]
    await inbox.ensure_connected()
    assert inbox.connected and len(dial.sockets) == 2 and dial.now.authenticated
    assert await drain(inbox) == []
    dial.now.feed(posted(post("l1")))
    assert ids(await drain(inbox)) == ["l1"]


async def test_a_failed_redial_fails_that_read_and_the_next_one_reconnects(chat, dial, inbox):
    """Risk 3, the read mark (RT-6.2 step 1): the socket drops and the next
    dial fails: that read raises and leaves the inbox unconnected; the call
    after it dials again and delivers what came meanwhile, once."""
    await inbox.ensure_connected()
    dial.now.feed(ERROR)
    await until_disconnected(inbox)
    chat.add(post("missed"))
    dial.fail = [OSError("connection refused")]
    with pytest.raises(OSError):
        await inbox.wait_for_events(timeout_sec=0.2)
    assert not inbox.connected and dial.dials == 2
    assert ids(await drain(inbox)) == ["missed"]
    assert inbox.connected and len(dial.sockets) == 2 and dial.now.authenticated


async def test_mentions_handed_over_while_the_socket_was_down_leave_nothing_pending(
        chat, dial, inbox):
    """Risk 3, the read mark (RT-6.2 step 1): the socket drops during the
    backfill, its mentions are handed over all the same; once the inbox is
    connected again, nothing counts as pending for the turn hooks."""
    chat.add(post("b1", "@agent-me one"))
    chat.add(post("b2", "@agent-me two"))
    dial.on_open = [ERROR]
    assert sorted(ids(await drain(inbox))) == ["b1", "b2"]
    assert inbox.connected and len(dial.sockets) == 2   # the last read reconnected
    assert await unread.channels(chat, {WORK}) == []


# 4. The read mark while the backfill runs (#244, #245) ------------------------


class SlowBackfill(Chat):
    """Chat whose posts read (the backfill's snapshot) waits until `go` is
    set: the live socket is read meanwhile, as when Mattermost answers slowly."""

    def __init__(self) -> None:
        super().__init__()
        self.go = asyncio.Event()

    async def channel_posts(self, channel_id: str, *, per_page: int | None = None,
                            since_ms: int | None = None) -> list[Post]:
        await self.go.wait()
        return await super().channel_posts(channel_id, per_page=per_page, since_ms=since_ms)


async def settle() -> None:
    """Let the live reader catch up with what the socket holds."""
    for _ in range(100):
        await asyncio.sleep(0)


def guest_inbox(chat: Chat) -> WebSocketInbox:
    """The home as a guest channel (RT-6.22): a post that does not name the
    agent is dropped there, and dropped is handled: the mark moves on."""
    return WebSocketInbox(client=chat, watched_channel_ids={WORK}, guest_channel_ids={WORK})


async def test_a_post_dropped_while_the_backfill_reads_keeps_the_mark_before_an_older_one(dial):
    """The read mark (RT-6.2 step 1, #245): while the backfill still reads,
    a newer post handled outside the hand-over (here dropped live as not for
    this agent, RT-6.22) does not move the mark past an older post the
    backfill has not queued yet. A server that ends in that moment leaves the
    older post to the next server's backfill, which delivers it."""
    chat = SlowBackfill()
    older, newer = post("older", "@agent-me please look"), post("newer")
    chat.add(older)
    chat.add(newer)
    dial.on_open = [posted(newer)]
    first = guest_inbox(chat)
    connecting = asyncio.create_task(first.ensure_connected())
    await settle()
    assert watermark.at(BOT, WORK) < older["create_at"]
    connecting.cancel()   # the server ends before its backfill queued `older`
    with pytest.raises(asyncio.CancelledError):
        await connecting
    await first.close()
    chat.go.set()
    again = guest_inbox(chat)
    try:
        assert ids(await drain(again)) == ["older"]
    finally:
        await again.close()


async def test_an_edit_seen_before_the_backfill_reaches_its_post_keeps_the_mark_before_it(dial):
    """The read mark (RT-6.2 step 1, #245): a live edit of a post the backfill
    has not reached yet queues that post as the edit (RT-6.12 step 3), and the
    backfill skips it as queued. Until the edit is handed over, a newer post
    handled meanwhile does not move the mark past it: a server that ends
    before the hand-over leaves it to the next server's backfill."""
    chat = SlowBackfill()
    named = post("named", "@agent-me please look")
    chat.add(named)
    dial.on_open = [edited(named, "@agent-me please look, typo fixed")]
    first = guest_inbox(chat)
    try:
        connecting = asyncio.create_task(first.ensure_connected())
        await settle()        # the edit is read before the backfill reaches its post
        chat.go.set()
        await connecting
        newer = post("newer")
        chat.add(newer)
        dial.now.feed(posted(newer))   # not for this agent: dropped, handled
        await settle()
        assert watermark.at(BOT, WORK) < named["create_at"]
    finally:
        await first.close()   # the server ends before the edit is handed over
    again = guest_inbox(chat)
    try:
        assert ids(await drain(again)) == ["named"]
    finally:
        await again.close()


async def test_a_hand_over_while_the_socket_is_down_passes_no_post_that_came_meanwhile(
        chat, dial, inbox):
    """The read mark (RT-6.2 step 1, #244): what is handled while the socket
    is down (here the bot's own post) moves the mark only after the
    reconnect's backfill has queued what came meanwhile: an older mention
    counts as pending until it is handed over, then the mark ends at the
    newest post handled."""
    from mattermost_ai_agent_bus.client import post_of
    await inbox.ensure_connected()
    dial.now.feed(ERROR)
    await until_disconnected(inbox)
    gap = post("gap", "@agent-me while you were away")
    chat.add(gap)
    own = post("own", "on it", user_id=BOT)
    chat.add(own)
    inbox.handled(post_of(own))
    await inbox.ensure_connected()
    assert [r.waiting for r in await unread.channels(chat, {WORK})] == [1]
    assert ids(await drain(inbox)) == ["gap"]
    assert watermark.read(BOT)[WORK].post_id == own["id"]
    assert await unread.channels(chat, {WORK}) == []


async def test_a_hand_over_before_the_reconnects_backfill_passes_no_post_that_came_meanwhile(
        chat, dial, inbox):
    """The read mark (RT-6.2 step 1, #245): at a reconnect the crown is renewed
    before the backfill (RT-6.6 step 6), and that tick may post: the bot's own
    post, handled at once. That hand-over, and one while the socket was down,
    move the mark past no post that came meanwhile: it counts as pending
    until it is handed over, and comes once."""
    from mattermost_ai_agent_bus.client import post_of
    opens = 0

    async def renew_crown() -> None:
        nonlocal opens
        opens += 1
        if opens == 2:   # the reconnect: the tick posts, the server hands it over
            tick = post("tick", "crown renewed", user_id=BOT)
            chat.add(tick)
            inbox.handled(post_of(tick))

    inbox.before_backfill = renew_crown
    await inbox.ensure_connected()
    dial.now.feed(ERROR)
    await until_disconnected(inbox)
    gap = post("gap", "@agent-me while you were away")
    chat.add(gap)
    own = post("own", "on it", user_id=BOT)
    chat.add(own)
    inbox.handled(post_of(own))
    await inbox.ensure_connected()
    assert watermark.at(BOT, WORK) < gap["create_at"]
    assert [r.waiting for r in await unread.channels(chat, {WORK})] == [1]
    assert ids(await drain(inbox)) == ["gap"]


async def test_a_backfill_that_cannot_read_lets_no_hand_over_pass_a_missed_post(
        chat, dial, inbox, monkeypatch):
    """The read mark (RT-6.2 step 1, #244): what was handed over while the
    socket was down waits for a backfill that reads what came meanwhile. One
    that cannot read (Mattermost answers 503) moves the mark past nothing it
    did not see, and the next reconnect's backfill delivers the missed post."""
    from mattermost_ai_agent_bus.client import post_of
    await inbox.ensure_connected()
    dial.now.feed(ERROR)
    await until_disconnected(inbox)
    gap = post("gap", "@agent-me while you were away")
    chat.add(gap)
    own = post("own", "on it", user_id=BOT)
    chat.add(own)
    inbox.handled(post_of(own))
    readable = chat.member_channels

    async def unavailable() -> list[UnreadChannel]:
        raise RuntimeError("503")

    monkeypatch.setattr(chat, "member_channels", unavailable)
    await inbox.ensure_connected()
    assert inbox.connected
    monkeypatch.setattr(chat, "member_channels", readable)
    assert watermark.at(BOT, WORK) < gap["create_at"]
    dial.now.feed(ERROR)
    await until_disconnected(inbox)
    assert ids(await drain(inbox)) == ["gap"]
    assert watermark.read(BOT)[WORK].post_id == own["id"]

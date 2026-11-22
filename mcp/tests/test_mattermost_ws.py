"""Mattermost's WebSocket as the inbox's event source (mattermost_ws.py,
arc42 §5.2): the frames it reads into events, the handshake, the typing
indicator, and the end of a connection, which lets the inbox reconnect
(RT-6.2 step 1)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError

from fakes import FakeChat, FakeEvents, read_frames
from mattermost_ai_agent_bus import compose, mattermost_ws, websocket_inbox
from mattermost_ai_agent_bus.client import MattermostAdapter, post_of, reaction_of, read_links
from mattermost_ai_agent_bus.events import MembershipEvent, PostEvent, ReactionEvent
from mattermost_ai_agent_bus.mattermost_ws import MattermostEvents, event_of

BASE = "https://mm.example/team/pl/"
HUMAN, BOT, CH = "h" * 26, "u" * 26, "c" * 26
LINKED = "if61sbeu1b88xf6wjpimzuh8uy"
POST = {"id": "p" * 26, "user_id": HUMAN, "channel_id": CH, "root_id": "",
        "message": f"see [Post]({BASE}{LINKED}).", "create_at": 1000}
REACTION = {"user_id": HUMAN, "post_id": "a" * 26, "emoji_name": "+1", "create_at": 7}


def frame(msg: dict[str, Any]) -> str:
    return json.dumps(msg)


# --- frames into events ---------------------------------------------------------------

@pytest.mark.parametrize("as_text", [True, False], ids=["post as JSON text", "post as object"])
def test_a_posted_frame_is_a_post_event_with_its_links_read(as_text):
    msg = {"event": "posted", "broadcast": {"channel_id": CH},
           "data": {"channel_type": "D", "post": json.dumps(POST) if as_text else dict(POST)}}
    event = event_of(frame(msg), BASE)
    assert isinstance(event, PostEvent)
    assert event.post == post_of(read_links(dict(POST), BASE))  # RT-6.15
    assert event.post.message == f"see {LINKED}."  # the bus's permalink as a bare id
    assert (event.channel_type, event.edited) == ("D", False)


def test_a_post_edited_frame_is_an_edited_post_event():
    """RT-6.12 step 3; no channel type in the frame: None, the inbox reads the channel."""
    edited = POST | {"edit_at": 2000}
    event = event_of(frame({"event": "post_edited", "data": {"post": json.dumps(edited)}}), "")
    assert isinstance(event, PostEvent)
    assert event.edited and event.channel_type is None and event.post.edit_at == 2000


@pytest.mark.parametrize("as_text", [True, False])
def test_a_reaction_added_frame_is_a_reaction_event_in_its_channel(as_text):
    msg = {"event": "reaction_added", "broadcast": {"channel_id": CH},
           "data": {"reaction": json.dumps(REACTION) if as_text else dict(REACTION)}}
    assert event_of(frame(msg), "") == ReactionEvent(reaction=reaction_of(REACTION), channel_id=CH)


def test_a_reaction_without_a_channel_names_none():
    msg = {"event": "reaction_added", "broadcast": {"channel_id": None},
           "data": {"reaction": json.dumps(REACTION)}}
    assert event_of(frame(msg), "") == ReactionEvent(reaction=reaction_of(REACTION), channel_id="")


@pytest.mark.parametrize("name", ["user_added", "user_removed"])
def test_membership_frames_name_their_users(name):
    """RT-6.22: the user id from the data (user_added) or the broadcast (user_removed)."""
    added = {"event": name, "data": {"user_id": BOT}, "broadcast": {"channel_id": CH}}
    removed = {"event": name, "data": {"channel_id": CH}, "broadcast": {"user_id": BOT}}
    for msg in (added, removed):
        assert event_of(frame(msg), "") == MembershipEvent(user_ids=(BOT,), name=name)
    both = {"event": name, "data": {"user_id": HUMAN}, "broadcast": {"user_id": BOT}}
    assert event_of(frame(both), "") == MembershipEvent(user_ids=(HUMAN, BOT), name=name)


@pytest.mark.parametrize("raw", [
    frame({"event": "typing", "data": {}}),
    frame({"event": "hello", "data": {"server_version": "10"}}),
    frame({"status": "OK", "seq_reply": 1}),
    frame({"event": "channel_viewed", "data": {"channel_id": CH}}),
], ids=["typing", "hello", "a reply", "channel_viewed"])
def test_other_frames_are_ignored(raw):
    assert event_of(raw, "") is None


@pytest.mark.parametrize("raw", [
    "{not json",
    frame({"event": "posted", "data": {"post": "{not json"}}),
    frame({"event": "posted", "data": {"post": "[1, 2]"}}),
    frame({"event": "posted", "data": {"post": {}}}),
    frame({"event": "posted", "data": {}}),
    frame({"event": "reaction_added", "data": {"reaction": "{not json"}}),
    frame({"event": "reaction_added", "data": {"reaction": "[1]"}}),
], ids=["frame", "post", "post no object", "empty post", "no post", "reaction", "reaction no object"])
def test_bad_json_is_ignored(raw):
    assert event_of(raw, "") is None


# --- the connection -------------------------------------------------------------------

class Socket:
    """Mattermost's WebSocket: `replies` answer the handshake (recv), then
    `frames` come by iteration, then it ends or drops with `error`."""

    def __init__(self, *replies: dict[str, Any], frames=(), error: BaseException | None = None):
        self.replies = [json.dumps(r) for r in replies]
        self.frames = [json.dumps(f) for f in frames]
        self.error = error
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def recv(self) -> str:
        return self.replies.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if self.frames:
            return self.frames.pop(0)
        if self.error is not None:
            raise self.error
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


def _client(token: str | None = "tok") -> MattermostAdapter:
    return MattermostAdapter(base_url="https://mm.example", token=token, team_name="team")


@pytest.fixture
def dialled(monkeypatch):
    """The sockets dialled, by URL; the next one to hand out goes in `next`."""
    out: dict[str, Any] = {"urls": [], "next": None}

    async def dial(url):
        out["urls"].append(url)
        return out["next"]

    monkeypatch.setattr(mattermost_ws, "ws_connect", dial)
    return out


@pytest.mark.parametrize("reply", [{"event": "hello"}, {"event": "authorized"}, {"status": "OK"}])
async def test_open_authenticates_as_the_bot(dialled, reply):
    """The challenge carries the bot's token as seq 1; frames before the
    server's answer are dropped, the answer ends the handshake."""
    dialled["next"] = sock = Socket({"event": "status_change"}, reply, {"event": "later"})
    events = MattermostEvents(_client())
    await events.open()
    assert dialled["urls"] == ["wss://mm.example/api/v4/websocket"] and events.connected
    assert sock.sent == [{"seq": 1, "action": "authentication_challenge", "data": {"token": "tok"}}]
    assert sock.replies == [json.dumps({"event": "later"})]


async def test_open_without_a_token_dials_nothing(dialled):
    with pytest.raises(RuntimeError, match="bot token required"):
        await MattermostEvents(_client(None)).open()
    assert dialled["urls"] == []


async def test_a_silent_server_times_the_handshake_out(dialled, monkeypatch):
    class Silent(Socket):
        async def recv(self) -> str:
            await asyncio.Event().wait()
            return ""

    monkeypatch.setattr(mattermost_ws, "HELLO_SEC", 0.01)
    dialled["next"] = Silent()
    events = MattermostEvents(_client())
    with pytest.raises(TimeoutError):
        await events.open()
    assert events.connected  # the inbox closes it (connect)


async def test_typing_numbers_on_after_the_challenge(dialled):
    """RT-6.5: one user_typing message per call, its seq after the challenge's."""
    dialled["next"] = sock = Socket({"event": "hello"})
    events = MattermostEvents(_client())
    await events.open()
    await events.typing(CH, "r" * 26)
    await events.typing(CH, "")
    assert sock.sent[1:] == [
        {"action": "user_typing", "seq": 2, "data": {"channel_id": CH, "parent_id": "r" * 26}},
        {"action": "user_typing", "seq": 3, "data": {"channel_id": CH, "parent_id": ""}}]


async def test_typing_unconnected_raises():
    with pytest.raises(ConnectionError):
        await MattermostEvents(_client()).typing(CH, "")


async def _opened(dialled, sock: Socket) -> MattermostEvents:
    dialled["next"] = sock
    events = MattermostEvents(_client())
    await events.open()
    return events


async def test_the_stream_gives_the_events_and_its_end_disconnects(dialled):
    """RT-6.2 step 1: a clean close ends the stream, and the source reads as
    not connected, so the next read reconnects; the frames it ignores are left out."""
    posted = {"event": "posted", "data": {"post": json.dumps(POST)}}
    sock = Socket({"event": "hello"}, frames=[{"event": "typing"}, posted,
                                               {"event": "user_added", "data": {"user_id": BOT}}])
    events = await _opened(dialled, sock)
    got = [e async for e in events.events()]
    assert [type(e) for e in got] == [PostEvent, MembershipEvent]
    assert not events.connected and not sock.closed


async def test_a_drop_raises_and_disconnects(dialled):
    """The regression the inbox once had: a dropped socket left set made it deaf."""
    events = await _opened(dialled, Socket({"event": "hello"}, error=ConnectionClosedError(None, None)))
    with pytest.raises(ConnectionClosedError):
        async for _ in events.events():
            pass
    assert not events.connected


async def test_aclose_disconnects_and_a_cancelled_read_does_not(dialled):
    """A reader that stops reading (an event failed in the inbox) ends the
    stream; a cancelled one leaves the socket to close()."""
    class Waits(Socket):
        async def __anext__(self) -> str:
            await asyncio.Event().wait()
            return ""

    events = await _opened(dialled, Waits({"event": "hello"}))
    stream = events.events()
    read = asyncio.create_task(stream.__anext__())
    await asyncio.sleep(0)
    read.cancel()
    with pytest.raises(asyncio.CancelledError):
        await read
    assert events.connected
    await stream.aclose()
    assert not events.connected


class Counting(Socket):
    """A socket that counts its closes: what the tests assert on, not the
    fake's `closed` flag, which the real ClientConnection does not have."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1
        await super().close()


async def test_aclose_closes_the_socket_it_forgets(dialled):
    """#250: a stream the inbox ends (an event failed in its handler) closes
    the socket once, and a later stream closes nothing, its socket being gone."""
    events = await _opened(dialled, sock := Counting({"event": "hello"}))
    await events.events().aclose()
    assert sock.closes == 1 and not events.connected
    await events.events().aclose()  # a stream with no socket: closes nothing
    assert sock.closes == 1


async def test_a_dropped_socket_is_not_closed_on_the_way_out(dialled):
    """#250: the stream ends by a failure (the socket dropped), so there is
    nothing left to close: aclose() closes nothing."""
    events = await _opened(dialled, sock := Counting(
        {"event": "hello"}, error=ConnectionClosedError(None, None)))
    with pytest.raises(ConnectionClosedError):
        async for _ in events.events():
            pass
    assert not events.connected
    await events.events().aclose()
    assert sock.closes == 0


async def test_open_closes_a_socket_still_open_before_dialling(dialled):
    """#250: open() over a source that is still connected closes the old
    socket first, so there is never a second connection beside the first."""
    old = Counting({"event": "hello"})
    events = await _opened(dialled, old)
    new = Counting({"event": "hello"})
    dialled["next"] = new
    await events.open()
    assert old.closes == 1 and new.closes == 0 and events.connected
    assert len(dialled["urls"]) == 2


async def test_an_old_stream_does_not_close_a_new_socket(dialled):
    """#250: a stream keeps the socket it was opened on, so ending it after the
    source dialled again closes nothing: the new connection stays."""
    events = await _opened(dialled, old := Counting({"event": "hello"}))
    stream = events.events()
    fresh = Counting({"event": "hello"})
    dialled["next"] = fresh
    await events.open()  # old closed here, fresh dialled
    await stream.aclose()
    assert old.closes == 1 and fresh.closes == 0 and events.connected


async def test_closing_wait_bounded(dialled, monkeypatch):
    """#250: a socket whose close never finishes costs CLOSE_SEC at most, then
    the reader goes on: the inbox is not held up by a discarded socket."""
    monkeypatch.setattr(mattermost_ws, "CLOSE_SEC", 0.05)

    class Hangs(Socket):
        async def close(self) -> None:
            await asyncio.Event().wait()

    events = await _opened(dialled, Hangs({"event": "hello"}))
    started = asyncio.get_event_loop().time()
    await events.events().aclose()  # returns, no exception
    assert asyncio.get_event_loop().time() - started < 1.0
    assert not events.connected


async def test_the_close_of_a_real_client_connection_does_not_raise(dialled):
    """#250: against websockets' own ClientConnection, which has no `closed`
    attribute at all: aclose() must not look for one."""
    from websockets.asyncio.client import ClientConnection
    from websockets.protocol import State

    ws = ClientConnection.__new__(ClientConnection)  # never dialled
    ws.protocol = type("P", (), {"state": State.CLOSED})()
    ws.close_connection_task = None
    events = MattermostEvents(_client())
    events._ws = ws
    await events.events().aclose()  # no AttributeError
    assert not events.connected


async def test_close_closes_the_socket(dialled):
    events = await _opened(dialled, sock := Socket({"event": "hello"}))
    await events.close()
    assert sock.closed and not events.connected
    await events.close()  # nothing left to close


def test_the_composition_root_builds_mattermosts_source():
    """An inbox handed no source builds Mattermost's at its first connect;
    a client without the server's address and the bot's token has none."""
    client = _client()
    source = compose.event_source(client)
    assert isinstance(source, MattermostEvents) and source.client is client
    inbox = websocket_inbox.WebSocketInbox(client=client)
    assert inbox._source is None and not inbox.connected
    built = inbox._events()
    assert isinstance(built, MattermostEvents) and built.client is client and inbox._events() is built
    with pytest.raises(TypeError, match="no source for this chat"):
        compose.event_source(FakeChat())


def test_the_wrapped_adapter_still_gives_a_source():
    """#256 review: the root hands out compose.Remembered, not the bare adapter,
    and `isinstance` against a runtime_checkable Protocol does not see
    `__getattr__` — a wrapper with only that would have left the inbox without
    a source, and the bus deaf, in every process."""
    wrapped = compose.Remembered(_client())
    assert isinstance(wrapped, mattermost_ws.Wire)
    source = compose.event_source(wrapped)
    assert isinstance(source, MattermostEvents)


def test_a_handed_source_is_the_one_attribute():
    events = FakeEvents()
    inbox = websocket_inbox.WebSocketInbox(client=FakeChat(), source=events)
    assert inbox._events() is events and "source" not in vars(inbox)


# --- the posts the inbox makes up -------------------------------------------------------

@pytest.mark.parametrize("raw", [
    {"id": "k", "root_id": "a" * 26, "channel_id": CH, "user_id": HUMAN, "create_at": 7,
     "message": ":+1:"},
    {"id": "k", "root_id": "a" * 26, "channel_id": "", "user_id": HUMAN, "create_at": 0,
     "message": ":white_check_mark:"},
    {"id": "", "message": "Stall: 2 open for PR #12 on branch fix/x", "create_at": 99},
    {"id": "", "message": "\U0001F64B @dembaca — **Merge   it?**", "create_at": 1},
], ids=["reaction", "reaction without channel", "stall", "ask text"])
def test_a_made_up_post_is_what_post_of_built(raw):
    """The inbox builds its reaction and stall posts without client.py
    (RT-6.11, RT-6.19): every field as client.post_of reads the same JSON."""
    fields = {k: v for k, v in raw.items() if k in ("id", "root_id", "channel_id", "user_id",
                                                   "create_at", "message")}
    made, built = websocket_inbox._made_up(raw, **fields), post_of(raw)
    for f in dataclasses.fields(made):
        assert getattr(made, f.name) == getattr(built, f.name), f.name


async def test_the_inbox_keeps_the_channel_type_the_event_named():
    """A DM's live post: channel_type(event) is the event's D, read from no
    frame in the inbox (the edit count and the gate's answer read it)."""
    inbox = websocket_inbox.WebSocketInbox(client=FakeChat(), source=FakeEvents())
    inbox._bot_user_id, inbox._bot_username = BOT, "agent-me"
    await read_frames(inbox, {"event": "posted", "data": {"channel_type": "D", "post": json.dumps(POST)}})
    [queued] = list(inbox._queue._queue)
    assert queued.kind == "dm" and queued.raw is None and websocket_inbox.channel_type(queued) == "D"

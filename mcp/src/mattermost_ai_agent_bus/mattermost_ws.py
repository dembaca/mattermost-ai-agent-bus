"""Mattermost's WebSocket as the inbox's event source (arc42 §5.2).

The adapter side of events.EventSource: dials the server's WebSocket,
authenticates with the bot's token, numbers what it sends (seq), reads the
frames into the events of events.py and sends the typing indicator (RT-6.5).
It reads Mattermost's frames and, through client.post_of and
client.reaction_of, its post and reaction JSON; the inbox reads only events.
compose.event_source builds it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Protocol, runtime_checkable

from websockets.asyncio.client import connect as ws_connect

from .client import post_of, reaction_of, read_links, ws_url_from_http
from .events import ChatEvent, MembershipEvent, PostEvent, ReactionEvent

log = logging.getLogger(__name__)

# Events that change the bot's channels (RT-6.22): the inbox reads its
# watched and guest channels again when they name the bot.
MEMBERSHIP = ("user_added", "user_removed")

# RT-6.12 step 3: Mattermost sends an edit as post_edited (same id, edit_at set).
EDITED = "post_edited"

# How long the server has to answer the authentication challenge.
HELLO_SEC = 15

# How long closing a socket the source no longer wants may take (#250): its
# close handshake waits up to close_timeout (10 s), and the inbox connects
# again meanwhile. A timeout there is no loss: the transport is dropped.
CLOSE_SEC = 2.0


def event_of(raw: str | bytes, permalink_base: str) -> ChatEvent | None:
    """One WebSocket frame as the inbox reads it: a post (posted,
    post_edited), a reaction (reaction_added) or a membership change
    (user_added, user_removed). None for any other frame, bad JSON, and a
    post or reaction that is no JSON object. permalink_base: the bus's
    permalinks in a post become bare ids (RT-6.15)."""
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if msg.get("event") == "reaction_added":
        return _reaction(msg)
    if msg.get("event") in MEMBERSHIP:
        data, where = msg.get("data") or {}, msg.get("broadcast") or {}
        named = (data.get("user_id"), where.get("user_id"))
        return MembershipEvent(user_ids=tuple(u for u in named if isinstance(u, str) and u),
                               name=str(msg.get("event")))
    if msg.get("event") in ("posted", EDITED):
        return _post(msg, permalink_base)
    return None


def _post(msg: dict[str, Any], permalink_base: str) -> PostEvent | None:
    """A posted or post_edited frame; its post comes as JSON text or as an object."""
    data = msg.get("data") or {}
    post_raw = data.get("post")
    if isinstance(post_raw, str):
        try:
            found = json.loads(post_raw)
        except json.JSONDecodeError:
            return None
    elif isinstance(post_raw, dict):
        found = post_raw
    else:
        return None
    if not isinstance(found, dict) or not found:
        return None
    # RT-6.15: the bus's permalinks as bare ids, odd links written out.
    post = post_of(read_links(found, permalink_base))
    return PostEvent(post=post, channel_type=data.get("channel_type"),
                     edited=msg.get("event") == EDITED)


def _reaction(msg: dict[str, Any]) -> ReactionEvent | None:
    """A reaction_added frame: its reaction as JSON text or an object, its
    channel from the broadcast."""
    raw = (msg.get("data") or {}).get("reaction")
    try:
        reaction = json.loads(raw) if isinstance(raw, str) else (raw or {})
    except json.JSONDecodeError:
        return None
    cid = (msg.get("broadcast") or {}).get("channel_id", "")
    if not isinstance(reaction, dict):
        return None
    return ReactionEvent(reaction=reaction_of(reaction), channel_id=str(cid or ""))


@runtime_checkable
class Wire(Protocol):
    """What the source needs of its chat client, besides the port: the
    server's address and the bot's token (MattermostAdapter has them)."""

    @property
    def base_url(self) -> str: ...

    @property
    def token(self) -> str | None: ...

    @property
    def permalink_base(self) -> str: ...


async def _close_quietly(ws: Any) -> None:
    """Close a socket the source no longer tracks (#250), best effort: an error
    while closing is logged and the caller goes on either way, and waiting is
    bounded by CLOSE_SEC, so a socket that does not answer costs the inbox no
    more than that before it connects again. `None`: nothing to do.

    No check for an already closed socket: websockets' ClientConnection has no
    `closed` (only the legacy API had), and `close()` is harmless to repeat."""
    if ws is None:
        return
    try:
        await asyncio.wait_for(ws.close(), timeout=CLOSE_SEC)
    except asyncio.CancelledError:
        raise  # the reader was cancelled: close() owns the socket then
    except Exception as exc:  # noqa: BLE001 — closing must never fail the reader
        log.info("closing the WebSocket: %s", exc)


class MattermostEvents:
    """events.EventSource over Mattermost's WebSocket, as the bot of `client`."""

    def __init__(self, client: Wire) -> None:
        self.client = client
        self._ws: Any = None
        # Every message sent on any of this source's connections has its own
        # number (Mattermost answers by it).
        self._seq = 1

    @property
    def connected(self) -> bool:
        return self._ws is not None

    async def open(self) -> None:
        """Dial the WebSocket and authenticate; raises RuntimeError without a
        bot token. Connected from the dial on, the handshake included. A
        socket still open from before is closed first (#250), so the source
        never holds two connections."""
        if not self.client.token:
            raise RuntimeError("bot token required for WebSocket")
        previous, self._ws = self._ws, None
        await _close_quietly(previous)
        self._ws = await ws_connect(ws_url_from_http(self.client.base_url))
        await self._authenticate(self._ws)

    async def _authenticate(self, ws: Any) -> None:
        """The authentication challenge, then the server's hello, authorized
        or OK within HELLO_SEC; other frames before it are dropped."""
        auth = {
            "seq": self._seq,
            "action": "authentication_challenge",
            "data": {"token": self.client.token},
        }
        self._seq += 1
        await ws.send(json.dumps(auth))
        deadline = asyncio.get_event_loop().time() + HELLO_SEC
        while asyncio.get_event_loop().time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=HELLO_SEC)
            msg = json.loads(raw)
            event = msg.get("event")
            if event in ("hello", "authorized") or msg.get("status") == "OK":
                break

    def events(self) -> _Frames:
        return _Frames(self)

    async def typing(self, channel_id: str, parent_id: str) -> None:
        """One user_typing message; Mattermost shows it for a few seconds."""
        ws = self._ws
        if ws is None:
            raise ConnectionError("WebSocket not connected")
        data = {"channel_id": channel_id, "parent_id": parent_id}
        msg = {"action": "user_typing", "seq": self._seq, "data": data}
        self._seq += 1
        await ws.send(json.dumps(msg))

    async def close(self) -> None:
        """Not connected from here on, then the socket closed."""
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()


class _Frames:
    """The open socket's frames as events (events.EventStream). Its end, a
    drop or aclose() drop the socket, so the next read reconnects: a dead
    socket left set would leave the inbox deaf with no error anywhere. It
    keeps the socket it was opened on, and only lets go of that one, never of
    a socket the source dialled since (#250)."""

    def __init__(self, source: MattermostEvents) -> None:
        self._source = source
        self._ws = source._ws
        self._frames = aiter(self._ws) if self._ws is not None else None

    def __aiter__(self) -> _Frames:
        return self

    async def __anext__(self) -> ChatEvent:
        if self._frames is None:
            raise StopAsyncIteration
        try:
            while True:
                event = event_of(await anext(self._frames), self._source.client.permalink_base)
                if event is not None:
                    return event
        except asyncio.CancelledError:
            raise  # close() owns the socket from here on
        except Exception:
            self._end()  # a clean close (StopAsyncIteration) or a drop
            raise

    async def aclose(self) -> None:
        """Stop reading and close the socket this stream leaves behind, waiting
        at most CLOSE_SEC for it: best effort, no exception out, and nothing at
        all when the source already let this socket go. See _close_quietly."""
        ws = self._source._ws if self._source._ws is self._ws else None
        self._end()
        await _close_quietly(ws)

    def _end(self) -> None:
        if self._source._ws is self._ws:
            self._source._ws = None

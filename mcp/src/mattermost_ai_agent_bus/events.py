"""Chat events: what the inbox reads live from its chat (arc42 §5.2).

The WebSocket inbox (websocket_inbox.py) takes its live events from an
EventSource and reads no wire format: mattermost_ws.MattermostEvents reads
Mattermost's WebSocket for it, and a second chat brings its own source,
chosen in compose.event_source. The events carry bus types (model.py); a
post's wire message stays in `raw`, for the tools asked for it (raw=true).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

from .model import Post, Reaction


@dataclass(frozen=True)
class PostEvent:
    """A post, new or edited (RT-6.12 step 3), its links read as the bus
    reads them (RT-6.15). channel_type: D, G, O or P where the event names
    it, else None (the inbox reads the channel). raw: the whole event as the
    chat sent it."""

    post: Post
    channel_type: str | None
    edited: bool
    raw: Mapping[str, Any]


@dataclass(frozen=True)
class ReactionEvent:
    """A reaction added to a post; channel_id is "" when the event names none."""

    reaction: Reaction
    channel_id: str


@dataclass(frozen=True)
class MembershipEvent:
    """Someone was added to a channel or removed from one (RT-6.22): the
    user ids the event names, and its name (for the log)."""

    user_ids: tuple[str, ...]
    name: str


ChatEvent: TypeAlias = PostEvent | ReactionEvent | MembershipEvent


class EventStream(Protocol):
    """One connection's events, in the order they came. It ends when the
    connection closes (the iteration stops) or drops (it raises), and when
    the reader stops reading it (aclose): the source then reads as not
    connected, so the next read connects again. A cancelled read leaves the
    source as it is: close() owns the connection then."""

    def __aiter__(self) -> EventStream: ...

    async def __anext__(self) -> ChatEvent: ...

    async def aclose(self) -> None: ...


class EventSource(Protocol):
    """Where the inbox's live events come from: the inbox opens it, reads its
    stream until the connection ends, opens it again on its next read, and
    shows the bot typing through it (RT-6.5)."""

    @property
    def connected(self) -> bool:
        """Whether a connection is open, so an event now reaches the inbox:
        from the moment open() starts connecting until the stream ends or
        close() runs."""
        ...

    async def open(self) -> None:
        """Connect and authenticate as the bot; raises when either fails.
        The inbox closes the source after a failed or cancelled open."""
        ...

    def events(self) -> EventStream:
        """The open connection's events, until it ends."""
        ...

    async def typing(self, channel_id: str, parent_id: str) -> None:
        """Show the bot typing in the channel, in thread parent_id ("" for the
        channel view), for a few seconds; raises when it cannot."""
        ...

    async def close(self) -> None:
        """Close the connection; afterwards the source reads as not connected."""
        ...

"""Bus types for what Mattermost keeps in its state carriers (arc42 §5.1).

Post props, custom status, unread counters and the profile description are
read and written in client.py, a2a.py and lease.py only; the rest of the
server gets these values instead. A post is a Post, built by client.post_of
from Mattermost's JSON: the fields the bus reads, typed.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class Post:
    """A post as the bus reads it (arc42 §5.1). Built by client.post_of, from
    Mattermost's JSON or a post the server makes up (a reaction, a stall); the
    rest of the server reads these fields, never the JSON's keys.

    kind, intent and to come from the bus's props (a2a.py), kind from the text
    when there are none. Anyone can write props: from_webhook says who did not
    write the post, intent and to say what it is for, never who wrote it.
    props and raw are read-only escape hatches: raw is the whole JSON, for
    tools asked for it (raw=true); props for the adapter modules."""

    id: str = ""
    channel_id: str = ""
    root_id: str = ""
    user_id: str = ""
    create_at: int = 0
    edit_at: int = 0
    delete_at: int = 0
    message: str = ""
    type: str = ""  # Mattermost's system post types; "" for an ordinary post
    reply_count: int = 0
    from_webhook: bool = False
    kind: str | None = None
    intent: str | None = None
    to: str = ""  # "all" (a2a.ALL), else ""
    reactions: tuple[Mapping[str, Any], ...] = field(default=(), compare=False)
    props: Mapping[str, Any] = field(default=_EMPTY, compare=False, repr=False)
    raw: Mapping[str, Any] = field(default=_EMPTY, compare=False, repr=False)

    @property
    def thread(self) -> str:
        """The thread the post is in: its root, or itself when it is one."""
        return self.root_id or self.id

    def json(self) -> dict[str, Any]:
        """The JSON it was built from, for a tool asked for raw=true."""
        return dict(self.raw)


@dataclass(frozen=True)
class Thread:
    """A thread as client.thread reads it: its root, the post ids in
    Mattermost's order and the posts in that order."""

    root_id: str
    order: tuple[str, ...] = ()
    posts: tuple[Post, ...] = ()

    def json(self) -> dict[str, Any]:
        """The JSON get_thread returns with raw=true."""
        return {"root_id": self.root_id, "order": list(self.order),
                "posts": [p.json() for p in self.posts]}


@dataclass(frozen=True)
class RosterEntry:
    """A reachable agent of the work channel (RT-6.6). status is a word:
    working, listening, idle, bus, or what the agent's status text says."""

    username: str
    status: str = ""
    coordinator: bool = False
    crown: bool = False


@dataclass(frozen=True)
class UnreadChannel:
    """A channel with something for this agent, to be read from `since` (ms).
    channel_type is Mattermost's, as the WebSocket names it too: D, G, O or P."""

    channel_id: str
    channel_type: str = "O"
    since: int = 0
    # The counters: mentions (in a DM or group: unread messages) and unread
    # posts. Not the channel's identity, so comparisons leave them out.
    pending: int = field(default=0, compare=False)
    unread: int = field(default=0, compare=False)

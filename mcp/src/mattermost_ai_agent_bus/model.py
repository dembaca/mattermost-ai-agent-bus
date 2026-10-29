"""Bus types for what Mattermost keeps in its state carriers (arc42 §5.1).

Post props, custom status, channel activity (unread counters, last post) and the profile description are
read and written in client.py, a2a.py and lease.py only; the rest of the
server gets these values instead. A post is a Post, built by client.post_of
from Mattermost's JSON: the fields the bus reads, typed. The bot account, a
channel, a channel's accounts and reactions come through the port the same
way (Me, Channel, ChannelUser, Reaction), built in client.py.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class Reaction:
    """A reaction on a post, from the post's metadata, port.reactions or the
    WebSocket (client.reaction_of)."""

    user_id: str = ""
    post_id: str = ""
    emoji_name: str = ""
    create_at: int = 0


@dataclass(frozen=True)
class FileInfo:
    """A file attached to a post (RT-6.23), from the post's metadata or
    port.file_info: what Mattermost keeps about it. extension is lower case
    without the dot; size in bytes; width and height for an image (0 else).
    user_id is who uploaded it, which Mattermost makes the post's author."""

    id: str = ""
    name: str = ""
    extension: str = ""
    size: int = 0
    mime_type: str = ""
    width: int = 0
    height: int = 0
    has_preview_image: bool = False
    post_id: str = ""
    user_id: str = ""


@dataclass(frozen=True)
class Me:
    """The bot account (users/me), as port.get_me gives it. raw is the JSON,
    for the get_me tool asked for raw=true."""

    id: str = ""
    username: str = ""
    raw: Mapping[str, Any] = field(default=_EMPTY, compare=False, repr=False)

    def json(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class Channel:
    """A channel as port.get_channel gives it: its name and Mattermost's type
    (D, G, O or P). raw is the JSON, for wait_for_events asked for raw=true."""

    id: str = ""
    name: str = ""
    type: str = ""
    raw: Mapping[str, Any] = field(default=_EMPTY, compare=False, repr=False)

    def json(self) -> dict[str, Any]:
        return dict(self.raw)


@dataclass(frozen=True)
class ChannelUser:
    """An account in a channel (port.channel_users), for the roster (lease.py):
    deleted is Mattermost's delete_at set; custom_status the status the
    account set, as Mattermost keeps it ({} without one)."""

    id: str = ""
    username: str = ""
    create_at: int = 0
    deleted: bool = False
    custom_status: Mapping[str, Any] = field(default=_EMPTY, compare=False)


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
    pr: int | None = None  # the PR it names: props, else its text (RT-6.14)
    branch: str | None = None
    crowned: bool = False  # the server wrote it while its author held the crown
    reactions: tuple[Reaction, ...] = field(default=(), compare=False)
    files: tuple[FileInfo, ...] = field(default=(), compare=False)  # RT-6.23
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
    """A reachable agent of the home (RT-6.6). status is a word: working,
    listening, idle, bus, or what the agent's status text says. crown_of: the
    other home whose crown it holds, when it is here as a guest (ADR-002)."""

    username: str
    status: str = ""
    coordinator: bool = False
    crown: bool = False
    crown_of: str = ""
    # Its bot's id, to read its description by (ADR-001); not the row's identity.
    user_id: str = field(default="", compare=False)


@dataclass(frozen=True)
class UnreadChannel:
    """A channel of this bot, with when its newest post came (ms; 0 when
    Mattermost does not say) and Mattermost's counters. Which of its posts
    wait is the bus's own read watermark's to say (watermark.py, RT-6.2).
    channel_type is Mattermost's, as the WebSocket names it too: D, G, O or P."""

    channel_id: str
    channel_type: str = "O"
    # Not the channel's identity, so comparisons leave them out. The counters:
    # mentions (in a DM or group: unread messages) and unread posts.
    last_post_at: int = field(default=0, compare=False)
    pending: int = field(default=0, compare=False)
    unread: int = field(default=0, compare=False)
    # Its name (town-square, …), where Mattermost gives it (RT-6.22 step 1).
    name: str = field(default="", compare=False)

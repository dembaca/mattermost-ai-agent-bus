"""The tests' stand-in for the chat port (port.ChatPort, arc42 §5.1).

FakeChat answers from what a test hands it and records what is posted. It is
a typed subset of the port, not a Mattermost emulator: it has only the members
the tests call, each with the port's signature (test_adapter_boundary checks
that, and mypy checks this file). A test that needs more of the port adds the
member here, with the port's signature; what is no port member (the adapter's
own attributes) goes into a subclass in the test.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping
from typing import Any

from mattermost_ai_agent_bus import owners
from mattermost_ai_agent_bus.client import post_of, reaction_of
from mattermost_ai_agent_bus.model import (Channel, ChannelUser, Me, Post, Reaction, Thread,
                                           UnreadChannel)

BOT = "u" * 26
WORK = "c" * 26


class FakeChat:
    """A ChatPort for tests: a bot, its work channel, posts, people, reactions.

    posts: Post or post JSON, newest first, as channel_posts returns them;
    posts_after returns them all. post_id: the id a post of the bot gets.
    reactions: post id -> Reaction or reaction JSON. names: user id ->
    username; agents: the user ids that are agents; trusted: user id ->
    username of the trusted humans; descriptions: user id -> bot description
    (missing: not readable, ADR-003); native_owners: user id -> username of
    the bot's native owner (missing: not readable, WP3); channel: every
    channel's name.
    members, presence: the accounts in every channel with their custom
    status, and who is online (the lease's roster); status: the own custom
    status written last; header: the channel header.
    """

    token: str | None = "t"
    session = None
    work_channel_name = "agents"

    def __init__(self, *, uid: str = BOT, username: str = "agent-me", created: int = 0,
                 posts: Iterable[Post | Mapping[str, Any]] = (),
                 reactions: Mapping[str, Iterable[Reaction | Mapping[str, Any]]] | None = None,
                 names: Mapping[str, str] | None = None, agents: Collection[str] = (),
                 trusted: Mapping[str, str] | None = None,
                 descriptions: Mapping[str, str] | None = None,
                 native_owners: Mapping[str, str] | None = None, channel: str = "agents",
                 channels: Iterable[UnreadChannel] = (), post_id: str = "new") -> None:
        self.me = Me(id=uid, username=username, raw={"id": uid, "username": username})
        self.created = created
        # As given; a test may change them, they are read as Post and Reaction.
        self.posts: list[Post | Mapping[str, Any]] = list(posts)
        self.reacts: dict[str, list[Reaction | Mapping[str, Any]]] = {
            pid: list(rs) for pid, rs in (reactions or {}).items()}
        self.names = dict(names or {})
        self._agents = set(agents)
        self.trusted = dict(trusted or {})
        self.descriptions = dict(descriptions or {})
        self.native_owners = dict(native_owners or {})
        self.channel = channel
        self.channels = list(channels)
        self.post_id = post_id
        self.members: list[ChannelUser] = []
        self.presence: dict[str, str] = {}
        self.status: dict[str, str] | None = None
        self.header = ""
        self.dms: set[str] = set()
        self.posted: list[dict[str, Any]] = []
        self.calls: list[tuple[str, int | None, int | None]] = []

    def _posts(self) -> list[Post]:
        return [p if isinstance(p, Post) else post_of(p) for p in self.posts]

    async def get_me(self) -> Me:
        return self.me

    async def account_created(self) -> int:
        return self.created

    async def work_channel_id(self) -> str:
        return WORK

    async def join_channel(self, channel_name: str) -> str:
        """Any channel is the work channel; a name not in `channel` is missing."""
        if channel_name != self.channel:
            raise LookupError(f"no channel {channel_name}")
        return WORK

    async def get_channel(self, channel_id: str) -> Channel:
        """Ids in `dms` are DMs; every other channel is open and named `channel`."""
        return Channel(id=channel_id, name=self.channel, type="D" if channel_id in self.dms else "O")

    async def post_channel_id(self, post_id: str) -> str:
        """The channel of a post in `posts`, else the work channel."""
        return next((p.channel_id for p in self._posts() if p.id == post_id and p.channel_id), WORK)

    async def usernames(self, user_ids: set[str]) -> dict[str, str]:
        return {i: self.names[i] for i in user_ids if i in self.names}

    async def agents(self, user_ids: set[str]) -> set[str]:
        return set(user_ids) & self._agents

    async def trusted_humans(self) -> dict[str, str]:
        return dict(self.trusted)

    async def bot_description(self, user_id: str) -> str | None:
        return self.descriptions.get(user_id)

    async def agent_owner(self, user_id: str) -> str | None:
        """The adapter's rule (owners.decide) on `native_owners` and `descriptions`."""
        return owners.decide(self.native_owners.get(user_id), self.descriptions.get(user_id), user_id)

    async def member_channels(self) -> list[UnreadChannel]:
        return list(self.channels)

    async def channel_header(self, channel_id: str) -> str:
        return self.header

    async def set_channel_header(self, channel_id: str, header: str) -> None:
        self.header = header

    async def channel_users(self, channel_id: str) -> list[ChannelUser]:
        return list(self.members)

    async def presences(self, user_ids: list[str]) -> dict[str, str]:
        return {i: self.presence[i] for i in user_ids if i in self.presence}

    async def set_presence(self, user_id: str, status: str) -> None:
        self.presence[user_id] = status

    async def set_custom_status(self, emoji: str, text: str, expires_at: str) -> None:
        """Kept as Mattermost keeps it, and shown on the own member row."""
        self.status = {"emoji": emoji, "text": text, "duration": "date_and_time",
                       "expires_at": expires_at}
        self.members = [ChannelUser(m.id, m.username, m.create_at, custom_status=self.status)
                        if m.id == self.me.id else m for m in self.members]

    async def clear_custom_status(self) -> None:
        self.status = None

    async def post(self, message: str, *, channel_id: str | None = None,
                   root_id: str | None = None, props: dict[str, Any] | None = None) -> Post:
        """Records the post; it comes back with the id post_id."""
        self.posted.append({"message": message, "channel_id": channel_id,
                            "root_id": root_id, "props": props})
        return post_of({"id": self.post_id, "message": message, "user_id": self.me.id,
                        "channel_id": channel_id or WORK, "root_id": root_id or ""})

    async def thread(self, post_id: str) -> Thread:
        return Thread(post_id, posts=tuple(p for p in self._posts() if post_id in (p.id, p.root_id)))

    async def channel_posts(self, channel_id: str, *, per_page: int | None = None,
                            since_ms: int | None = None) -> list[Post]:
        self.calls.append(("posts", per_page, since_ms))
        if since_ms is not None:
            return [p for p in self._posts() if p.create_at >= since_ms]
        return self._posts()[:per_page]

    async def posts_after(self, since_ms: int, *, channel_id: str | None = None,
                          max_pages: int = 25) -> list[Post]:
        """All posts: the test hands only what came after."""
        return self._posts()

    async def reactions(self, post_id: str) -> list[Reaction]:
        return [r if isinstance(r, Reaction) else reaction_of(r) for r in self.reacts.get(post_id, [])]


class HookClient(FakeChat):
    """FakeChat with what the hook API reads of the adapter beyond the port:
    the channel names it resolves the watched channels from, none here, so
    the work channel is the one watched."""

    project_channel_name = ""
    channel_name = ""

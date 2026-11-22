"""The tests' stand-ins for the chat port (port.ChatPort, arc42 §5.1) and the
registration port (registration.RegistrationPort, arc42 §5.2).

FakeChat answers from what a test hands it and records what is posted. It is
a typed subset of the port, not a Mattermost emulator: it has only the members
the port has, each with the port's signature (test_adapter_boundary checks
both, and mypy checks this file). A member the port gains is added here, with
the port's signature; what is no port member (the adapter's own attributes)
goes into a subclass in the test. FakeRegistrar does the same for the
registration port.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, cast

from mattermost_ai_agent_bus import owners
from mattermost_ai_agent_bus.client import dm_partner, post_of, reaction_of
from mattermost_ai_agent_bus.ids import mm_name
from mattermost_ai_agent_bus.model import (Channel, ChannelUser, FileInfo, Me, Post, Reaction,
                                           Thread, UnreadChannel)
from mattermost_ai_agent_bus.session_file import SessionInfo

if TYPE_CHECKING:
    from mattermost_ai_agent_bus.port import ChatPort
    from mattermost_ai_agent_bus.registration import RegistrationPort

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
    the bot's native owner (missing: not readable, WP3); attestations: user
    id -> the attestation its account carries (RT-6.24); channel: every
    channel's name. props: the own account's props.
    members, presence: the accounts in every channel with their custom
    status, and who is online (the lease's roster); status: the own custom
    status written last; header: the channel header. bots: the bot accounts
    by lower-case username (RT-6.21), in the channel or not. people: user
    id -> username of the accounts that are people, not bots (human_id,
    humans_among); none by default, so a mention stays as written.
    dms: the DM channels, direct_channel_with adds its own; viewed: the
    channels marked read; sent_sync: what post_sync and post_by_name_sync
    sent. No lobby of its own (it is the home), no channel names, no
    permalink base: links stay as written.
    """

    token: str | None = "t"
    session = None
    work_channel_name = "agents"
    channel_name = ""
    project_channel_name = ""
    permalink_base = ""
    trusted_source = ""

    def __init__(self, *, uid: str = BOT, username: str = "agent-me", created: int = 0,
                 posts: Iterable[Post | Mapping[str, Any]] = (),
                 reactions: Mapping[str, Iterable[Reaction | Mapping[str, Any]]] | None = None,
                 names: Mapping[str, str] | None = None, agents: Collection[str] = (),
                 trusted: Mapping[str, str] | None = None,
                 descriptions: Mapping[str, str] | None = None,
                 native_owners: Mapping[str, str] | None = None,
                 attestations: Mapping[str, str] | None = None,
                 people: Mapping[str, str] | None = None, channel: str = "agents",
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
        self.attestations = dict(attestations or {})
        self.people = dict(people or {})
        self.props: dict[str, str] = {}
        self.channel = channel
        self.channels = list(channels)
        self.post_id = post_id
        self.members: list[ChannelUser] = []
        self.presence: dict[str, str] = {}
        self.bots: dict[str, ChannelUser] = {}
        self.status: dict[str, str] | None = None
        self.header = ""
        self.dms: set[str] = set()
        self.viewed: list[str] = []
        self.sent_sync: list[dict[str, Any]] = []
        self.guest_join_failures: dict[str, str] = {}
        self.posted: list[dict[str, Any]] = []
        self.calls: list[tuple[str, int | None, int | None]] = []
        # RT-6.23: file id -> its info, and its content and preview.
        self.files: dict[str, FileInfo] = {}
        self.contents: dict[tuple[str, bool], bytes] = {}
        # RT-6.33: the files uploaded, and the ids the next uploads get.
        self.uploads: list[dict[str, Any]] = []
        self.upload_ids: list[str] = []
        # RT-6.25 step 8: the registrar's ask-mode record per home, and each read.
        self.mode_records: dict[str, str | BaseException] = {}
        self.fetched: list[str] = []
        self.mandate_records: dict[str, str | BaseException] = {}
        self.guests: set[str] = set()   # guest_channel_ids (RT-6.22)
        self.fetched_mandates: list[str] = []
        # RT-6.31: the registrar's debug record per home (or what it raises), each read.
        self.debug_records: dict[str, str | BaseException] = {}
        self.fetched_debug: list[str] = []
        # RT-6.26: the home's pinned posts (or an exception they raise), how
        # often they were read; channel ids by name besides the work channel.
        self.pinned: list[Post | Mapping[str, Any]] | BaseException = []
        self.pinned_reads = 0
        self.channel_ids: dict[str, str] = {}

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

    async def get_channel_id(self, channel_name: str | None = None,
                             team_name: str | None = None) -> str:
        """`channel` (or no name) is the work channel, else `channel_ids`;
        any other name is missing."""
        if not channel_name or channel_name == self.channel:
            return WORK
        if channel_name in self.channel_ids:
            return self.channel_ids[channel_name]
        raise LookupError(f"no channel {channel_name}")

    async def lobby_channel_id(self) -> str:
        """The home: the lobby is no channel of its own."""
        return await self.work_channel_id()

    def forget_guests(self) -> None:
        """Nothing kept: guest_channel_ids reads `guests` each time."""

    async def get_channel(self, channel_id: str) -> Channel:
        """Ids in `dms` are DMs; every other channel is open and named `channel`."""
        return Channel(id=channel_id, name=self.channel, type="D" if channel_id in self.dms else "O")

    async def post_channel_id(self, post_id: str) -> str:
        """The channel of a post in `posts`, else the work channel."""
        return next((p.channel_id for p in self._posts() if p.id == post_id and p.channel_id), WORK)

    async def usernames(self, user_ids: set[str]) -> dict[str, str]:
        return {i: self.names[i] for i in user_ids if i in self.names}

    async def username(self, user_id: str) -> str:
        """From `names`; raises KeyError for anyone else."""
        return self.names[user_id]

    async def agents(self, user_ids: set[str]) -> set[str]:
        return set(user_ids) & self._agents

    def _people(self) -> dict[str, str]:
        """lower-case username -> user id of `people`."""
        return {n.lower(): i for i, n in self.people.items()}

    async def human_id(self, username: str) -> str | None:
        return self._people().get(username.strip().lstrip("@").lower())

    async def humans_among(self, names: set[str]) -> set[str]:
        people = self._people()
        return {n for n in names if n.lower() in people}

    def dm_partner(self, channel: Channel, me: str) -> str:
        """Mattermost's rule: a DM is named `<id>__<id>`."""
        return dm_partner(channel, me)

    async def direct_channel_with(self, user_id: str) -> str:
        """A DM id made from the user id, added to `dms`."""
        cid = f"dm{user_id}"[:26]
        self.dms.add(cid)
        return cid

    async def direct_channel_with_bot(self, username: str) -> str:
        """The DM with a bot in `bots` or an agent in `names`; ValueError for
        a person or an unknown name, InvalidId for an invalid one."""
        name = mm_name(username.strip().lstrip("@").lower(), "user")
        if name in self.bots:
            return await self.direct_channel_with(self.bots[name].id)
        agent = next((i for i, n in self.names.items() if n.lower() == name and i in self._agents), None)
        if agent:
            return await self.direct_channel_with(agent)
        if name in self._people():
            raise ValueError(f"{name} is a person: ask people with ask_human")
        raise ValueError(f"no user {name}")

    async def trusted_humans(self) -> dict[str, str]:
        return dict(self.trusted)

    async def bot_description(self, user_id: str) -> str | None:
        return self.descriptions.get(user_id)

    async def agent_owner(self, user_id: str) -> str | None:
        """The adapter's rule (owners.decide) on `native_owners`, `descriptions`
        and a valid attestation in `attestations` (RT-6.24)."""
        claim = await owners.attested(cast("ChatPort", self), user_id)
        return owners.decide(self.native_owners.get(user_id), self.descriptions.get(user_id),
                             user_id, claim)

    async def agent_attestation(self, user_id: str) -> str | None:
        """`attestations`; '' for a bot with a readable description and none."""
        if user_id in self.attestations:
            return self.attestations[user_id]
        return "" if user_id in self.descriptions else None

    async def attestation_record(self, bot_id: str) -> str | None:
        """`attestations[bot_id]` (RT-6.24 step 5), None for a bot without one."""
        return self.attestations.get(bot_id) or None

    async def member_channels(self) -> list[UnreadChannel]:
        return list(self.channels)

    async def view_channel(self, channel_id: str) -> None:
        self.viewed.append(channel_id)

    async def channel_header(self, channel_id: str) -> str:
        return self.header

    async def set_channel_header(self, channel_id: str, header: str) -> None:
        self.header = header

    async def channel_users(self, channel_id: str) -> list[ChannelUser]:
        return list(self.members)

    async def bots_named(self, usernames: set[str]) -> dict[str, ChannelUser]:
        return {n.lower(): self.bots[n.lower()] for n in usernames if n.lower() in self.bots}

    async def is_member(self, channel_id: str, user_id: str) -> bool:
        """Every channel has the accounts in `members`."""
        return any(m.id == user_id for m in self.members)

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

    async def guest_channel_ids(self) -> set[str]:
        return set(self.guests)

    async def mandate_record(self, channel_id: str) -> str | None:
        """`mandate_records[channel_id]` (RT-6.26 step 8): a JWS, or an exception
        it raises; None (404) for a home without one. Counted in `fetched_mandates`."""
        self.fetched_mandates.append(channel_id)
        got = self.mandate_records.get(channel_id)
        if isinstance(got, BaseException):
            raise got
        return got

    async def debug_record(self, channel_id: str) -> str | None:
        """`debug_records[channel_id]` (RT-6.31 step 2): a JWS, or an exception
        it raises; None (the exact 404) for a home without one. Counted in `fetched_debug`."""
        self.fetched_debug.append(channel_id)
        got = self.debug_records.get(channel_id)
        if isinstance(got, BaseException):
            raise got
        return got

    async def ask_mode_record(self, channel_id: str) -> str | None:
        """`mode_records[channel_id]` (RT-6.25 step 8): a JWS, or an exception
        it raises; None (404) for a home without one. Counted in `fetched`."""
        self.fetched.append(channel_id)
        got = self.mode_records.get(channel_id)
        if isinstance(got, BaseException):
            raise got
        return got

    async def clear_custom_status(self) -> None:
        self.status = None

    async def post(self, message: str, *, channel_id: str | None = None,
                   root_id: str | None = None, props: dict[str, Any] | None = None,
                   file_ids: Sequence[str] = ()) -> Post:
        """Records the post (file_ids only when given); it comes back with the id post_id."""
        self.posted.append({"message": message, "channel_id": channel_id,
                            "root_id": root_id, "props": props}
                           | ({"file_ids": list(file_ids)} if file_ids else {}))
        return post_of({"id": self.post_id, "message": message, "user_id": self.me.id,
                        "channel_id": channel_id or WORK, "root_id": root_id or "",
                        "file_ids": list(file_ids)})

    def post_sync(self, message: str, *, channel_id: str, root_id: str = "",
                  props: dict[str, Any] | None = None, timeout: float = 5.0) -> Post:
        """Records the post in `sent_sync`; it comes back with the id post_id."""
        self.sent_sync.append({"message": message, "channel_id": channel_id,
                               "root_id": root_id, "props": props})
        return post_of({"id": self.post_id, "message": message, "user_id": self.me.id,
                        "channel_id": channel_id, "root_id": root_id})

    def post_by_name_sync(self, channel_name: str, message: str, *, join: str = "") -> bool:
        """Records the post in `sent_sync`; a name not in `channel` is missing."""
        self.sent_sync.append({"message": message, "channel_name": channel_name, "join": join})
        return channel_name == self.channel

    async def upload_file(self, channel_id: str, name: str, data: bytes) -> FileInfo:
        """Records the upload in `uploads`; the file gets the next id of `upload_ids`."""
        fid = self.upload_ids.pop(0) if self.upload_ids else f"{len(self.uploads):0>26}"
        self.uploads.append({"channel_id": channel_id, "name": name, "size": len(data), "id": fid})
        return FileInfo(id=fid, name=name, extension=name.rpartition(".")[2], size=len(data))

    async def thread(self, post_id: str) -> Thread:
        return Thread(post_id, posts=tuple(p for p in self._posts() if post_id in (p.id, p.root_id)))

    async def channel_posts(self, channel_id: str, *, per_page: int | None = None,
                            since_ms: int | None = None) -> list[Post]:
        self.calls.append(("posts", per_page, since_ms))
        if since_ms is not None:
            return [p for p in self._posts() if p.create_at >= since_ms]
        return self._posts()[:per_page]

    async def list_recent(self, *, channel_id: str | None = None, per_page: int = 20,
                          since_ms: int | None = None) -> list[Post]:
        """The newest `per_page` posts (or those since `since_ms`), oldest first."""
        posts = self._posts()
        posts = [p for p in posts if p.create_at >= since_ms] if since_ms is not None else posts[:per_page]
        return sorted(posts, key=lambda p: p.create_at)

    async def posts_after(self, since_ms: int, *, channel_id: str | None = None,
                          max_pages: int = 25) -> list[Post]:
        """All posts: the test hands only what came after."""
        return self._posts()

    async def file_info(self, file_id: str) -> FileInfo:
        return self.files[file_id]

    async def file_bytes(self, file_id: str, *, preview: bool = False, limit: int) -> bytes:
        """The content handed in self.contents[(id, preview)], cut at limit + 1."""
        self.calls.append(("preview" if preview else "file", limit, None))
        return self.contents[(file_id, preview)][:limit + 1]

    async def pinned_posts(self, channel_id: str) -> list[Post]:
        """`pinned` (RT-6.26 step 1), or the exception it holds; counted."""
        self.pinned_reads += 1
        if isinstance(self.pinned, BaseException):
            raise self.pinned
        return [p if isinstance(p, Post) else post_of(p) for p in self.pinned]

    async def reactions(self, post_id: str) -> list[Reaction]:
        return [r if isinstance(r, Reaction) else reaction_of(r) for r in self.reacts.get(post_id, [])]

    async def add_reaction(self, post_id: str, emoji: str) -> None:
        """The bot's reaction, kept in `reacts` (reactions reads it back)."""
        self.reacts.setdefault(post_id, []).append(
            Reaction(user_id=self.me.id, post_id=post_id, emoji_name=emoji))


class HookClient(FakeChat):
    """FakeChat as the hook API's tests name it: no channel names, so the
    work channel is the one watched."""


class FakeRegistrar:
    """A RegistrationPort for tests: the registrar session without a registrar.

    info: the bot session_start registers; kept: the bot of an ended server
    that adopt_sync takes over (None: none); keeps: whether the bot outlives
    the server (keeps_bot_on_exit); setup: what finish_session_setup
    reports. calls: what was asked of it, in order.
    Its public methods are the port's, each with the port's signature.
    Unlike the adapter, keeps is a free flag (the adapter derives it from the
    session and its file) and session_end never raises (the adapter does
    without a name and a bot).
    """

    def __init__(self, *, info: SessionInfo | None = None, kept: SessionInfo | None = None,
                 keeps: bool = False, setup: Mapping[str, Any] | None = None) -> None:
        self.info = info or SessionInfo(url="https://mm.example", name="agent-x",
                                        username="agent-agent-x", user_id=BOT, bot_token="t")
        self.kept = kept
        self.keeps_bot_on_exit = keeps
        self.setup = dict(setup or {})
        self.session: SessionInfo | None = None
        self.reused = False
        self.reaped: list[str] = []
        self.profile_warning = ""
        self.session_file_status: dict[str, Any] = {}
        self.calls: list[str] = []

    @property
    def bot_id(self) -> str:
        return self.session.user_id if self.session else ""

    async def session_start(self, name: str, display_name: str | None = None, *,
                            unique: bool = True) -> SessionInfo:
        """Registers `info`, or returns the bot it has (reused)."""
        self.calls.append(f"session_start {name}")
        self.reused = self.session is not None
        if self.session is None:
            self.session = self.info
            self.session_file_status = {"path": "", "written": True}
        return self.session

    async def finish_session_setup(self) -> dict[str, Any]:
        self.calls.append("finish_session_setup")
        return dict(self.setup)

    async def session_end(self, name: str | None = None) -> None:
        self.calls.append(f"session_end {name or ''}".rstrip())
        self.session = None

    def adopt_sync(self) -> SessionInfo | None:
        """Takes `kept` over, once."""
        self.calls.append("adopt_sync")
        if self.session is not None or self.kept is None:
            return None
        self.session, self.kept = self.kept, None
        return self.session

    def keep_sync(self) -> str:
        self.calls.append("keep_sync")
        if self.session is None:
            return "no session"
        self.session = None
        return "kept the bot for claude --continue (offline)"

    def shutdown_sync(self) -> str:
        self.calls.append("shutdown_sync")
        if self.session is None:
            return "no session"
        name, self.session = self.session.name, None
        return f"kept {name} for claude --continue" if self.keeps_bot_on_exit else f"unregistered {name}"


if TYPE_CHECKING:
    def _fake_chat_is_a_port(fake: FakeChat) -> ChatPort:
        """mypy checks that the fakes satisfy their ports, structurally."""
        return fake

    def _fake_registrar_is_a_port(fake: FakeRegistrar) -> RegistrationPort:
        return fake

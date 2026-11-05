"""The chat port: what the bus asks of its chat (arc42 §5.1).

The modules that only post, read and set status depend on ChatPort, not on
client.py. client.MattermostAdapter implements it for Mattermost and stays the
only module that talks HTTP. Registration (the registrar, bot and token) is no
chat operation and stays outside the port: the adapter does it, server.py and
hosted.py drive it.

The members are the ones the port's modules call, nothing more. What comes
back is a bus type (model.py), never Mattermost's JSON: posts and threads, the
bot account, a channel, a channel's accounts and reactions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .model import Channel, ChannelUser, FileInfo, Me, Post, Reaction, Thread, UnreadChannel


class BotSession(Protocol):
    """The bot this server registered, as the port's modules see it."""

    @property
    def user_id(self) -> str: ...


class ChatPort(Protocol):
    """Chat and carrier operations of the bus (arc42 §5.1)."""

    # --- who and where -------------------------------------------------------
    @property
    def token(self) -> str | None:
        """The bot's token; None while no bot is set up (nothing to post as)."""
        ...

    @property
    def session(self) -> BotSession | None:
        """The bot this server registered, None when it adopted or was given one."""
        ...

    @property
    def work_channel_name(self) -> str: ...

    @property
    def permalink_base(self) -> str:
        """A post's permalink is this plus its id (RT-6.15)."""
        ...

    async def get_me(self) -> Me: ...

    async def account_created(self) -> int:
        """When the bot's account was created, epoch ms; 0 unknown."""
        ...

    async def work_channel_id(self) -> str:
        """The home's id (ADR-002): the project channel, else MM_CHANNEL."""
        ...

    async def join_channel(self, channel_name: str) -> str:
        """The team's channel `channel_name`, joined as the bot (a non-member
        cannot read it; joining twice is harmless): its id. Never creates one:
        a missing channel raises."""
        ...

    async def get_channel(self, channel_id: str) -> Channel: ...

    async def get_channel_id(self, channel_name: str | None = None,
                             team_name: str | None = None) -> str:
        """The id of the team's channel `channel_name` (default: the home),
        without joining it (RT-6.26 step 4); raises when it is missing or
        the bot may not see it."""
        ...

    async def channel_header(self, channel_id: str) -> str:
        """The channel's header as it is now, never cached (RT-6.6 step 7)."""
        ...

    async def set_channel_header(self, channel_id: str, header: str) -> None: ...

    # --- people --------------------------------------------------------------
    async def usernames(self, user_ids: set[str]) -> dict[str, str]: ...

    async def agents(self, user_ids: set[str]) -> set[str]: ...

    async def bots_named(self, usernames: set[str]) -> dict[str, ChannelUser]:
        """The bot accounts among these usernames, by lower-case username,
        with their custom status (RT-6.21); people, unknown and deleted
        accounts are left out."""
        ...

    async def trusted_humans(self) -> dict[str, str]: ...

    async def bot_description(self, user_id: str) -> str | None:
        """A bot's profile description, read with the bot's own token, where
        the registrar writes `for @<owner> · trusts @…` (ADR-003); None when
        it cannot be read."""
        ...

    async def agent_owner(self, user_id: str) -> str | None:
        """Whose agent the bot is (ADR-003, WP3): the username of its native
        owner where the chat system keeps one and lets the bot read it, else
        `for @<owner>` from its description, lower case; '' when it has none
        (unbound, owned by the registrar's account, or the two disagree), None
        when neither can be read. owners.decide has the rule."""
        ...

    async def agent_attestation(self, user_id: str) -> str | None:
        """The registrar's signed owner claim its account carries (RT-6.24: none
        today, a bot cannot write its own props), read where its description is
        read; '' when it carries none, None when it cannot be read.
        owners.attested checks it."""
        ...

    # --- channel membership, status and presence -----------------------------
    async def member_channels(self) -> list[UnreadChannel]: ...

    async def channel_users(self, channel_id: str) -> list[ChannelUser]: ...

    async def is_member(self, channel_id: str, user_id: str) -> bool:
        """Whether the account is a member of the channel (RT-6.21)."""
        ...

    async def presences(self, user_ids: list[str]) -> dict[str, str]: ...

    async def set_presence(self, user_id: str, status: str) -> None: ...

    async def set_custom_status(self, emoji: str, text: str, expires_at: str) -> None: ...

    async def clear_custom_status(self) -> None: ...

    async def ask_mode_record(self, channel_id: str) -> str | None:
        """The registrar's signed ask-mode record of a home (RT-6.25 step 8),
        unverified (attest.verify_mode checks it): the compact JWS, None when
        no mode is set there. Raises on any other failure. The one registrar
        read on the port: it is about a chat channel, and the tests fake it."""
        ...

    async def publish_usage(self, value: str) -> bool:
        """Put this bot's usage report (ADR-008 step 3, usage.py) on its own
        account as `agent_usage`, keeping the rest, like publish_attestation;
        False when it is there already. The caller holds lease.status_lock()."""
        ...

    async def agent_usage(self, user_id: str) -> str | None:
        """The `agent_usage` a bot published on its account (ADR-008 step 3);
        '' when it carries none, None when it cannot be read. Cached about a
        minute."""
        ...

    # --- posting and replying ------------------------------------------------
    async def post(self, message: str, *, channel_id: str | None = None,
                   root_id: str | None = None, props: dict[str, Any] | None = None) -> Post:
        """Post, or reply with root_id."""
        ...

    def post_sync(self, message: str, *, channel_id: str, root_id: str = "",
                  props: dict[str, Any] | None = None, timeout: float = 5.0) -> Post:
        """Post without an event loop, for the exit handler."""
        ...

    def post_by_name_sync(self, channel_name: str, message: str, *, join: str = "") -> bool: ...

    # --- reading -------------------------------------------------------------
    async def thread(self, post_id: str) -> Thread: ...

    async def post_channel_id(self, post_id: str) -> str:
        """The channel a post is in."""
        ...

    async def channel_posts(self, channel_id: str, *, per_page: int | None = None,
                            since_ms: int | None = None) -> list[Post]: ...

    async def posts_after(self, since_ms: int, *, channel_id: str | None = None,
                          max_pages: int = 25) -> list[Post]: ...

    async def reactions(self, post_id: str) -> list[Reaction]: ...

    async def pinned_posts(self, channel_id: str) -> list[Post]:
        """A channel's pinned posts, newest first (RT-6.26 step 1); raises
        when they cannot be read."""
        ...

    async def file_info(self, file_id: str) -> FileInfo:
        """A file attached to a post (RT-6.23); raises where the bot may not read it."""
        ...

    async def file_bytes(self, file_id: str, *, preview: bool = False, limit: int) -> bytes:
        """A file's content, or with preview its image preview: at most
        limit + 1 bytes (more than limit: it is longer)."""
        ...

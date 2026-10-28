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
    from .model import Channel, ChannelUser, Me, Post, Reaction, Thread, UnreadChannel


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

    async def work_channel_id(self) -> str: ...

    async def get_channel(self, channel_id: str) -> Channel: ...

    # --- people --------------------------------------------------------------
    async def usernames(self, user_ids: set[str]) -> dict[str, str]: ...

    async def agents(self, user_ids: set[str]) -> set[str]: ...

    async def trusted_humans(self) -> dict[str, str]: ...

    # --- channel membership, status and presence -----------------------------
    async def member_channels(self) -> list[UnreadChannel]: ...

    async def channel_users(self, channel_id: str) -> list[ChannelUser]: ...

    async def presences(self, user_ids: list[str]) -> dict[str, str]: ...

    async def set_presence(self, user_id: str, status: str) -> None: ...

    async def set_custom_status(self, emoji: str, text: str, expires_at: str) -> None: ...

    async def clear_custom_status(self) -> None: ...

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

    async def channel_posts(self, channel_id: str, *, per_page: int | None = None,
                            since_ms: int | None = None) -> list[Post]: ...

    async def posts_after(self, since_ms: int, *, channel_id: str | None = None,
                          max_pages: int = 25) -> list[Post]: ...

    async def reactions(self, post_id: str) -> list[Reaction]: ...

"""The composition root: the one place that builds the chat adapter (arc42 §5.2).

The entry points (server.py, hosted.py) hold a BusClient, the chat port and
the registration port on one object, and get it from client_from_env; none of
them imports client.py (test_adapter_boundary); the WebSocket inbox gets its
live events from event_source. A second chat adapter, and its event source,
is chosen here and nowhere else.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, Protocol, cast

from . import ownpost, unanswered
from .port import ChatPort
from .registration import RegistrationPort

# Wire at runtime, not only under TYPE_CHECKING: Remembered inherits it, so
# the class body needs the name when the module loads.
from .mattermost_ws import Wire

if TYPE_CHECKING:
    from .events import EventSource
    from .model import Post
    from .session_file import SessionInfo

log = logging.getLogger(__name__)


class BusClient(ChatPort, RegistrationPort, Protocol):
    """What the entry points hold: the chat and the bot's registrar session
    on one object, since a new bot's token and the chat's caches change
    together (registration.py)."""

    @property
    def session(self) -> SessionInfo | None:
        """The bot this server registered or took over (both ports name it)."""
        ...


class Remembered(Wire):
    """A ChatPort that keeps the bot's own posts as the bus's own (#255):
    `post` is the one place the bus logic, lease.py, departed.py, askroute.py,
    mandate.py, remind.py, stats.py, hosted.py and the inbox's replies all go
    through, so the bookkeeping holds for any adapter, Mattermost's and the
    NATS one alike.

    The members named here are explicit, not handed on by `__getattr__`, and
    that matters: `isinstance` against a `runtime_checkable` Protocol looks the
    members up with `inspect.getattr_static`, which does not see `__getattr__`.
    `event_source` tests its client against `mattermost_ws.Wire`, so a wrapper
    with `__getattr__` only would have failed that test and left the inbox
    without a source — the bus deaf, in every process (#256 review)."""

    def __init__(self, port: BusClient) -> None:
        self.port = port

    # Named for the Wire check (the three above are its members too) and in
    # use here: `__getattr__` is not enough for the isinstance test.
    @property
    def base_url(self) -> str:
        return cast("Wire", self.port).base_url

    @property
    def token(self) -> str | None:
        return self.port.token

    @property
    def permalink_base(self) -> str:
        return self.port.permalink_base

    @property
    def bot_id(self) -> str:
        """The bot's user id as the port names it, read live: the adapter takes
        it from its session or the environment, a fake from its registrar."""
        return self.port.bot_id

    @property
    def session(self) -> SessionInfo | None:
        return self.port.session

    def __getattr__(self, name: str) -> Any:
        # Everything else the port has, handed on unchanged. Not found by
        # isinstance against a runtime_checkable Protocol (see the class
        # docstring): a member a protocol test depends on belongs above.
        return getattr(self.port, name)

    async def post(self, message: str, *, channel_id: str | None = None,
                   root_id: str | None = None, props: dict[str, Any] | None = None,
                   file_ids: Sequence[str] = ()) -> Post:
        post: Post = await self.port.post(message, channel_id=channel_id, root_id=root_id,
                                          props=props, file_ids=file_ids)
        # The chat resolved the channel (the home when none was named): the
        # post carries it, so no request is repeated here.
        await _remember(self.port, self.port.bot_id, post.channel_id, post)
        return post


async def _remember(port: ChatPort, bot_id: str, channel_id: str, post: Post) -> None:
    """Keep the bot's latest post in the work channel for the Stop hook and the
    exit handler (ownpost.py); a post in a thread (or DM) answers the posts
    there that waited for it (unanswered.py, RT-6.2 step 2) — not a line the
    server writes on its own, nor a pure status such as an IDLE
    (unanswered.answers). Bookkeeping: never fails the post (#255)."""
    try:
        if not bot_id:
            return
        unanswered.answered(bot_id, channel_id, post)
        if channel_id == await port.work_channel_id():
            ownpost.record(bot_id, channel_id, post)
    except Exception as exc:  # noqa: BLE001
        log.info("own post not remembered: %s", exc)


def client_from_env() -> BusClient:
    """The chat adapter the environment names (MM_CHAT_URL, MM_BOT_TOKEN,
    MM_REGISTER_URL, …): Mattermost's, wrapped so that its posts are kept as
    the bus's own (#255). Raises RuntimeError without a chat URL."""
    from .client import MattermostAdapter

    adapter = MattermostAdapter.from_env()
    return Remembered(adapter)


def event_source(client: ChatPort) -> EventSource:
    """The live events of the chat client_from_env built (events.py), for a
    WebSocket inbox handed none: Mattermost's WebSocket, as client's bot.
    Raises TypeError for a client without the server's address and the bot's
    token (mattermost_ws.Wire): no source for that chat."""
    from .mattermost_ws import MattermostEvents, Wire

    if not isinstance(client, Wire):
        raise TypeError("event_source: no source for this chat")
    return MattermostEvents(client)

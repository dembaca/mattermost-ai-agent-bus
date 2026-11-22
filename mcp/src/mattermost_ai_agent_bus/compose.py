"""The composition root: the one place that builds the chat adapter (arc42 §5.2).

The entry points (server.py, hosted.py) hold a BusClient, the chat port and
the registration port on one object, and get it from client_from_env; none of
them imports client.py (test_adapter_boundary); the WebSocket inbox gets its
live events from event_source. A second chat adapter, and its event source,
is chosen here and nowhere else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, cast

from .port import ChatPort
from .registration import RegistrationPort

if TYPE_CHECKING:
    from .client import MattermostAdapter
    from .events import EventSource
    from .session_file import SessionInfo


class BusClient(ChatPort, RegistrationPort, Protocol):
    """What the entry points hold: the chat and the bot's registrar session
    on one object, since a new bot's token and the chat's caches change
    together (registration.py)."""

    @property
    def session(self) -> SessionInfo | None:
        """The bot this server registered or took over (both ports name it)."""
        ...


def client_from_env() -> BusClient:
    """The chat adapter the environment names (MM_CHAT_URL, MM_BOT_TOKEN,
    MM_REGISTER_URL, …): Mattermost's. Raises RuntimeError without a chat URL."""
    from .client import MattermostAdapter

    return MattermostAdapter.from_env()


def event_source(client: ChatPort) -> EventSource:
    """The live events of the chat client_from_env built (events.py), for a
    WebSocket inbox handed none: Mattermost's WebSocket, as client's bot."""
    from .mattermost_ws import MattermostEvents

    # client_from_env builds the Mattermost adapter and nothing else; the
    # source reads its address and token, which are no port members.
    return MattermostEvents(cast("MattermostAdapter", client))

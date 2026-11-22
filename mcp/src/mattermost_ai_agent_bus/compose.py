"""The composition root: the one place that builds the chat adapter (arc42 §5.2).

The entry points (server.py, hosted.py) hold a BusClient, the chat port and
the registration port on one object, and get it from client_from_env; none of
them imports client.py (test_adapter_boundary). A second chat adapter is
chosen here and nowhere else.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from .port import ChatPort
from .registration import RegistrationPort

if TYPE_CHECKING:
    from .session_file import SessionInfo


class BusClient(ChatPort, RegistrationPort, Protocol):
    """What the entry points hold: the chat and the bot's registrar session
    on one object, since a new bot's token and the chat's caches change
    together (registration.py)."""

    @property
    def session(self) -> SessionInfo | None:
        """The bot this server registered or took over (both ports name it)."""
        ...

    @property
    def base_url(self) -> str:
        """The chat server's address, for the WebSocket inbox only: it goes
        when the inbox becomes an event source of the adapter's own."""
        ...


def client_from_env() -> BusClient:
    """The chat adapter the environment names (MM_CHAT_URL, MM_BOT_TOKEN,
    MM_REGISTER_URL, …): Mattermost's. Raises RuntimeError without a chat URL."""
    from .client import MattermostAdapter

    return MattermostAdapter.from_env()

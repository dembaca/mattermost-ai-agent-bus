"""The registration port: the bot's life at the registrar (arc42 §5.2).

Registering a bot, taking over the bot of a server that ended, and ending it
are no chat operations (port.py): they go to the registrar with
MM_REG_SECRET, or with the bot's own token to remove it, before a chat
session exists and after it ended. RegistrationPort names what the entry
points (server.py, hosted.py) drive of it, so that no bus-logic module can
create or remove a bot. client.MattermostAdapter implements it; in this phase
the same object holds the chat port, since a new bot's token and the chat
caches change together (RT-6.1, RT-6.10, RT-6.18).

The members are the ones the entry points call, nothing more.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from .session_file import SessionInfo


class RegistrationPort(Protocol):
    """The registrar session of this server's bot (RT-6.1, RT-6.10)."""

    # --- the bot -------------------------------------------------------------
    @property
    def session(self) -> SessionInfo | None:
        """The bot this server registered or took over; None when it was
        given one (MM_BOT_TOKEN) or has none."""
        ...

    @property
    def bot_id(self) -> str:
        """The bot's user id as far as it is known without a request."""
        ...

    @property
    def keeps_bot_on_exit(self) -> bool:
        """The bot outlives this server, for `--resume <id>` or the
        workspace's next server (RT-6.10)."""
        ...

    # --- what the last session_start did -------------------------------------
    @property
    def reused(self) -> bool:
        """It returned this session's bot instead of registering another."""
        ...

    @property
    def reaped(self) -> list[str]:
        """The bots of long-ended sessions it unregistered first."""
        ...

    @property
    def profile_warning(self) -> str:
        """What the registrar wrote into the profile that differs from what
        was sent (RT-6.8, RT-6.24 step 2); '' when nothing does."""
        ...

    @property
    def session_file_status(self) -> dict[str, Any]:
        """Where the session file for the hooks went, and whether it was
        written (path, written, error, project_dir)."""
        ...

    # --- life ----------------------------------------------------------------
    async def session_start(self, name: str, display_name: str | None = None, *,
                            unique: bool = True) -> SessionInfo:
        """Register a bot (or return this session's, reused) and make it this
        server's. Raises inside a corral sandbox (RT-6.18) and without the
        registrar's secret or address."""
        ...

    async def finish_session_setup(self) -> dict[str, Any]:
        """Join the home and the guest channels and empty the new bot's inbox
        (RT-6.22): what each join did; reports a failure, never raises."""
        ...

    async def session_end(self, name: str | None = None) -> None:
        """Unregister the bot (default: this server's) and forget it."""
        ...

    def adopt_sync(self) -> SessionInfo | None:
        """Take over the bot of the server that ended (RT-6.10), while its
        token is still accepted; None when there is none."""
        ...

    def keep_sync(self) -> str:
        """Leave the bot to the host session hooks (RT-6.18 step 9): offline
        only. Best effort, never raises; the outcome for the log."""
        ...

    def shutdown_sync(self) -> str:
        """At the process's exit, without an event loop: keep the bot for
        the next server or unregister it (RT-6.10). Best effort, never
        raises; the outcome for the log."""
        ...

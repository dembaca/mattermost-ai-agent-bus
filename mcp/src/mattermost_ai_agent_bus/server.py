"""MCP server exposing Mattermost agent-bus tools."""

from __future__ import annotations

import atexit
import json
import logging
import os
import signal
import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from .client import MattermostClient
from .websocket_inbox import WebSocketInbox

log = logging.getLogger(__name__)

mcp = MCPServer(
    "mattermost-ai-agent-bus",
    instructions=(
        "Mattermost agent bus. When MM_BOT_TOKEN is already set, a host-side session "
        "hook registered the bot and resolved the channels — post/reply directly and "
        "do NOT call session_start/session_end. Only when MM_BOT_TOKEN is unset, "
        "register with session_start (needs MM_REG_SECRET) and session_end when done. "
        "session_start joins MM_PROJECT_CHANNEL and appends a session suffix to the name. "
        "Posts go to MM_PROJECT_CHANNEL when set, else MM_CHANNEL; wait_for_events "
        "covers mentions/DMs and both channels. Never create channels or teams."
    ),
)

_client: MattermostClient | None = None
_inbox: WebSocketInbox | None = None


def _get_client() -> MattermostClient:
    global _client
    if _client is None:
        _client = MattermostClient.from_env()
    return _client


def _get_inbox() -> WebSocketInbox:
    global _inbox
    client = _get_client()
    if _inbox is None or _inbox.client is not client:
        _inbox = WebSocketInbox(client=client)
    return _inbox


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


@mcp.tool()
async def session_start(
    name: str, display_name: str = "", unique: bool = True
) -> str:
    """Register an ephemeral Mattermost bot via the registrar.

    Requires MM_REG_SECRET and MM_REGISTER_URL / MM_CHAT_URL.
    A session suffix is appended to ``name`` (set unique=false to opt out): a live
    name cannot be registered twice. Joins MM_PROJECT_CHANNEL when set. Returns
    session fields (name, username, user_id, url) and the join result. The bot
    token goes to a 0600 session file so the Claude Code hooks can see it.
    """
    client = _get_client()
    info = await client.session_start(name, display_name or None, unique=unique)
    global _inbox
    _inbox = None  # reset inbox for new identity
    setup = await client.finish_session_setup()
    return _dump(
        {
            "name": info.name,
            "username": info.username,
            "user_id": info.user_id,
            "url": info.url,
            "token_set": True,
            # Hooks read the token from here; written=false means they stay inert.
            "hooks_session_file": client.session_file_status,
            **setup,
        }
    )


@mcp.tool()
async def session_end(name: str = "") -> str:
    """Unregister the ephemeral bot (DELETE registrar). Call when the task ends."""
    client = _get_client()
    global _inbox
    if _inbox is not None:
        await _inbox.close()
        _inbox = None
    await client.session_end(name or None)
    return _dump({"ok": True, "ended": name or os.environ.get("MM_BOT_NAME", "")})


@mcp.tool()
async def get_me() -> str:
    """Return the current bot user (users/me)."""
    me = await _get_client().get_me()
    return _dump(me)


@mcp.tool()
async def post_message(
    message: str, channel_id: str = "", root_id: str = ""
) -> str:
    """Create a post. Uses default MM_CHANNEL when channel_id is empty."""
    post = await _get_client().post(
        message,
        channel_id=channel_id or None,
        root_id=root_id or None,
    )
    return _dump(post)


@mcp.tool()
async def reply_in_thread(
    root_id: str, message: str, channel_id: str = ""
) -> str:
    """Reply in a thread (sets root_id on the new post)."""
    post = await _get_client().post(
        message,
        channel_id=channel_id or None,
        root_id=root_id,
    )
    return _dump(post)


@mcp.tool()
async def get_thread(post_id: str) -> str:
    """Fetch a thread (root + replies) for post_id."""
    data = await _get_client().thread(post_id)
    return _dump(data)


@mcp.tool()
async def list_recent(
    channel_id: str = "", per_page: int = 20, since_ms: int = 0
) -> str:
    """List recent posts in a channel (oldest→newest)."""
    posts = await _get_client().list_recent(
        channel_id=channel_id or None,
        per_page=per_page,
        since_ms=since_ms or None,
    )
    return _dump(posts)


@mcp.tool()
async def wait_for_events(
    timeout_sec: float = 60.0, max_events: int = 10
) -> str:
    """Wait for inbox events: mentions, DMs, or watched-channel posts.

    Connects the Mattermost WebSocket if needed. Returns JSON list of
    {kind, post, channel} (empty list on timeout).
    """
    inbox = _get_inbox()
    events = await inbox.wait_for_events(
        timeout_sec=timeout_sec, max_events=max_events
    )
    payload = [
        {"kind": e.kind, "post": e.post, "channel": e.channel} for e in events
    ]
    return _dump(payload)


_exit_handled = False


def _cleanup_on_exit() -> None:
    """Unregister the bot this server owns when the process ends."""
    global _exit_handled
    if _exit_handled or _client is None:
        return
    _exit_handled = True
    outcome = _client.shutdown_sync()
    if outcome not in ("no session", "session not owned"):
        log.info("exit cleanup: %s", outcome)


def _install_exit_handlers() -> None:
    # Claude Code ends an MCP server by closing stdin, which returns from
    # mcp.run() and reaches atexit. SIGTERM/SIGHUP would skip it.
    atexit.register(_cleanup_on_exit)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        try:
            signal.signal(sig, lambda *_: sys.exit(0))
        except (ValueError, OSError):  # not the main thread / unsupported
            pass


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    _install_exit_handlers()
    mcp.run()


if __name__ == "__main__":
    main()

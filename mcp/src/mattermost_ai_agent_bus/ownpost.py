"""The agent's own latest post in the work channel (RT-6.2 in docs/arc42/06-runtime-view.md).

Two readers ask what the agent last said there: the Stop hook, whether it signed
off, and the exit handler, whether it left without. Finding it in the channel
cost the hook a 100-post fetch on every turn, so whoever posts as the agent keeps
it in ``last-<uid>.env`` in the state directory (``mm_bot_state_file last``):

    CHANNEL=<channel id>   the work channel
    ID=<post id>           its latest post there, any thread
    AT=<create_at ms>
    MSG=<first words>      one line, without the 👑 (RT-6.6)
    HELLO=<post id>        the agent's announcement: the thread a BYE goes to

Written by the MCP server (client.post) and bin/mm-agent-post.sh; a reader
without a file asks the channel as before. A post by another route leaves it
stale, accepted: the hook asked for a sign-off before it too only saw what the
channel showed it a moment later.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import a2a, session_file

if TYPE_CHECKING:
    from .client import MattermostClient

log = logging.getLogger("mattermost_ai_agent_bus.ownpost")

# What the Stop hook counts as a sign-off (a HANDOVER is not: the agent is
# still there). Same words as `^(IDLE|BYE)` in hooks/stop-inbox-check.sh.
SIGN_OFFS = ("IDLE", "BYE")
BYE = "BYE · session ended without sign-off"
# An exit handler has little time and the unregistering comes after it.
BYE_TIMEOUT_SEC = 1.5
_MSG_CHARS = 80


def path(uid: str) -> Path:
    return session_file.state_dir() / f"last-{uid}.env"


def read(uid: str) -> dict[str, str]:
    return session_file.parse(path(uid)) if uid else {}


def record(uid: str, channel_id: str, post: Mapping[str, Any]) -> None:
    """Remember `post`, the bot's latest in the work channel."""
    if not uid or not post.get("id"):
        return
    message = str(post.get("message", "")).removeprefix("👑").strip()
    prev = read(uid)
    hello = post["id"] if not post.get("root_id") and a2a.word(message) == "HELLO" else (
        prev.get("HELLO", "") if prev.get("CHANNEL") == channel_id else "")
    lines = [f"CHANNEL={channel_id}", f"ID={post['id']}", f"AT={post.get('create_at', 0)}",
             f"MSG={' '.join(message.split())[:_MSG_CHARS]}", f"HELLO={hello}"]
    p = path(uid)
    try:
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        p.write_text("\n".join(lines) + "\n")
    except OSError as exc:
        log.info("own post not remembered: %s", exc)


def remove(uid: str) -> None:
    if uid:
        path(uid).unlink(missing_ok=True)


def bye_sync(client: MattermostClient, uid: str) -> str:
    """Post BYE in the agent's HELLO thread when its last post in the work
    channel is no sign-off (RT-6.2). Synchronous, for the exit handler: one
    request, bounded, fail-open; returns a short outcome for the log."""
    last = read(uid)
    if not (client.token and last.get("HELLO") and last.get("CHANNEL")):
        return "no announcement"  # never part of the coordination: nothing to end
    if a2a.word(last.get("MSG", "")) in SIGN_OFFS:
        return "signed off"
    try:
        post = client.post_sync(BYE, channel_id=last["CHANNEL"], root_id=last["HELLO"],
                                props=a2a.props(BYE), timeout=BYE_TIMEOUT_SEC)
        record(uid, last["CHANNEL"], post)
    except Exception as exc:  # noqa: BLE001 — exit handler: fail open
        return f"BYE not posted: {type(exc).__name__}"
    return "BYE posted"

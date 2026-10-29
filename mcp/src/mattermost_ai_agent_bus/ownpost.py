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
    DELIVERED=1            an addressed event (a mention, a DM, a human's task,
                           a trusted human's answer on an ask) reached the
                           agent after this post: a sign-off no longer covers
                           it (RT-6.2 step 2); the next own post clears it

A reminder the server sends on its own (remind.py, RT-6.19) is not the agent's
post: it is not remembered, so it neither undoes a sign-off nor opens a new
listen window.

Written by the MCP server (client.post) and bin/mm-agent-post.sh; a reader
without a file asks the channel as before.

The threads the agent took part in in guest channels (RT-6.22) live next to
it, in ``threads-<uid>.env``, one line per thread, the latest last:

    <root id> <channel name> <bus word of the agent's latest post there>

A reply there wakes the agent (it is its thread), and a BYE lists those not
ended with DONE or CANCEL. A post by another route leaves it
stale, accepted: the hook asked for a sign-off before it too only saw what the
channel showed it a moment later.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING

from . import a2a, session_file
from .model import Post

if TYPE_CHECKING:
    from .port import ChatPort

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


def record(uid: str, channel_id: str, post: Post) -> None:
    """Remember `post`, the bot's latest in the work channel."""
    if not uid or not post.id or post.kind == a2a.REMINDER:
        return
    message = post.message.removeprefix("👑").strip()
    prev = read(uid)
    hello = post.id if not post.root_id and a2a.word(message) == "HELLO" else (
        prev.get("HELLO", "") if prev.get("CHANNEL") == channel_id else "")
    lines = [f"CHANNEL={channel_id}", f"ID={post.id}", f"AT={post.create_at}",
             f"MSG={' '.join(message.split())[:_MSG_CHARS]}", f"HELLO={hello}"]
    p = path(uid)
    try:
        session_file.write_private(p, "\n".join(lines) + "\n")
    except OSError as exc:
        log.info("own post not remembered: %s", exc)


def delivered(uid: str) -> None:
    """An addressed event reached the agent: its last sign-off no longer covers
    what happened on the bus (RT-6.2 step 2). Only for an agent that posted
    in the work channel; never raises."""
    rec = read(uid)
    if not rec.get("ID") or rec.get("DELIVERED") == "1":
        return
    rec["DELIVERED"] = "1"
    try:
        session_file.write_private(path(uid), "".join(f"{k}={v}\n" for k, v in rec.items()))
    except OSError as exc:
        log.info("delivery not remembered: %s", exc)


_SIGNED_OFF = re.compile(r"^(IDLE|BYE)([^A-Za-z]|$)")


def signed_off(message: str, rec: dict[str, str] | None = None) -> bool:
    """Nothing changed on the bus since the agent's sign-off (RT-6.2 step 2):
    its latest own post in the work channel is an IDLE or BYE (the 👑 before
    it aside) and no addressed event reached it after that post."""
    return (bool(_SIGNED_OFF.match(message.removeprefix("👑").strip()))
            and (rec or {}).get("DELIVERED") != "1")


def remove(uid: str) -> None:
    if uid:
        path(uid).unlink(missing_ok=True)


# Guest threads (RT-6.22): at most this many are kept; a thread whose latest
# own post says one of these is no longer open.
GUEST_THREADS_MAX = 50
CLOSED = ("DONE", "CANCEL", "BYE")


def threads_path(uid: str) -> Path:
    return session_file.state_dir() / f"threads-{uid}.env"


def guest_threads(uid: str) -> dict[str, tuple[str, str]]:
    """root id -> (channel name, latest own word), oldest first."""
    try:
        lines = threads_path(uid).read_text().splitlines() if uid else []
    except OSError:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for line in lines:
        parts = line.split()
        if len(parts) >= 2:
            out.pop(parts[0], None)
            out[parts[0]] = (parts[1], parts[2] if len(parts) > 2 else "")
    return out


def record_guest(uid: str, channel_name: str, post: Post) -> None:
    """The agent posted in a guest channel: its thread is the agent's (RT-6.22)."""
    root = post.root_id or post.id
    if not uid or not root:
        return
    rows = guest_threads(uid)
    rows.pop(root, None)
    rows[root] = (channel_name or "-", a2a.word(post.message.removeprefix("👑").strip()) or "-")
    kept = list(rows.items())[-GUEST_THREADS_MAX:]
    try:
        session_file.write_private(threads_path(uid), "".join(
            f"{r} {c} {w}\n" for r, (c, w) in kept))
    except OSError as exc:
        log.info("guest thread not remembered: %s", exc)


def open_guest_threads(uid: str) -> list[tuple[str, str]]:
    """(root id, channel name) of the guest threads not ended (RT-6.22)."""
    return [(r, c) for r, (c, w) in guest_threads(uid).items() if w not in CLOSED]


def with_guest_threads(message: str, uid: str) -> str:
    """A BYE with the guest threads still open (RT-6.22): whoever reads the
    home learns where this agent left work in other channels."""
    left = open_guest_threads(uid)
    if not left:
        return message
    return message.rstrip() + "\nOpen in guest channels: " + ", ".join(
        f"~{c} {r}" for r, c in left)


def bye_sync(client: ChatPort, uid: str) -> str:
    """Post BYE in the agent's HELLO thread when its last post in the work
    channel is no sign-off (RT-6.2). Synchronous, for the exit handler: one
    request, bounded, fail-open; returns a short outcome for the log."""
    last = read(uid)
    if not (client.token and last.get("HELLO") and last.get("CHANNEL")):
        return "no announcement"  # never part of the coordination: nothing to end
    if a2a.word(last.get("MSG", "")) in SIGN_OFFS:
        return "signed off"
    try:
        post = client.post_sync(with_guest_threads(BYE, uid), channel_id=last["CHANNEL"],
                                root_id=last["HELLO"], props=a2a.props(BYE),
                                timeout=BYE_TIMEOUT_SEC)
        record(uid, last["CHANNEL"], post)
    except Exception as exc:  # noqa: BLE001 — exit handler: fail open
        return f"BYE not posted: {type(exc).__name__}"
    return "BYE posted"

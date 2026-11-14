"""Bus posts that reached the agent and have no answer by it in their thread
yet (RT-6.2 step 2 in docs/arc42/06-runtime-view.md).

A post that needs an answer — a mention or DM, a reply in a thread the agent
posted in, a post for the crown holder; never an fyi post, a gated one or a
reaction (server.py decides at delivery) — is kept here until the agent posts
in its thread (or, for a DM, in that DM). The Stop hook and the turn plugins
read the open ones once at a turn end and block once for them: an answer only
in the terminal reaches nobody on the bus.

One line per post in ``unanswered-<uid>.env`` in the state directory, next to
``last-<uid>.env`` (ownpost.py), so a restarted server of the same bot keeps
them; the newest last:

    <post id> <root id> <channel id> <channel name|DM> <author> <at s> <dm 0|1> <reminded 0|1>

At most MAX are kept, none older than MAX_AGE_SEC. A reminded post blocks no
turn end again; it stays until answered or aged out.
"""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import session_file
from .model import Post

log = logging.getLogger("mattermost_ai_agent_bus.unanswered")

MAX = 10
MAX_AGE_SEC = 24 * 3600
DM = "DM"


@dataclass(frozen=True)
class Open:
    post_id: str
    root_id: str
    channel_id: str
    channel: str
    author: str
    at: int
    dm: bool = False
    reminded: bool = False


def path(uid: str) -> Path:
    return session_file.state_dir() / f"unanswered-{uid}.env"


def _word(value: str) -> str:
    return "_".join(value.split()) or "-"


def read(uid: str, now: float | None = None) -> list[Open]:
    """The open posts, oldest first, without those older than MAX_AGE_SEC."""
    if not uid:
        return []
    try:
        lines = path(uid).read_text().splitlines()
    except OSError:
        return []
    cutoff = (time.time() if now is None else now) - MAX_AGE_SEC
    out: list[Open] = []
    for line in lines:
        f = line.split()
        if len(f) != 8 or not f[5].isdigit() or int(f[5]) < cutoff:
            continue
        out.append(Open(f[0], f[1], f[2], f[3], f[4], int(f[5]), f[6] == "1", f[7] == "1"))
    return out


def _write(uid: str, rows: list[Open]) -> None:
    p = path(uid)
    try:
        if not rows:
            p.unlink(missing_ok=True)
            return
        session_file.write_private(p, "".join(
            f"{r.post_id} {r.root_id} {r.channel_id} {_word(r.channel)} {_word(r.author)} "
            f"{r.at} {int(r.dm)} {int(r.reminded)}\n" for r in rows[-MAX:]))
    except OSError as exc:
        log.info("unanswered posts not kept: %s", exc)


def add(uid: str, post: Post, *, channel: str, author: str, dm: bool, now: float | None = None) -> None:
    """A post that needs an answer reached the agent. Never raises."""
    if not (uid and post.id):
        return
    rows = [r for r in read(uid, now) if r.post_id != post.id]
    rows.append(Open(post.id, post.root_id or post.id, post.channel_id or "-",
                     DM if dm else (channel or post.channel_id or "-"), author or post.user_id or "-",
                     int(time.time() if now is None else now), dm))
    _write(uid, rows)


def answered(uid: str, channel_id: str, post: Post) -> None:
    """The agent posted: in a thread, that thread's open posts are answered; in
    a DM, every open post of that DM. Never raises."""
    if not uid:
        return
    try:
        rows = read(uid)
    except Exception:  # noqa: BLE001 — bookkeeping never fails the post
        return
    if not rows:
        return
    thread = post.root_id or post.id
    left = [r for r in rows if not (r.root_id == thread or (r.dm and r.channel_id == channel_id))]
    if len(left) != len(rows):
        _write(uid, left)


def remind(uid: str, now: float | None = None) -> list[dict[str, str]]:
    """The open posts not reminded of yet, as the hook API hands them out
    ({post_id, root_id, channel, author}); marks them reminded, so each blocks
    one turn end at most."""
    rows = read(uid, now)
    due = [r for r in rows if not r.reminded]
    if not due:
        return []
    _write(uid, [replace(r, reminded=True) for r in rows])
    return [{k: v for k, v in asdict(r).items() if k in ("post_id", "root_id", "channel", "author")}
            for r in due]


def remove(uid: str) -> None:
    if uid:
        path(uid).unlink(missing_ok=True)

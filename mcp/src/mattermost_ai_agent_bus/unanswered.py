"""Bus posts that reached the agent and have no answer by it in their thread
yet (RT-6.2 step 2 in docs/arc42/06-runtime-view.md).

A post that needs an answer (server.py decides at delivery, `_note_unanswered`)
is kept here until the agent answers in its thread (or, for a DM, in that DM),
or resolves the ask it belongs to. The Stop hook and the turn plugins read the
open ones not yet reminded of at a turn end (`due`), and mark them (`remind`)
only when they really block for them: an answer only in the terminal reaches
nobody on the bus, and a read that is dropped leaves the post due.

One line per post in ``unanswered-<uid>.env`` in the state directory, next to
``last-<uid>.env`` (ownpost.py), so a restarted server of the same bot keeps
them; the newest last:

    <post id> <root id> <channel id> <channel name|DM> <author> <at s> <dm 0|1> <reminded 0|1>

At most MAX are kept, none older than MAX_AGE_SEC. A reminded post blocks no
turn end again; it stays until answered or aged out.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from . import a2a, session_file
from .model import Post

log = logging.getLogger("mattermost_ai_agent_bus.unanswered")

MAX = 10
MAX_AGE_SEC = 24 * 3600
DM = "DM"
# Own posts that answer nothing: a line the server writes on its own (a
# reminder, the context line, a permission prompt, an ask mode's or mandate's
# acknowledgement) and a pure status: a sign-off, a HELLO, the roster. An ACK,
# RESULT or DONE is a fair answer and counts.
_NOT_ANSWER_KINDS = (*a2a.SERVER_OWN, a2a.PERMISSION, a2a.ASK_MODE, a2a.MANDATE)
_STATUS_ONLY = ("HELLO", "IDLE", "BYE", "ASSIGNMENTS")
# Set while the server posts a line of its own without a kind of its own (the
# permission relay's verdict and notes, resolve_ask's note): no answer either.
_server_line: contextvars.ContextVar[bool] = contextvars.ContextVar("server_line", default=False)


@contextlib.contextmanager
def server_line() -> Iterator[None]:
    """Posts made inside are the server's own lines: they answer no post."""
    token = _server_line.set(True)
    try:
        yield
    finally:
        _server_line.reset(token)


def answers(post: Post) -> bool:
    """Whether the agent's own post answers the open posts of its thread."""
    if _server_line.get() or post.kind in _NOT_ANSWER_KINDS or a2a.system(post):
        return False
    return a2a.word(a2a.strip_crown(post.message).strip()) not in _STATUS_ONLY


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
    """A post that needs an answer reached the agent."""
    if not (uid and post.id):
        return
    rows = [r for r in read(uid, now) if r.post_id != post.id]
    rows.append(Open(post.id, post.root_id or post.id, post.channel_id or "-",
                     DM if dm else (channel or post.channel_id or "-"), author or post.user_id or "-",
                     int(time.time() if now is None else now), dm))
    _write(uid, rows)


def _clear(uid: str, thread: str, dm_channel: str = "") -> None:
    rows = read(uid)
    left = [r for r in rows if not (r.root_id == thread or (dm_channel and r.dm and r.channel_id == dm_channel))]
    if len(left) != len(rows):
        _write(uid, left)


def answered(uid: str, channel_id: str, post: Post) -> None:
    """The agent posted: unless the post answers nothing (`answers`), in a
    thread that thread's open posts are answered; in a DM, every open post of
    that DM."""
    if uid and answers(post):
        _clear(uid, post.root_id or post.id, channel_id)


def resolved(uid: str, ask_id: str) -> None:
    """The agent resolved its ask: what waited in the ask's thread is settled."""
    if uid and ask_id:
        _clear(uid, ask_id)


def due(uid: str, now: float | None = None) -> list[dict[str, str]]:
    """The open posts not reminded of yet, as the hook API hands them out
    ({post_id, root_id, channel, author}). Marks nothing: the caller that
    blocks for them marks them (`remind`)."""
    return [{k: v for k, v in asdict(r).items() if k in ("post_id", "root_id", "channel", "author")}
            for r in read(uid, now) if not r.reminded]


def remind(uid: str, post_ids: Iterable[str]) -> int:
    """The caller blocked a turn end for these posts: each blocks no turn end
    again. The number marked."""
    wanted = set(post_ids)
    rows = read(uid)
    marked = [replace(r, reminded=True) if r.post_id in wanted and not r.reminded else r for r in rows]
    n = sum(1 for a, b in zip(rows, marked, strict=True) if a is not b)
    if n:
        _write(uid, marked)
    return n


def remove(uid: str) -> None:
    if uid:
        path(uid).unlink(missing_ok=True)

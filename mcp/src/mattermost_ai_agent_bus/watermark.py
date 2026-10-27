"""The bus's read watermark (RT-6.2 in docs/arc42/06-runtime-view.md): per
channel, the newest post this bot has handled. What came after it is what
may still wait; Mattermost's unread counters decide nothing.

Handled means handed to the agent (wait_for_events, the channel push), gated
(RT-6.16), or the bot's own. A channel without a line reads from the bot's
creation: nothing older can be for this session.

``read-<uid>.env`` in the private state directory, 0600, one line per channel:
``<channel_id> <post_id> <create_at>``. Keyed on the bot, not the session, so
the next server of the same bot (``claude --continue``, the workspace's next
server, RT-6.10) goes on from it; it goes when the bot is unregistered, and
with the host's state directory (RT-6.18).

A mark only moves forward, except for hold(): a post that reaches the inbox
behind the mark (it was on its way while the bot posted) pulls it back, so it
counts as pending until it is handed over.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import NamedTuple

from . import session_file

log = logging.getLogger(__name__)

_ID = re.compile(r"[a-z0-9]{1,64}")


class Mark(NamedTuple):
    post_id: str
    at: int


def path(uid: str) -> Path:
    return session_file.state_dir() / f"read-{uid}.env"


def read(uid: str) -> dict[str, Mark]:
    marks: dict[str, Mark] = {}
    try:
        lines = path(uid).read_text().splitlines() if uid else []
    except OSError:
        return marks
    for line in lines:
        parts = line.split()
        if len(parts) == 3 and _ID.fullmatch(parts[0]) and parts[2].isdigit():
            marks[parts[0]] = Mark(parts[1], int(parts[2]))
    return marks


def at(uid: str, channel_id: str) -> int:
    """The create_at of the channel's newest handled post, 0 for none."""
    mark = read(uid).get(channel_id)
    return mark.at if mark else 0


def _write(uid: str, marks: dict[str, Mark]) -> None:
    text = "".join(f"{cid} {m.post_id or '-'} {m.at}\n" for cid, m in sorted(marks.items()))
    try:
        session_file.write_private(path(uid), text)
    except OSError as exc:  # the mark stays behind: posts count once more, none is lost
        log.info("read watermark not written: %s", exc)


def advance(uid: str, channel_id: str, post_id: str, created: int) -> bool:
    """Move the channel's mark to this post, if it is newer than the mark."""
    if not (uid and channel_id and _ID.fullmatch(channel_id) and created > 0):
        return False
    marks = read(uid)
    if created <= marks.get(channel_id, Mark("", 0)).at:
        return False
    marks[channel_id] = Mark(post_id if _ID.fullmatch(post_id or "") else "-", created)
    _write(uid, marks)
    return True


def hold(uid: str, channel_id: str, created: int) -> None:
    """A post created at `created` waits unhandled: keep the mark before it."""
    marks = read(uid)
    mark = marks.get(channel_id)
    if mark is None or created <= 0 or mark.at < created:
        return
    marks[channel_id] = Mark("-", created - 1)
    _write(uid, marks)


def remove(uid: str) -> None:
    if uid:
        path(uid).unlink(missing_ok=True)

"""Open asks of this bot: posts made with ask_human that wait for an answer (RT-6.11).

A human answers an ask by replying in its thread or with a reaction (👍/✅ yes,
👎/❌ no). Mattermost raises no unread counter for a reaction, so the turn hooks
and the listener would never notice one; this file tells them which posts to
look at. It lives next to the other per-bot state, asks-<uid>.env:

    ASK=<post_id> <epoch>          an open ask
    SEEN=<post_id>:<user_id>:<emoji>   a reaction already handed to the agent

The background listener (bin/mm-agent-listen.sh) wakes the agent for any
reaction on an open ask that is not SEEN. resolve() drops the ask and its SEEN
lines. Asks older than MAX_AGE_SEC are forgotten.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import session_file

MAX_AGE_SEC = 7 * 86400

YES = frozenset({"+1", "thumbsup", "white_check_mark", "heavy_check_mark",
                 "ballot_box_with_check"})
NO = frozenset({"-1", "thumbsdown", "x", "no_entry", "no_entry_sign"})


def answer_of(emoji: str) -> str | None:
    """yes / no for the agreed emoji, None for anything else (a remark only)."""
    if emoji in YES:
        return "yes"
    if emoji in NO:
        return "no"
    return None


def _path(uid: str) -> Path:
    return session_file.state_dir() / f"asks-{uid}.env"


def _read(uid: str) -> tuple[dict[str, float], set[str]]:
    asks: dict[str, float] = {}
    seen: set[str] = set()
    try:
        lines = _path(uid).read_text().splitlines()
    except OSError:
        return asks, seen
    now = time.time()
    for line in lines:
        key, _, val = line.partition("=")
        if key == "ASK":
            pid, _, at = val.partition(" ")
            try:
                when = float(at)
            except ValueError:
                when = now
            if pid and now - when < MAX_AGE_SEC:
                asks[pid] = when
        elif key == "SEEN" and val:
            seen.add(val)
    return asks, seen


def _write(uid: str, asks: dict[str, float], seen: set[str]) -> None:
    seen = {s for s in seen if s.split(":", 1)[0] in asks}
    lines = [f"ASK={pid} {int(at)}" for pid, at in asks.items()]
    lines += [f"SEEN={s}" for s in sorted(seen)]
    path = _path(uid)
    if not lines:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)


def open_asks(uid: str) -> list[str]:
    return list(_read(uid)[0])


def is_open(uid: str, post_id: str) -> bool:
    return post_id in _read(uid)[0]


def add(uid: str, post_id: str) -> None:
    asks, seen = _read(uid)
    asks[post_id] = time.time()
    _write(uid, asks, seen)


def seen_key(post_id: str, user_id: str, emoji: str) -> str:
    return f"{post_id}:{user_id}:{emoji}"


def was_seen(uid: str, key: str) -> bool:
    return key in _read(uid)[1]


def mark_seen(uid: str, key: str) -> None:
    asks, seen = _read(uid)
    if key.split(":", 1)[0] in asks:
        seen.add(key)
        _write(uid, asks, seen)


def resolve(uid: str, post_id: str) -> bool:
    asks, seen = _read(uid)
    if asks.pop(post_id, None) is None:
        return False
    _write(uid, asks, seen)
    return True

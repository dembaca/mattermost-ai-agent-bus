"""Open asks of this bot: posts made with ask_human that wait for an answer (RT-6.11).

A human answers an ask by replying in its thread or with a reaction (👍/✅ yes,
👎/❌ no). Mattermost raises no unread counter for a reaction, so the turn hooks
and the listener would never notice one; this file tells them which posts to
look at. It lives next to the other per-bot state, asks-<uid>.env:

    ASK=<post_id> <epoch> <human> <root_id>  an open ask, whom it asks (- for
                                             nobody), the thread it is in
    SEEN=<post_id>:<user_id>:<emoji>   a reaction already handed to the agent
    ANSWERED=<post_id>                 a trusted human answered it (RT-6.9): the
                                       wait is counted once, whatever comes next

The background listener (bin/mm-agent-listen.sh) wakes the agent for any
reaction on an open ask that is not SEEN. resolve() drops the ask and its SEEN
lines. A yes approves only within APPROVAL_SEC of the ask: a later answer is
handed to the agent as expired. Asks older than MAX_AGE_SEC are forgotten.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from . import session_file

MAX_AGE_SEC = 7 * 86400
# A yes approves only this long after the ask (RT-6.11): a day later the
# situation it names may have changed. Kept until MAX_AGE_SEC to say so.
APPROVAL_SEC = 86400

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
    asks, seen, _ = _read_all(uid)
    return asks, seen


def _read_all(uid: str) -> tuple[dict[str, float], set[str], dict[str, str]]:
    """Open asks with their time, the SEEN keys, and "<human> <root_id>" per ask."""
    asks: dict[str, float] = {}
    whom: dict[str, str] = {}
    seen: set[str] = set()
    try:
        lines = _path(uid).read_text().splitlines()
    except OSError:
        return asks, seen, whom
    now = time.time()
    for line in lines:
        key, _, val = line.partition("=")
        if key == "ASK":
            parts = val.split()
            pid = parts[0] if parts else ""
            try:
                when = float(parts[1])
            except (IndexError, ValueError):
                when = now
            if pid and now - when < MAX_AGE_SEC:
                asks[pid] = when
                if len(parts) > 2:
                    whom[pid] = " ".join(parts[2:4])
        elif key == "SEEN" and val:
            seen.add(val)
    return asks, seen, whom


def _read_answered(uid: str) -> set[str]:
    try:
        lines = _path(uid).read_text().splitlines()
    except OSError:
        return set()
    return {v for k, _, v in (line.partition("=") for line in lines) if k == "ANSWERED" and v}


def _write(uid: str, asks: dict[str, float], seen: set[str],
           whom: dict[str, str] | None = None, done: set[str] | None = None) -> None:
    if whom is None:
        whom = _read_all(uid)[2]
    if done is None:
        done = _read_answered(uid)
    seen = {s for s in seen if s.split(":", 1)[0] in asks}
    lines = [f"ASK={pid} {int(at)} {whom[pid]}".rstrip() if pid in whom
             else f"ASK={pid} {int(at)}" for pid, at in asks.items()]
    lines += [f"SEEN={s}" for s in sorted(seen)]
    lines += [f"ANSWERED={p}" for p in sorted(done) if p in asks]
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


def add(uid: str, post_id: str, human: str = "", root_id: str = "") -> None:
    asks, seen, whom = _read_all(uid)
    asks[post_id] = time.time()
    if human or root_id:
        whom[post_id] = f"{human.lower() or '-'} {root_id}".rstrip()
    _write(uid, asks, seen, whom)


def open_to(uid: str, human: str) -> list[str]:
    """Open asks of this bot to that human: one at a time (RT-6.11)."""
    asks, _, whom = _read_all(uid)
    return [pid for pid in asks if whom.get(pid, "").split(" ")[0] == human.lower()]


def expired(uid: str, post_id: str, at: float = 0, *, thread: bool = False) -> bool:
    """An answer given at `at` (default now) to that open ask — with thread, to
    the asks in the thread with that root — comes too late to approve (RT-6.11)."""
    asks, _, whom = _read_all(uid)
    when = at or time.time()
    hits = [t for pid, t in asks.items()
            if pid == post_id or (thread and whom.get(pid, "").split(" ")[1:] == [post_id])]
    return bool(hits) and all(when - t >= APPROVAL_SEC for t in hits)


def answered(uid: str, post_id: str, at: float = 0, *, thread: bool = False) -> tuple[str, int] | None:
    """A trusted human's answer at `at` (default now) to an ask_human ask: with
    thread, to the asks in the thread with that root. Returns the ask and how
    many seconds it waited, once per ask (RT-6.9); None for a later answer, and
    for a post that is no ask of an agent to a human (a permission prompt, RT-6.13)."""
    asks, seen, whom = _read_all(uid)
    done = _read_answered(uid)
    hits = sorted((t, pid) for pid, t in asks.items()
                  if pid in whom and pid not in done
                  and (pid == post_id or (thread and whom[pid].split(" ")[1:] == [post_id])))
    if not hits:
        return None
    t, pid = hits[0]
    _write(uid, asks, seen, whom, done | {pid})
    return pid, max(0, round((at or time.time()) - t))


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

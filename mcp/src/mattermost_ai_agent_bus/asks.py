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
    TITLE=<post_id> <headline>         the ask's bold title: its links show it
                                       (RT-6.15 step 2); kept after resolve for
                                       the newest TITLES asks, open or not
    ANSWERS=<post_id> <base64 of json>  the answers as posted, by position: a
                                       reaction's choice or yes/no names one
                                       (RT-6.11); kept like TITLE
    QUESTION=<post_id> <human> <0|1>   a question to the home's 👑 (RT-6.25): the
                                       human asked if it does not answer in time
                                       (- once settled), whether it blocks; kept
                                       like TITLE, so an answer is known as advice
    QUEUED=<target> <base64 of json>   an ask to <target> that waits behind an
                                       open one (RT-6.11 step 2): every field of
                                       ask_human on one line, posted when the
                                       open one is answered or resolved

The background listener (bin/mm-agent-listen.sh) wakes the agent for any
reaction on an open ask that is not SEEN. resolve() drops the ask and its SEEN
lines. A yes approves only within APPROVAL_SEC of the ask: a later answer is
handed to the agent as expired. Asks older than MAX_AGE_SEC are forgotten.
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from . import session_file

MAX_AGE_SEC = 7 * 86400
# A yes approves only this long after the ask (RT-6.11): a day later the
# situation it names may have changed. Kept until MAX_AGE_SEC to say so.
APPROVAL_SEC = 86400
# Headlines kept, open asks or resolved: an IDLE right after resolve_ask still
# links `ask <id>` by its headline (RT-6.15 step 2). Not an open ask.
TITLES = 50
# Asks that wait behind an open ask to the same target, per target (RT-6.11).
QUEUE_MAX = 5

YES = frozenset({"+1", "thumbsup", "white_check_mark", "heavy_check_mark",
                 "ballot_box_with_check"})
NO = frozenset({"-1", "thumbsdown", "x", "no_entry", "no_entry_sign"})
# A choice among three or four alternatives (RT-6.11 step 1): one emoji each.
CHOICES = ("one", "two", "three", "four")
KEYCAPS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣")


def answer_of(emoji: str) -> str | None:
    """yes / no for the agreed emoji, None for anything else (a remark only)."""
    if emoji in YES:
        return "yes"
    if emoji in NO:
        return "no"
    return None


def choice_of(emoji: str) -> int | None:
    """The 1-based alternative a keycap emoji picks, None for anything else."""
    return CHOICES.index(emoji) + 1 if emoji in CHOICES else None


def is_answer(emoji: str) -> bool:
    """An emoji that answers an ask: yes, no or a choice; a remark answers nothing."""
    return answer_of(emoji) is not None or choice_of(emoji) is not None


# An emoji an agent put in front of an answer, `👍 = yes`: stripped, the server
# gives the emojis by position (RT-6.11 step 1).
_MARK = re.compile(r"^\s*(?:👍|✅|👎|❌|[1-4]\ufe0f?\u20e3)\s*[=:\-–]?\s*")
_SPACED_SLASH = re.compile(r"\s+/\s+")


def clean_answer(text: str) -> str:
    """One answer as plain text: no leading emoji mark, one line."""
    return " ".join(_MARK.sub("", str(text), count=1).split())


def normalize_answers(answers: list[str] | str | None) -> list[str]:
    """The answers of an ask as a list of plain texts (RT-6.11 step 1). A list
    is taken as it is; a `str` (one version only, 0.71.15) is split at ` / ` with
    a space on both sides, never at a bare `/`. Marks are stripped in both."""
    if answers is None:
        return []
    items = _SPACED_SLASH.split(answers) if isinstance(answers, str) else list(answers)
    return [c for c in (clean_answer(x) for x in items) if c]


def valid_answers(answers: list[str]) -> bool:
    """None or two to four of them; anything else is no choice."""
    return len(answers) in (0, 2, 3, 4)


def marks(count: int) -> list[str]:
    """The emojis the server gives `count` answers by position: 👍 (the go) and
    👎 for two, the keycaps for three or four."""
    return ["👍", "👎"] if count == 2 else list(KEYCAPS[:count])


def answer_line(answers: list[str], decision: bool) -> str:
    """The `Answer` field's text: every answer with its own emoji, by position,
    none twice; none given, 👍 yes (to the decision) and 👎 no."""
    alts = answers if 2 <= len(answers) <= len(CHOICES) else []
    if alts:
        parts = [f"{m} = {a}" for m, a in zip(marks(len(alts)), alts, strict=True)]
    else:
        parts = ["👍 = yes to the decision" if decision else "👍 = yes", "👎 = no"]
    return " · ".join(parts) + " · or reply in this thread"


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


def _lines(uid: str, key: str) -> list[str]:
    try:
        lines = _path(uid).read_text().splitlines()
    except OSError:
        return []
    return [v for k, _, v in (line.partition("=") for line in lines) if k == key and v]


def _read_answered(uid: str) -> set[str]:
    return set(_lines(uid, "ANSWERED"))


def titles(uid: str) -> dict[str, str]:
    """The headline of each of the newest TITLES asks, open or resolved, that
    has one (RT-6.11 step 1, RT-6.15 step 2), oldest first."""
    out = {}
    for v in _lines(uid, "TITLE"):
        pid, _, title = v.partition(" ")
        if title.strip():
            out[pid] = title.strip()
    return out


def answers_of(uid: str, post_id: str) -> list[str]:
    """The answers an ask was posted with, in order (RT-6.11); empty when it
    had none or is forgotten."""
    for v in reversed(_lines(uid, "ANSWERS")):
        pid, _, blob = v.partition(" ")
        if pid == post_id:
            try:
                data = json.loads(base64.b64decode(blob))
            except (ValueError, TypeError):
                return []
            return [str(x) for x in data] if isinstance(data, list) else []
    return []


def _all_answers(uid: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in _lines(uid, "ANSWERS"):
        pid, _, blob = v.partition(" ")
        out.pop(pid, None)
        out[pid] = blob
    return out


def questions(uid: str) -> dict[str, str]:
    """The questions to the 👑 (RT-6.25), open or not: "<human> <0|1>" each,
    oldest first."""
    out = {}
    for v in _lines(uid, "QUESTION"):
        pid, _, rest = v.partition(" ")
        out[pid] = " ".join(rest.split())
    return out


def _write(uid: str, asks: dict[str, float], seen: set[str],
           whom: dict[str, str] | None = None, done: set[str] | None = None,
           title: dict[str, str] | None = None, question: dict[str, str] | None = None,
           answers: dict[str, str] | None = None,
           queue: list[dict[str, Any]] | None = None) -> None:
    if whom is None:
        whom = _read_all(uid)[2]
    if done is None:
        done = _read_answered(uid)
    heads = titles(uid)
    for pid, head in (title or {}).items():
        heads.pop(pid, None)
        heads[pid] = head
    ans = _all_answers(uid)
    for pid, blob in (answers or {}).items():
        ans.pop(pid, None)
        ans[pid] = blob
    qs = questions(uid)
    for pid, val in (question or {}).items():
        qs.pop(pid, None)
        qs[pid] = val
    seen = {s for s in seen if s.split(":", 1)[0] in asks}
    lines = [f"ASK={pid} {int(at)} {whom[pid]}".rstrip() if pid in whom
             else f"ASK={pid} {int(at)}" for pid, at in asks.items()]
    lines += [f"SEEN={s}" for s in sorted(seen)]
    lines += [f"ANSWERED={p}" for p in sorted(done) if p in asks]
    lines += [f"TITLE={p} {t}" for p, t in list(heads.items())[-TITLES:]]
    lines += [f"ANSWERS={p} {b}" for p, b in list(ans.items())[-TITLES:]]
    lines += [f"QUESTION={p} {v}" for p, v in list(qs.items())[-TITLES:]]
    lines += [f"QUEUED={e['target']} " + base64.b64encode(json.dumps(e).encode()).decode()
              for e in (queued(uid) if queue is None else queue)]
    path = _path(uid)
    if not lines:
        path.unlink(missing_ok=True)
        return
    session_file.write_private(path, "\n".join(lines) + "\n")


def open_asks(uid: str) -> list[str]:
    return list(_read(uid)[0])


def waiting(uid: str) -> list[tuple[str, float, str, str]]:
    """Asks to a human no trusted human has answered yet (RT-6.19): post id,
    when, the human, the thread root ('' for a top-level ask). A permission
    prompt names no human here and is left out, and so is a question to the
    👑 (RT-6.25: its server asks the human after the wait instead)."""
    asks, _, whom = _read_all(uid)
    done = _read_answered(uid)
    routed = questions(uid)
    out = []
    for pid, at in asks.items():
        human, _, root = whom.get(pid, "").partition(" ")
        if human and human != "-" and pid not in done and pid not in routed:
            out.append((pid, at, human, root))
    return out


def is_open(uid: str, post_id: str) -> bool:
    return post_id in _read(uid)[0]


def add(uid: str, post_id: str, human: str = "", root_id: str = "", title: str = "",
        *, question: str = "", blocking: bool = False,
        answers: list[str] | None = None) -> None:
    """An open ask to `human`; with question, a question to the 👑 `human`
    that falls back to the human `question` names (RT-6.25)."""
    asks, seen, whom = _read_all(uid)
    asks[post_id] = time.time()
    if human or root_id:
        whom[post_id] = f"{human.lower() or '-'} {root_id}".rstrip()
    _write(uid, asks, seen, whom, title={post_id: " ".join(title.split())} if title.strip() else None,
           question={post_id: f"{question.lower()} {int(blocking)}"} if question else None,
           answers={post_id: base64.b64encode(json.dumps(answers).encode()).decode()}
           if answers else None)


def questions_open(uid: str) -> list[tuple[str, float, str, str]]:
    """Open questions to the 👑 not yet settled (RT-6.25 step 6): post id,
    when, the 👑, the human to ask if it does not answer."""
    asks, _, whom = _read_all(uid)
    out = []
    for pid, val in questions(uid).items():
        human = val.split(" ")[0] if val else "-"
        if pid in asks and human != "-":
            out.append((pid, asks[pid], whom.get(pid, "").split(" ")[0], human))
    return out


def question_settled(uid: str, post_id: str) -> None:
    """The 👑 or a trusted human answered: no human is asked for it."""
    asks, seen = _read(uid)
    blocking = questions(uid).get(post_id, "- 0").split(" ")[-1]
    _write(uid, asks, seen, question={post_id: f"- {blocking}"})


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


def queued(uid: str, target: str = "") -> list[dict[str, Any]]:
    """Asks waiting behind an open ask (RT-6.11 step 2), oldest first: the
    fields of ask_human and `target`, whom it goes to; of that target only,
    when given. A line that does not read, or older than MAX_AGE_SEC, is gone."""
    out = []
    now = time.time()
    for v in _lines(uid, "QUEUED"):
        who, _, blob = v.partition(" ")
        try:
            entry = json.loads(base64.b64decode(blob))
        except ValueError:
            continue
        if (isinstance(entry, dict) and entry.get("target") == who
                and now - float(entry.get("at", now)) < MAX_AGE_SEC
                and (not target or who == target.lower())):
            out.append(entry)
    return out


@contextlib.contextmanager
def _queue_lock(uid: str) -> Any:
    """An exclusive advisory lock around a change of the queue, so two servers of
    one bot do not both take the same entry. A state dir that cannot be written
    locks nothing."""
    fd = -1
    try:
        fd = os.open(session_file.state_dir() / f"asks-{uid}.lock", os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        pass
    try:
        yield
    finally:
        if fd >= 0:
            os.close(fd)


def enqueue(uid: str, target: str, fields: dict[str, Any]) -> tuple[int, str]:
    """Queue an ask to `target` behind its open one: its place in that queue
    and its id (`q-…`, what resolve_ask takes to withdraw it). The caller
    checks QUEUE_MAX."""
    entry = {**fields, "target": target.lower(), "at": time.time(), "id": f"q-{uuid.uuid4().hex[:8]}"}
    with _queue_lock(uid):
        everything = queued(uid) + [entry]
        asks, seen = _read(uid)
        _write(uid, asks, seen, queue=everything)
    return len([e for e in everything if e["target"] == entry["target"]]), entry["id"]


def claim(uid: str, qid: str) -> dict[str, Any] | None:
    """Take the entry `qid` out of the queue (it is posted, or withdrawn): the
    entry, or None when it is gone, so only one caller gets it."""
    with _queue_lock(uid):
        everything = queued(uid)
        hit = next((e for e in everything if e.get("id") == qid), None)
        if hit is not None:
            everything.remove(hit)
            asks, seen = _read(uid)
            _write(uid, asks, seen, queue=everything)
        return hit


def unclaim(uid: str, entry: dict[str, Any]) -> None:
    """Put a claimed entry back in front: it was not posted."""
    with _queue_lock(uid):
        asks, seen = _read(uid)
        _write(uid, asks, seen, queue=[entry, *queued(uid)])


def queued_targets(uid: str) -> list[str]:
    return sorted({e["target"] for e in queued(uid)})


def whom_of(uid: str, post_id: str) -> tuple[str, str]:
    """The human an open ask goes to and the thread it is in ('' for none)."""
    human, _, root = _read_all(uid)[2].get(post_id, "").partition(" ")
    return ("" if human == "-" else human), root


def blocking(uid: str, human: str) -> list[str]:
    """Open asks to that human no trusted human answered (as the agent saw it):
    they hold the queue back. Answered but not yet resolved ones do not."""
    done = _read_answered(uid)
    return [p for p in open_to(uid, human) if p not in done]

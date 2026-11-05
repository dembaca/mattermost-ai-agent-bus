"""Reminders for open asks (RT-6.19 in docs/arc42/06-runtime-view.md).

A human who forgot an ask is reminded by the MCP server of the agent that
asked, from its heartbeat (lease.heartbeat): no model, no tokens. The first
reminder comes MM_ASK_REMIND_SEC (default 1200 s) after the ask, the next
after half that, doubling (10, 20, 40 … min), each gap capped at
MM_REMIND_MAX_SEC (default 8 h). A reminder is a short reply in the ask's
thread with the human's @-mention and the ask's title: part of the ask, the
one place a human is mentioned (RT-6.11). It stops once the ask is answered
(a yes/no reaction by the human or a trusted human, a reply by the human in
the ask's thread, a trusted human's answer the agent collected) or resolved,
at 24 h (asks.APPROVAL_SEC: a yes
approves nothing then, the coordinator's stall digest says to ask again) and
with the session, whose heartbeat it runs in.

remind-<uid>.env in the state dir keeps what was sent, so a restarted server
(`claude --continue`) sends nothing early; remind-<uid>.lock (flock) keeps two
servers of one bot from both sending:

    R=<ask post id> <reminders sent> <epoch of the last one>
    STOP=<ask post id>                 answered on Mattermost: no more
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from . import a2a, askroute, asks, lease, session_file, stats, tasks
from .model import Mark

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

_TITLE = re.compile(r"\*\*(.+?)\*\*")
TITLE = 90
# Posts the next ask queued for a target (RT-6.11 step 2); the server sets it.
advance: Callable[[ChatPort, str, set[str]], Awaitable[None]] | None = None


def _sec(name: str, default: int) -> int:
    s = os.environ.get(name, "")
    return max(60, int(s)) if s.isdigit() else default


def ask_remind_sec() -> int:
    """How long after an ask the first reminder comes (MM_ASK_REMIND_SEC)."""
    return _sec("MM_ASK_REMIND_SEC", 1200)


def remind_max_sec() -> int:
    """The longest gap between two reminders or stall wakes (MM_REMIND_MAX_SEC)."""
    return _sec("MM_REMIND_MAX_SEC", 28800)


def due(asked: float, sent: int, last: float) -> float:
    """When the next reminder is due: ask_remind_sec after the ask, then half
    that after the last reminder, doubling, each gap capped."""
    first, cap = ask_remind_sec(), remind_max_sec()
    if sent <= 0:
        return asked + min(first, cap)
    return last + min(first / 2 * 2.0 ** min(sent - 1, 32), cap)


def _path(uid: str) -> Path:
    return session_file.state_dir() / f"remind-{uid}.env"


def _read(uid: str) -> tuple[dict[str, tuple[int, float]], set[str]]:
    sent: dict[str, tuple[int, float]] = {}
    stop: set[str] = set()
    try:
        lines = _path(uid).read_text().splitlines()
    except OSError:
        return sent, stop
    for line in lines:
        key, _, val = line.partition("=")
        parts = val.split()
        if key == "R" and len(parts) == 3 and parts[1].isdigit() and parts[2].replace(".", "", 1).isdigit():
            sent[parts[0]] = (int(parts[1]), float(parts[2]))
        elif key == "STOP" and val:
            stop.add(val)
    return sent, stop


def _write(uid: str, sent: dict[str, tuple[int, float]], stop: set[str]) -> None:
    lines = [f"R={pid} {n} {at:.3f}" for pid, (n, at) in sent.items()]
    lines += [f"STOP={pid}" for pid in sorted(stop)]
    path = _path(uid)
    try:
        if not lines:
            path.unlink(missing_ok=True)
        else:
            session_file.write_private(path, "\n".join(lines) + "\n")
    except OSError as exc:
        log.debug("reminders not saved: %s", exc)


def _ago(sec: float) -> str:
    minutes = max(1, round(sec / 60))
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60} h {minutes % 60} min".replace(" 0 min", "")


def text(human: str, title: str, waited: float, more: int = 0) -> str:
    """The reminder: the human's mention, the ask's title, how long it waits,
    and how many more asks of the agent wait behind it (RT-6.11 step 2)."""
    head = f"**{title}**" if title else "your answer to the ask above"
    tail = f" · {more} more waiting" if more else ""
    return (f"{a2a.symbol(Mark.REMINDER)} @{human} — reminder: {head} · waiting {_ago(waited)} · "
            f"{a2a.symbol(Mark.YES)}/{a2a.symbol(Mark.NO)} on the ask, or reply here{tail}")


async def _answered(client: ChatPort, uid: str, pid: str, root: str,
                    human: str) -> tuple[bool, str]:
    """Whether the ask is answered on Mattermost (tasks.ask_answered: a yes/no
    reaction by the human or a trusted human, a reply by the human in its
    thread after it; or the ask gone), and its title."""
    reactions = await client.reactions(pid)
    posts = (await client.thread(root or pid)).posts
    ask = next((p for p in posts if p.id == pid), None)
    if ask is None or ask.delete_at:
        return True, ""
    ids = {p.user_id for p in posts} | {r.user_id for r in reactions}
    names = await client.usernames(ids - {"", uid})
    if tasks.ask_answered(ask, posts, reactions, names, human, await client.trusted_humans()):
        return True, ""
    title = _TITLE.search(ask.message)
    return False, " ".join(title[1].split())[:TITLE] if title else ""


async def tick(client: ChatPort, uid: str, now: float | None = None) -> float | None:
    """Send the reminders that are due (RT-6.19); seconds until the next one,
    None without one to come. Called by the heartbeat; raises on a bus error,
    which the heartbeat swallows (the reminder is sent at the next tick)."""
    now = now or time.time()
    lock = _lock(uid)
    if lock is None:
        return None  # another server of this bot is sending: look at the next tick
    try:
        # RT-6.25 step 6: a question the 👑 left unanswered goes to the human,
        # under the same lock, so two servers of one bot do not both ask.
        routed = await askroute.tick(client, uid, now)
        due = await _tick(client, uid, now)
        await _advance(client, uid)
        return min((d for d in (routed, due) if d is not None), default=None)
    finally:
        os.close(lock)


def _lock(uid: str) -> int | None:
    """An exclusive advisory lock on remind-<uid>.lock around read, decide,
    post and write, so two servers of one bot send no reminder twice. None
    while another holds it; a state dir that cannot be written locks nothing."""
    path = session_file.state_dir() / f"remind-{uid}.lock"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        log.debug("reminder lock not taken: %s", exc)
        return os.open(os.devnull, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


async def _advance(client: ChatPort, uid: str) -> None:
    """RT-6.11 step 2: for each target whose open asks are all answered (the
    rule that stops the reminders), the next queued ask is posted. An expired
    ask stays a block until the agent closes it. Never raises."""
    if advance is None:
        return
    for key in asks.queued_targets(uid):
        try:
            answered: set[str] = set()
            for pid in asks.blocking(uid, key):
                if asks.expired(uid, pid):
                    break
                human, root = asks.whom_of(uid, pid)
                if pid in asks.questions(uid):  # a question to the 👑 (RT-6.25)
                    done = await askroute._answered(client, uid, pid, human)
                else:
                    done, _ = await _answered(client, uid, pid, root, human)
                if not done:
                    break
                answered.add(pid)
            else:
                await advance(client, key, answered)
        except Exception as exc:  # noqa: BLE001 — the next tick tries again
            log.info("queued ask to %s not posted: %s", key, exc)


async def _tick(client: ChatPort, uid: str, now: float) -> float | None:
    open_asks = asks.waiting(uid)
    sent, stop = _read(uid)
    ids = {pid for pid, *_ in open_asks}
    sent = {pid: v for pid, v in sent.items() if pid in ids}
    stop &= ids
    nxt: float | None = None
    try:
        for pid, asked, human, root in open_asks:
            if pid in stop:
                continue
            n, last = sent.get(pid, (0, 0.0))
            when = due(asked, n, last)
            if when >= asked + asks.APPROVAL_SEC:
                continue  # a yes approves nothing any more: no reminder (RT-6.11)
            if when > now:
                nxt = when if nxt is None else min(nxt, when)
                continue
            done, title = await _answered(client, uid, pid, root, human)
            if done:
                stop.add(pid)
                continue
            message = lease.with_crown(text(human, title, now - asked, len(asks.queued(uid, human))), uid,
                                       here=await lease.crown_here(client, uid, root_id=root or pid))
            stats.record("post", chars=len(message))
            await client.post(message, root_id=root or pid, props=a2a.props(kind=a2a.REMINDER))
            stats.record("remind", id=pid)
            sent[pid] = (n + 1, now)
            when = due(asked, n + 1, now)
            if when < asked + asks.APPROVAL_SEC:
                nxt = when if nxt is None else min(nxt, when)
    finally:
        _write(uid, sent, stop)
    return None if nxt is None else max(0.0, nxt - now)


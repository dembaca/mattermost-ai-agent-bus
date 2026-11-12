"""The 👑's receipts and the claim over a missing one (ADR-007 steps 3 to 5 and
amendment step 2, phase E; RT-6.6 step 10, RT-6.12 step 7, RT-6.25 step 6).

A post that wakes the crown holder in its home (a human's plain post, a mention
of the holder there, a question routed to it) gets a receipt from the holder's
server, no model turn:

- **👀 when the agent has it.** A push is taken up (life.py: a turn begins or a
  tool call comes), and a post `wait_for_events` returns is delivered with that
  return (the Stop hook's drain, a listen window's wake, the OpenCode plugin).
  Without a channel only that return delivers it.
- **⏳ at once while a turn runs** (decided 2026-11-12: every waking post, not
  only a push): the server has it, the agent gets it with the next turn; the
  👀 follows at the take-up.

Every other server of the home that would count the post as such a wake (its
own trust: a post it would gate starts nothing, RT-6.16) watches it, but only
when the holder's status shows the receipt marker (lease.RECEIPT_MARK; decided
2026-11-12: an older plugin's holder is judged as before). It hears the
holder's reaction on its WebSocket; without one it reads the reactions after
MM_ACK_WAIT_SEC and once more SECOND_LOOK_SEC later. Still neither 👀 nor ⏳:
the holder's server or its WebSocket is gone, or its session takes nothing up,
and this agent claims at its slot after the wait (lease.take_over), the oldest
that stands first, then posts crown_taken.

State lives in memory: a new server watches and owes nothing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import a2a, channel, gate, lease, life, tasks, unread
from .model import Mark, Post, Reaction

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

# ADR-007 step 3: the one more look after the wait.
SECOND_LOOK_SEC = 10.0
# A receipt owed longer than this is forgotten (the agent never took it up).
KEEP_SEC = 3600.0

# Set by server.py: the session takes pushes (a confirmed channel, RT-6.12), so a
# take-up delivers what was pushed; without one only wait_for_events does.
pushes: Callable[[], bool] = lambda: False  # noqa: E731

_client: ChatPort | None = None
# The holder's side: post id -> (it came during a turn, when): its 👀 waits for the take-up.
_owed: dict[str, tuple[bool, float]] = {}
_tasks: set[asyncio.Task[None]] = set()


@dataclass
class _Watch:
    at: float      # when this server saw the post
    holder: str    # the holder's id then
    due: float     # the next look
    step: int = 0  # 0 the look after the wait, 1 the second look, 2 the claim at the slot
    slot: int = 0


_watch: dict[str, _Watch] = {}


def reset() -> None:
    """A new process's state (tests)."""
    global _client
    _client = None
    _owed.clear()
    _watch.clear()


def wakes_holder(post: Post, cls: str, holder_id: str, holder_name: str, home: str) -> bool:
    """The post wakes the holder in its home (RT-6.12 step 3, RT-6.6 step 5): it
    names the holder (a question routed to it is such a mention, RT-6.25), or it
    is a human's plain post naming no agent and not everyone. Never a system
    post, the holder's own or an agent's quiet line."""
    if not post.id or post.channel_id != home or post.type or post.user_id == holder_id:
        return False
    if cls in gate.AGENTS and post.kind in a2a.QUIET:
        return False
    if unread.names_me(post.message, holder_id, holder_name):
        return True
    return (cls not in gate.AGENTS and not post.from_webhook
            and not tasks._mentions(post.message) and not channel.broadcast(post.message))


async def arrived(client: ChatPort, post: Post, uid: str, gated: bool) -> None:
    """A live post the inbox queued (websocket_inbox._on_post), after its gate.
    A crown_taken naming this agent goes to the lease (it yields at its tick);
    holding the crown, a post that wakes it owes a 👀 (and gets ⏳ now while a
    turn runs); else, with a marked holder, a post that wakes the holder is
    watched. Never raises."""
    global _client
    _client = client
    try:
        if post.kind == a2a.CROWN_TAKEN:
            if lease.a2a_holder(post.props) == uid and post.user_id != uid:
                lease.ousted(post.user_id, post.create_at / 1000)
            return
        if gated:
            return  # RT-6.16: a post this server gates starts nothing
        holding = lease.crown_held(uid)
        holder, name, marked = lease.holder()
        if not holding and not (holder and marked):
            return  # no holder seen, or an older plugin's: judged as before
        home = await client.work_channel_id()
        if post.channel_id != home:
            return
        if holding:
            me = (await client.get_me()).username
            cls = (await gate.author_classes(client, [post]))[0]
            if wakes_holder(post, cls, uid, me, home):
                now = time.time()
                for pid in [p for p, (_, at) in _owed.items() if now - at > KEEP_SEC]:
                    del _owed[pid]
                busy = life.in_turn()
                _owed[post.id] = (busy, now)
                if busy:
                    _spawn(_react(client, [post.id], Mark.WAITS))
            return
        if post.user_id == holder:
            return
        cls =(await gate.author_classes(client, [post]))[0]
        if cls in (gate.OTHER, gate.FOREIGN) and await client.trusted_humans():
            return  # its own trust (RT-6.16): it would not count the post as a wake
        if wakes_holder(post, cls, holder, name, home):
            now = time.time()
            _watch[post.id] = _Watch(at=now, holder=holder, due=now + life.ack_wait_sec())
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a receipt is never worth a lost post
        log.info("receipt: post %s not judged: %s", post.id or None, exc)


def _taken(boundary: bool) -> None:
    """life's take-up (ADR-007 amendment step 2): with pushes, the 👀 for what was
    pushed; a tool call takes up what came while no turn ran, a turn's begin or
    end also what came during the turn before."""
    if not pushes():
        return
    _give([p for p, (busy, _) in _owed.items() if boundary or not busy])


life.on_take_up(_taken)


def delivered(post_ids: Iterable[str]) -> None:
    """wait_for_events returned these posts: delivered, so the 👀 for those owed."""
    _give([p for p in post_ids if p in _owed])


def _give(ids: list[str]) -> None:
    for pid in ids:
        _owed.pop(pid, None)
    if ids and _client is not None:
        _spawn(_react(_client, ids, Mark.SEEN))


def _spawn(coro: Coroutine[Any, Any, None]) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()  # no loop (a sync caller): no receipt
        return
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _react(client: ChatPort, ids: list[str], mark: Mark) -> None:
    """Put the receipt on each post; a failed call is tried once more (the
    amendment's open question: a WebSocket dead one way)."""
    for pid in ids:
        for attempt in (1, 2):
            try:
                await client.add_reaction(pid, a2a.receipt_reaction(mark))
                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — the others may claim: a clean handover
                if attempt == 2:
                    log.info("receipt %s not set on %s: %s", mark.value, pid, exc)


def heard(reaction: Reaction) -> None:
    """A reaction on the WebSocket: the watched holder's 👀 or ⏳ ends the watch,
    so an answered post costs nothing."""
    w = _watch.get(reaction.post_id)
    if w is not None and reaction.user_id == w.holder and a2a.receipt_of(reaction.emoji_name):
        del _watch[reaction.post_id]


def due() -> float:
    """When the next watched post is due for a look (epoch s); 0 none."""
    return min((w.due for w in _watch.values()), default=0.0)


async def _receipted(client: ChatPort, pid: str, holder: str) -> bool:
    return any(r.user_id == holder and a2a.receipt_of(r.emoji_name)
               for r in await client.reactions(pid))


async def check(client: ChatPort) -> bool:
    """The watched posts whose look is due (ADR-007 step 3): a receipt ends the
    watch; none after the wait, one more look SECOND_LOOK_SEC later; none then,
    this agent's slot (lease.slot_over) after the wait, and a claim there
    (lease.take_over) when nobody took it before. True when it claimed."""
    now, wait = time.time(), life.ack_wait_sec()
    for pid, w in list(_watch.items()):
        if w.due > now:
            continue
        try:
            if w.step < 2 or w.slot > 1:
                if await _receipted(client, pid, w.holder):
                    del _watch[pid]
                    continue
            if w.step == 0:
                w.step, w.due = 1, w.at + wait + SECOND_LOOK_SEC
                continue
            if w.step == 1:
                w.slot = await lease.slot_over(client, w.holder)
                if not w.slot:
                    del _watch[pid]
                    continue
                w.step, w.due = 2, w.at + wait + lease.SLOT_SEC * w.slot
                if w.due > now:
                    continue
            del _watch[pid]
            if await lease.take_over(client, w.holder, pid):
                _watch.clear()  # this agent holds the crown now
                return True
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — unsure: no claim
            log.info("receipt watch of %s ended: %s", pid, exc)
            _watch.pop(pid, None)
    return False


async def missing(client: ChatPort, pid: str, holder_name: str) -> bool:
    """RT-6.25 step 6 (ADR-007 step 5): the question `pid` to the 👑 `holder_name`
    has neither its 👀 nor its ⏳ after the wait, from a holder that sets
    receipts, or no 👀 from one that turned silent (its ⏳ counts no more): the
    human is asked now. A holder without the marker is judged as before (False).
    Raises when Mattermost cannot be read."""
    holder, state = await lease.receipt_holder(client, holder_name)
    if not holder:
        return False
    found = [a2a.receipt_of(r.emoji_name) for r in await client.reactions(pid) if r.user_id == holder]
    if state == "silent":
        return Mark.SEEN not in found
    return not found

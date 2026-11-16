"""The home's channel banner shows what waits (RT-6.6 step 11, ADR-013 phase 1).

Only the server whose agent holds the home's crown writes the home's banner,
and only when the state it would show differs from the one it wrote last: a
stall (RT-6.19 step 2), asks waiting on a human (RT-6.11, RT-6.19 step 1),
else the crown and the agents by status. The inputs are what the holder reads
anyway, at its heartbeat's stall check (stall.check calls show); its own asks
and crown changes bring that read forward (stall.refresh_banner). Below the
port the state is a model.BannerState; the Mattermost adapter renders the
text, its marker, symbols and colours (client.render_banner).

At least BANNER_GAP_SEC between two writes of the home: a change inside the
gap is written at its end, with the latest state; a pending write checks the
crown when it fires and is dropped without it. Before a write the banner is
read: an enabled one without the marker is a human's and stops the writing
(one bus_info line, one DM to the responsible human per banner text). The
holder listens to `channel_updated` (observed): its own write comes back and
changes nothing; a changed marker banner, or a free one, is rewritten at the
gap's end. A 403 (no Enterprise Advanced licence, no banner permission) is
logged once and backs off for BACKOFF_SEC or until the crown is taken again;
other failures are logged without a storm and tried again with the next
change. A banner write is no post: it starts no quiet period of the stall
check. Display only: nothing here decides a crown, a mode, an ask or a
mandate from the banner's text (ADR-013 point 3).
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from . import lease
from .model import Banner, BannerKind, BannerState, Refused, RosterEntry

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

BANNER_GAP_SEC = 60.0          # between two writes of one home (ADR-013 point 1)
BACKOFF_SEC = 6 * 3600.0       # after a 403: licence or permission (point 5)
END_TIMEOUT_SEC = 0.75         # the clean end's write, inside the exit handler's bound
STATUS_WORDS = ("working", "listening", "idle")
SILENT = lease.SILENT

# Set by server.py: send a plain DM to the responsible human (a human's own
# banner, ADR-013 point 4); None: nobody to tell.
tell: Callable[[str], Awaitable[None]] | None = None


@dataclass
class _Home:
    cid: str = ""
    uid: str = ""
    home: str = ""
    want: BannerState | None = None      # the latest state to show
    written: BannerState | None = None   # the state handled last (None: write when due)
    text: str = ""                       # the text written last ('' none, or after a restart)
    at: float = 0.0                      # when the last write was tried, epoch s
    human: str = ""                      # a human's banner text that stops the writing
    drift: bool = False                  # a channel_updated to look at, at the gap's end
    told: set[str] = field(default_factory=set)
    off_until: float = 0.0               # a 403's back-off
    refused_logged: bool = False
    failed_logged: bool = False
    timer: asyncio.Task[None] | None = None
    client: ChatPort | None = None


_h = _Home()
_sleep = asyncio.sleep  # the gap's wait (the tests' clock replaces it)


def reset() -> None:
    """Forget everything (a new process, the tests)."""
    global _h
    if _h.timer is not None:
        _h.timer.cancel()
    _h = _Home()


# --- the state ---------------------------------------------------------------------

def _counts(roster: Sequence[RosterEntry], me: str) -> tuple[tuple[str, int], ...]:
    """The home's agents by status (RT-6.6 step 3, RT-6.14): working,
    listening, idle, and silent only when an agent is. The holder counts
    itself; an ambassador of another home does not count here."""
    n = dict.fromkeys((*STATUS_WORDS, SILENT), 0)
    seen = False
    for r in roster:
        if r.crown_of:
            continue
        seen = seen or r.username.lower() == me.lower()
        word = r.status if r.status in n else "idle"
        n[word] += 1
    if me and not seen:
        n["working"] += 1  # the holder's own server runs this check
    return tuple((w, c) for w, c in n.items() if c or w in STATUS_WORDS)


def state_of(found: Sequence[Mapping[str, Any]], quiet_ms: int, roster: Sequence[RosterEntry],
             holder: str, now: float, stall_sec: float, mode: str = "", mandate: str = "") -> BannerState:
    """The banner's state, the first that applies (ADR-013 point 2): a stall,
    asks waiting on a human, else the crown and the agents. `found` are the
    home's open items of the stall check (stall.items), oldest first. Never
    shown: the questions to the 👑 (they wait on no human) and permission
    prompts (they are no ask item, RT-6.13)."""
    shown = [i for i in found if i.get("what") != "question" and not i.get("channel")]
    asks = sorted((i for i in shown if i.get("what") == "ask" and i.get("human")),
                  key=lambda i: int(i.get("asked") or i.get("at") or 0))
    stalled = (any(not i.get("reminded") for i in shown) and quiet_ms > 0
               and now * 1000 - quiet_ms >= stall_sec * 1000)
    if stalled:
        first = shown[0]
        return BannerState(BannerKind.STALL, holder=holder, since_ms=quiet_ms, open=len(shown),
                           first=str(first.get("what") or ""), title=str(first.get("title") or ""),
                           next=str(first.get("next") or ""), asks=len(asks))
    if asks:
        humans = tuple(dict.fromkeys(str(i["human"]) for i in asks))
        oldest = asks[0]
        return BannerState(BannerKind.ASKS, holder=holder, asks=len(asks), humans=humans,
                           since_ms=int(oldest.get("asked") or oldest.get("at") or 0),
                           asked_by=str(oldest.get("by") or ""), ask_id=str(oldest.get("id") or ""))
    return BannerState(BannerKind.CROWN, holder=holder, counts=_counts(roster, holder),
                       mode="" if mode in ("", "direct") else mode, mandate=mandate)


# --- the writer --------------------------------------------------------------------

async def show(client: ChatPort, uid: str, state: BannerState, home: str = "") -> None:
    """The holder's state now (stall.check): written when it differs from the
    state written last, at the gap's end at the earliest. Never raises."""
    try:
        if not lease.crown_held(uid):
            return
        _h.client, _h.uid, _h.home = client, uid, home or _h.home
        if not _h.cid:
            _h.cid = await client.work_channel_id()
        _h.want = state
        await _due()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — display only
        log.debug("banner: %s", exc)


async def _due() -> None:
    """Write now, or at the gap's end (one timer, the latest state then)."""
    if _h.want is None or (_h.want == _h.written and not _h.drift) or time.time() < _h.off_until:
        return
    wait = _h.at + BANNER_GAP_SEC - time.time()
    if wait > 0:
        if _h.timer is None or _h.timer.done():
            _h.timer = asyncio.get_running_loop().create_task(_later(wait))
        return
    await _fire()


async def _later(wait: float) -> None:
    await _sleep(wait)
    _h.timer = None
    try:
        await _fire()
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — display only
        log.debug("banner: %s", exc)


async def _fire() -> None:
    """The write itself: the crown checked, the banner read (a human's stops
    it), the state rendered and written by the adapter."""
    client, state = _h.client, _h.want
    if client is None or state is None or (state == _h.written and not _h.drift):
        return
    if not lease.crown_held(_h.uid):
        log.debug("banner: the crown is gone, the pending write is dropped")
        return
    now = time.time()
    if now < _h.off_until:
        return
    _h.at = now
    try:
        current = await client.channel_banner(_h.cid)
        drift, _h.drift = _h.drift, False
        if not current.free and not current.marked:
            _h.written = state
            _human(current.text)
            return
        if drift and state == _h.written and current.enabled and current.text == _h.text:
            return  # the event was its own write after all: nothing changed
        _h.human = ""
        _h.text = await client.set_channel_banner(_h.cid, replace(state, at_ms=int(now * 1000)))
        _h.written = state
        _h.failed_logged = False
    except asyncio.CancelledError:
        raise
    except Refused as exc:
        _h.written, _h.drift = state, False
        if exc.status == 403:
            _h.off_until = now + BACKOFF_SEC
            if not _h.refused_logged:
                _h.refused_logged = True
                log.warning("channel banner not written (no Enterprise Advanced licence, or no banner "
                            "permission; tried again in 6 h or with the next crown): %s", exc)
            else:
                log.debug("channel banner: %s", exc)
        else:
            _failed(exc)
    except Exception as exc:  # noqa: BLE001 — a 5xx, a timeout: with the next change
        _h.written, _h.drift = state, False
        _failed(exc)


def _failed(exc: BaseException) -> None:
    if not _h.failed_logged:
        _h.failed_logged = True
        log.info("channel banner not written (tried again with the next change): %s", exc)
    else:
        log.debug("channel banner: %s", exc)


def _human(text: str) -> None:
    """A human's own banner (ADR-013 point 4): the bus writes none and says so,
    once per banner text, by DM to the responsible human."""
    _h.human = text
    if text in _h.told:
        return
    _h.told.add(text)
    where = f"~{_h.home}" if _h.home else "the home"
    log.info("channel banner: a human's own in %s, the bus writes none", where)
    if tell is None:
        return
    message = (f"The channel {where} has a banner of its own, set by a person. The agent bus "
               f"shows its banner there (what waits, who coordinates) again once that banner "
               f"is cleared or turned off.")
    task = asyncio.get_running_loop().create_task(_tell(message))
    _telling.add(task)
    task.add_done_callback(_telling.discard)


_telling: set[asyncio.Task[None]] = set()


async def _tell(message: str) -> None:
    try:
        if tell is not None:
            await tell(message)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — the bus_info line still says it
        log.info("banner: the responsible human not told: %s", exc)


async def observed(channel_id: str, banner: Banner) -> None:
    """A `channel_updated` of the home, as the holder hears it (drift, ADR-013
    point 1). Its own write changes nothing; a changed marker banner and a
    free one are written at the gap's end; any other text is a human's.
    Never raises."""
    try:
        if not _h.cid or channel_id != _h.cid or not lease.crown_held(_h.uid):
            return
        if banner.enabled and _h.text and banner.text == _h.text:
            return  # its own write
        if banner.free or banner.marked:
            # Looked at again at the gap's end: the banner is read then, so an
            # event of its own write that came before the answer changes nothing.
            _h.human = ""
            _h.drift = True
            await _due()
        elif banner.text != _h.human:
            _human(banner.text)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — display only
        log.debug("banner drift: %s", exc)


def crown_came() -> None:
    """The crown passed to this agent (RT-6.6 steps 2 and 4): write at the next
    look, and a 403's back-off ends (ADR-013 point 5)."""
    _h.written = None
    _h.off_until = 0.0


def crown_kept() -> None:
    """Two crowns settled and this agent kept it ("the older keeps it",
    RT-6.6 step 2): counts as the crown passing, so it writes."""
    _h.written = None


def end_sync(client: ChatPort, uid: str) -> str:
    """The clean end (RT-6.1 step 7, RT-6.18 step 7): one last neutral banner,
    `no coordinator since HH:MM`, from the holder that wrote the banner. Sync,
    bounded, fail-open; a short outcome for the log."""
    if not (_h.cid and _h.text) or _h.human:
        return "no banner of the bus"
    if not lease.crown_held(uid):
        return "no crown"
    if _h.timer is not None:
        _h.timer.cancel()
    now_ms = int(time.time() * 1000)
    try:
        _h.text = client.set_channel_banner_sync(
            _h.cid, BannerState(BannerKind.ENDED, since_ms=now_ms, at_ms=now_ms), timeout=END_TIMEOUT_SEC)
    except Exception as exc:  # noqa: BLE001 — exit handler: fail open
        return f"banner not ended: {type(exc).__name__}"
    _h.written = None
    return "banner ended"


def info() -> str | None:
    """bus_info's `banner` line: a human's own banner, or the back-off; None else."""
    if _h.human:
        return f"a human's own in ~{_h.home}" if _h.home else "a human's own in the home"
    if time.time() < _h.off_until:
        return ("not written: no Enterprise Advanced licence or no banner permission, tried again at "
                + time.strftime("%H:%M", time.localtime(_h.off_until)))
    return None

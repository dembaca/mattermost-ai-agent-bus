"""Presence lease, coordinator crown and roster (RT-6.6 in docs/arc42/06-runtime-view.md).

The MCP server keeps them. Mattermost carries them as the bot's user status and
custom status (emoji, text, expiry), read and written here and in client.py
only. Two files in the state dir hold the local side: presence-<uid>.env (the
state and label last reported, STATE / LABEL) and coord-<uid>.env (the crown
lease CROWN_UNTIL, a pending take, YIELD_UNTIL after a release); the status
line (bin/mm-agent-statusline.sh) reads CROWN_UNTIL.

Every status is a lease of MM_LEASE_SEC (default 900 s). heartbeat() renews it
every MM_HEARTBEAT_SEC (default 300 s) while the session lives, faster while a
take is pending, and claims or yields the crown on the way (tick). A session
that dies stops renewing, so its status, and a crown, run out on their own.
The crown is short (ADR-007 phase 1, RT-6.6 steps 2 and 6): its holder renews
it every MM_CROWN_BEAT_SEC (default 60 s) and it ends after MM_CROWN_LEASE_SEC
(default 150 s); the others look again when it would end, each after its slot
(SLOT_SEC times its place among those that stand), and the first that finds
it gone claims it.
Everything here fails open: presence is cosmetic and must never disturb a
session.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import a2a, channel, life, owners, ownpost, session_file, stats, usage
from .model import ChannelUser, Mark, Refused, RosterEntry, StatusMark

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

PENDING_POLL_SEC = 30.0
# ADR-007 step 2: the n-th agent that stands looks for a free crown n slots
# after the holder's status would end.
SLOT_SEC = 10.0
STATES = ("working", "listening", "idle", "bus", "offline")
# The status text's first word for an agent without a sign (ADR-007 amendment step 1).
SILENT = "silent"
_SILENT_SINCE = re.compile(rf"{SILENT} · no sign since (\d\d:\d\dZ)")
# A segment of a holder's status text: its server sets the receipts (👀, ⏳) on the
# posts that wake it (ADR-007 amendment, decided 2026-11-12; RT-6.6 step 10). A
# holder without it (an older plugin) is judged as before: nobody claims over it.
RECEIPT_MARK = a2a.symbol(Mark.SEEN)
ACTIONS = ("who", "take", "release", "tick")
_UID = re.compile(r"[a-z0-9]{26}")
# One lock per event loop: a status, take, release or tick reads and writes
# coord-<uid>.env around awaits, so they never interleave (a release during a
# tick's renewal would otherwise be undone by the tick's stale reading).
_LOCKS: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = weakref.WeakKeyDictionary()


def _lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _LOCKS.get(loop)
    if lock is None:
        lock = _LOCKS[loop] = asyncio.Lock()
    return lock


def _state_word(emoji: str) -> str:
    """The lease's status emoji as a word, for a status without text (the
    crown's is none)."""
    mark = a2a.status_of(emoji)
    return mark.value if mark and mark is not StatusMark.COORDINATOR else ""


def heartbeat_sec() -> float:
    try:
        return max(30.0, float(os.environ.get("MM_HEARTBEAT_SEC", "300")))
    except ValueError:
        return 300.0


def lease_sec() -> int:
    """How long a status lives without renewal (MM_LEASE_SEC, default 900 s),
    also the window of the agent's sign of life (life.window_sec)."""
    return life.window_sec()


def crown_beat_sec() -> int:
    """How often the holder renews its crown (ADR-007 step 1:
    MM_CROWN_BEAT_SEC, default 60 s, at least 15)."""
    s = os.environ.get("MM_CROWN_BEAT_SEC", "")
    return max(15, int(s)) if s.isdigit() else 60


def crown_lease_sec() -> int:
    """How long a crown lives without its beat (ADR-007 step 1:
    MM_CROWN_LEASE_SEC, default 150 s, never below two beats)."""
    s = os.environ.get("MM_CROWN_LEASE_SEC", "")
    return max(2 * crown_beat_sec(), int(s) if s.isdigit() else 150)


# --- local state ----------------------------------------------------------------

def _file(kind: str, user_id: str) -> Path | None:
    """presence-<uid>.env or coord-<uid>.env; None for an id that is none."""
    return session_file.state_dir() / f"{kind}-{user_id}.env" if _UID.fullmatch(user_id) else None


def _read(path: Path | None) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in path.read_text().splitlines() if path else ():
            k, sep, v = line.partition("=")
            if sep and k not in out:
                out[k] = v
    except OSError:
        pass
    return out


def _write(path: Path | None, text: str) -> None:
    if path is None:
        return
    try:
        session_file.write_private(path, text)
    except OSError:
        pass


def _coord(user_id: str) -> dict[str, int]:
    return {k: int(v) for k, v in _read(_file("coord", user_id)).items() if v.isdigit()}


def _save_coord(user_id: str, crown_until: int, pending: int, yield_until: int) -> None:
    _write(_file("coord", user_id),
           f"CROWN_UNTIL={crown_until}\nPENDING={pending}\nYIELD_UNTIL={yield_until}\n")


def crown_held(user_id: str) -> bool:
    return _coord(user_id).get("CROWN_UNTIL", 0) > time.time()


def crown_until(user_id: str) -> int:
    """Epoch until which this agent holds the crown; 0 without one."""
    return _coord(user_id).get("CROWN_UNTIL", 0)


def take_pending(user_id: str) -> bool:
    return _coord(user_id).get("PENDING", 0) == 1


def with_crown(message: str, user_id: str, *, here: bool = True) -> str:
    """Prefix the coordinator's posts: bot posts never show a custom status.
    here: the post goes where the crown shows (crown_here); False leaves it
    plain, as a guest's post (ADR-002)."""
    if here and user_id and crown_held(user_id):
        return a2a.with_crown(message)
    return message


# Channel types that are no team channel: a DM, a group message.
_PRIVATE = ("D", "G")
_TYPES: dict[str, str] = {}


async def _channel_type(client: ChatPort, channel_id: str) -> str:
    if channel_id not in _TYPES:
        _TYPES[channel_id] = (await client.get_channel(channel_id)).type
    return _TYPES[channel_id]


def not_reachable(row: Mapping[str, Any] | None) -> str:
    """Why an agent of _rows is not reachable: `absent` (no row), `lease` (its
    status lease ran out), `silent` (no sign of the agent, ADR-007 amendment
    step 1), `idle` (💤, until its human resumes), `offline`; '' when it is
    reachable (RT-6.14 step 10)."""
    if row is None:
        return "absent"
    if row["reachable"]:
        return ""
    if not row["live"]:
        return "lease"
    if row["emoji"] != a2a.status_emoji(StatusMark.IDLE):
        return "offline"
    return "silent" if str(row.get("text", "")).startswith(f"{SILENT} ") else "idle"


async def is_direct(client: ChatPort, channel_id: str, *, unsure: bool = False) -> bool:
    """A DM or a group message (no team channel); `unsure` when that cannot be told."""
    try:
        return await _channel_type(client, channel_id) in _PRIVATE
    except Exception as exc:  # noqa: BLE001
        log.debug("channel type: %s", exc)
        return unsure


async def crown_here(client: ChatPort, user_id: str, channel_id: str = "",
                     root_id: str = "") -> bool:
    """Whether a post of this agent in channel_id, or in root_id's thread,
    shows the 👑 (prefix and props.a2a.crown; RT-6.6 step 5, ADR-002): it
    holds the crown, and the post goes to its home (no channel: the home), a
    DM or a group message. Never in another team channel: there the agent is
    a guest, and that channel has its own crown. Unsure: no crown."""
    if not user_id or not crown_held(user_id):
        return False
    try:
        if not channel_id and root_id:
            channel_id = await client.post_channel_id(root_id)
        if not channel_id or channel_id == await client.work_channel_id():
            return True
        return await is_direct(client, channel_id)
    except Exception as exc:  # noqa: BLE001 — cosmetic: a plain post
        log.debug("crown here: %s", exc)
        return False


async def routes(client: ChatPort) -> set[str]:
    """The team channels whose plain posts the crown holder routes (RT-6.6
    step 5, ADR-002): its home only. In any other team channel, the lobby
    too when it is not its home, it is an agent like the rest: a plain post
    there wakes only that channel's own crown, if someone has it as home.
    Empty when the home cannot be resolved."""
    try:
        return {await client.work_channel_id()}
    except Exception as exc:  # noqa: BLE001
        log.debug("crown channels: %s", exc)
        return set()


async def coordinates_in(client: ChatPort, user_id: str, channel_id: str) -> bool:
    """This agent holds the crown and acts as coordinator for a post in
    channel_id (RT-6.6 step 5, RT-6.16 step 2): a channel it routes
    (`routes`), a DM or group message, or one it was not told (''). Unsure:
    as before, the crown counts."""
    if not user_id or not crown_held(user_id):
        return False
    if not channel_id or channel_id in await routes(client):
        return True
    return await is_direct(client, channel_id, unsure=True)


async def _user_id(client: ChatPort) -> str:
    if client.session:
        return client.session.user_id
    return os.environ.get("MM_BOT_USER_ID") or (await client.get_me()).id


# --- the status -------------------------------------------------------------------

def _rfc3339(epoch: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))


async def set_status(client: ChatPort, state: str, label: str = "") -> None:
    """Show what this agent is doing (RT-6.6 step 1): working, listening (label:
    until epoch), idle, bus (no turn hooks), offline, or renew (the state last
    reported again; without one, bus). A label names the project, never the
    task; without one, the directory the session works in. While the agent
    holds the crown, the emoji is 👑 and the text starts with "coordinator";
    the crown is extended only while the agent is reachable, so an idle
    coordinator loses it when its crown lease runs out. Idle in a session started as
    a channel is reachable: the bus wakes it. ValueError for an unknown
    state; anything Mattermost answers is ignored."""
    if not client.token:
        return
    async with _lock():
        await _set_status(client, state, label)


async def _set_status(client: ChatPort, state: str, label: str = "") -> None:
    """set_status under the lock."""
    uid = await _user_id(client)
    pres, coord = _file("presence", uid), _file("coord", uid)
    now, lease = int(time.time()), lease_sec()
    if state == "renew":
        saved = _read(pres)
        state, label = saved.get("STATE") or "bus", saved.get("LABEL", "")
        # A listen window that ran out without a Stop hook is over.
        if state == "listening" and not (label.isdigit() and int(label) > now):
            state, label = "idle", ""
    if state not in STATES:
        raise ValueError(f"state must be one of {', '.join(STATES)} or renew")
    if not label and state != "listening":
        label = Path(os.environ.get("CORRAL_WORKDIR") or os.getcwd()).name

    until, reachable, presence, named = now + lease, True, "online", ""
    if state == "listening":
        if not label.isdigit():
            raise ValueError("listening needs the window's end as epoch seconds")
        emoji = a2a.status_emoji(StatusMark.LISTENING)
        text = f"listening · until {time.strftime('%H:%M', time.localtime(int(label)))}, then idle"
        # The window may end without the Stop hook running, so the status ends with it.
        until = min(until, int(label))
    elif state == "working":
        emoji, text, named = a2a.status_emoji(StatusMark.WORKING), "working", label
    elif state == "idle" and channel.confirmed_file(uid).exists():
        # Started as a channel (RT-6.12): this server pushes what comes in, so
        # the agent stays reachable, and keeps a crown, between its turns.
        emoji, text = a2a.status_emoji(StatusMark.LISTENING), "listening · the bus wakes me"
    elif state == "idle":
        emoji, text, reachable = a2a.status_emoji(StatusMark.IDLE), "idle · until my human resumes", False
    elif state == "bus":
        emoji, text, named = a2a.status_emoji(StatusMark.BUS), "on the bus", label
    else:
        presence, emoji, text = "offline", "", ""
    # ADR-007 amendment step 1: an agent that would be reachable but shows no
    # sign is silent, shown with the 💤 every plugin reads as not reachable.
    since = _silent(uid, state)
    if since:
        emoji, named, reachable = a2a.status_emoji(StatusMark.IDLE), "", False
        text = f"{SILENT} · no sign since {time.strftime('%H:%MZ', time.gmtime(since))}"

    if state == "offline":
        for f in (pres, coord):
            if f is not None:
                f.unlink(missing_ok=True)
    else:
        _write(pres, f"STATE={state}\nLABEL={label}\n")

    crown = _coord(uid).get("CROWN_UNTIL", 0)
    if since and crown > now:
        # A silent holder drops the crown; the next agent claims at its slot. It
        # yields one crown lease, not a release's two leases: a silence may end
        # with the next tool call, and the slots come first anyway.
        _save_coord(uid, 0, 0, now + crown_lease_sec())
        log.info("crown dropped: no sign of the agent since %s (ADR-007 amendment step 1)",
                 _rfc3339(int(since)))
        crown = 0
    if state != "offline" and crown > now:
        if reachable:
            crown = now + crown_lease_sec()  # ADR-007 step 1: the beat renews it
        # The marker (ADR-007 amendment, decided 2026-11-12): this holder's server sets
        # the receipts, so the others may claim over a post without one (receipt.py).
        emoji, until = a2a.status_emoji(StatusMark.COORDINATOR), crown
        text = f"coordinator · {text} · {RECEIPT_MARK}"
    else:
        crown = 0
    if text:
        text = status_text(text, named, usage.segments(), client.work_channel_name)
        usage.shown()
    if coord is not None and coord.is_file():
        lines = _read_lines(coord)
        _write(coord, "".join(f"CROWN_UNTIL={crown}\n" if ln.startswith("CROWN_UNTIL=") else f"{ln}\n"
                              for ln in lines))

    try:
        await client.set_presence(uid, presence)
    except Exception as exc:  # noqa: BLE001 — fail open
        log.debug("status: %s", exc)
    try:
        if emoji:
            await client.set_custom_status(emoji, text[:STATUS_MAX], _rfc3339(until))
        else:
            await client.clear_custom_status()
        life.shown(bool(since))  # only once written: a failed flip is written again
    except Exception as exc:  # noqa: BLE001
        log.debug("custom status: %s", exc)


# Mattermost caps custom status text at 100 characters.
STATUS_MAX = 100
_HOME = " · ~"


def homed(text: str, home: str) -> str:
    """A status text that names the agent's home (RT-6.6 step 1, ADR-002):
    "<text> · ~<home>". A custom status is global per user; the home tells
    every channel's roster whose crown it is. Past STATUS_MAX the text
    before it is cut (the label first, it comes last), never the home."""
    if not home:
        return text
    tail = f"{_HOME}{home}"
    return text[:max(0, STATUS_MAX - len(tail))] + tail


def status_text(head: str, label: str, segments: list[str], home: str) -> str:
    """The custom status text (RT-6.6 step 1, ADR-008): `<head> · <label> ·
    <segments> · ~<home>`, e.g. `working · bus · ctx 62% · 5h 40% · ~home`.
    The home stays last and STATUS_MAX holds: when it does not fit, the label
    goes first, then the last segments (`7d`, `5h`, `ctx` in that order, each
    shown only when it exists), never the home. Without segments it is
    homed() of head and label, the label cut as before."""
    if not segments:
        return homed(f"{head} · {label}" if label else head, home)
    tail = f"{_HOME}{home}" if home else ""
    for keep, with_label in ((len(segments), True), (len(segments), False),
                             *((n, False) for n in range(len(segments) - 1, 0, -1))):
        text = " · ".join([head, *([label] if label and with_label else []), *segments[:keep]])
        if len(text) + len(tail) <= STATUS_MAX:
            return text + tail
    return homed(head, home)


def sets_receipts(text: str) -> bool:
    """A holder's status text carries the receipt marker (RT-6.6 step 10)."""
    return RECEIPT_MARK in (w.strip() for w in str(text).split(" · "))


def home_of(text: str) -> str | None:
    """The home a status text names; None for a text without one (an older
    plugin's, or set by hand), which counts in every channel."""
    _, sep, home = str(text).rpartition(_HOME)
    if not sep or not home.strip() or " · " in home:
        return None
    return home.strip()


def _read_lines(path: Path) -> list[str]:
    try:
        return path.read_text().splitlines()
    except OSError:
        return []


async def _render(client: ChatPort) -> None:
    """The own status again, after the crown changed; never raises. Under the
    lock (from _command)."""
    try:
        await _set_status(client, "renew")
    except Exception as exc:  # noqa: BLE001
        log.debug("status renew: %s", exc)


# --- the roster ---------------------------------------------------------------------

def _at(value: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return at.replace(tzinfo=timezone.utc) if at.tzinfo is None else at


def _expired(value: Any, now: datetime) -> bool:
    at = _at(value)
    return at is None or at <= now


def live(custom: Mapping[str, Any], now: datetime | None = None) -> bool:
    """Whether a custom status is a live lease (RT-6.1): an emoji, a duration
    and an expiry still ahead. One without an expiry predates the lease (or
    was set by hand) and proves nothing about a live session."""
    return (bool(custom.get("emoji")) and bool(custom.get("duration"))
            and not _expired(custom.get("expires_at"), now or datetime.now(timezone.utc)))


def live_home(user: ChannelUser) -> str | None:
    """RT-6.21: the home a live agent's status names ('' when it names none,
    an older plugin); None when its status is no live lease."""
    if not live(user.custom_status):
        return None
    return home_of(str(user.custom_status.get("text") or "")) or ""


async def _rows(client: ChatPort, channel_id: str) -> list[dict[str, Any]]:
    """The bus's bots in a channel (the registrar's prefix), oldest first, with
    what the lease rules need: id, username, create_at, presence, emoji, text,
    home (the one its status names, None: none), live, until_ms (the lease's
    end, 0 unknown), reachable, crown. A custom
    status counts (live) while its lease has not expired;
    one without an expiry predates the lease (or was set by hand) and proves
    nothing about a live session. Reachable: not offline, a live status, not
    💤. Crown: reachable with a live 👑."""
    prefix = session_file.agent_prefix()
    users = sorted((u for u in await client.channel_users(channel_id)
                    if u.username.startswith(prefix) and not u.deleted),
                   key=lambda u: u.create_at)
    if not users:
        return []
    status = await client.presences([u.id for u in users])
    now = datetime.now(timezone.utc)
    rows = []
    for u in users:
        custom = u.custom_status
        emoji = str(custom.get("emoji") or "")
        on = live(custom, now)
        presence = status.get(u.id) or "offline"
        reachable = presence != "offline" and on and emoji != a2a.status_emoji(StatusMark.IDLE)
        until = _at(custom.get("expires_at")) if custom.get("duration") else None
        text = str(custom.get("text") or "")
        rows.append({"id": u.id, "username": u.username,
                     "create_at": u.create_at, "presence": presence,
                     "emoji": emoji, "text": text, "home": home_of(text), "live": on,
                     "until_ms": int(until.timestamp() * 1000) if until else 0,
                     "reachable": reachable, "crown": reachable and emoji == a2a.status_emoji(StatusMark.COORDINATOR)})
    return rows


def _at_home(rows: list[dict[str, Any]], home: str) -> list[dict[str, Any]]:
    """The rows that count in the roster and the election of the channel
    `home` (RT-6.6, ADR-002): the agents whose status names it, and those
    whose status names no home (an older plugin), as before."""
    return [r for r in rows if r["home"] in (None, home)]


def _coordinator_first(rows: list[dict[str, Any]], standing: list[str] | None = None
                       ) -> list[dict[str, Any]]:
    """The reachable rows, the coordinator first and marked: the agent with a
    live 👑, else the oldest reachable one that stands for the crown
    (`standing`, _election: only a peer is handed it; [] none, None every
    reachable one), which the lease hands it to next."""
    reachable = [r["id"] for r in rows if r["reachable"]]
    coord = next((r["id"] for r in rows if r["crown"]), "") or next(
        (i for i in reachable if standing is None or i in standing), "")
    ordered = [r for r in rows if r["id"] == coord] + [r for r in rows if r["id"] != coord]
    return [dict(r, coordinator=r["id"] == coord) for r in ordered if r["reachable"]]


async def roster(client: ChatPort) -> list[RosterEntry]:
    """The reachable agents of the home, coordinator first (RT-6.6, RT-6.14
    step 4); [] when it cannot be read. A member whose home is elsewhere does
    not count; one that holds its own home's crown comes last as that home's
    ambassador (crown_of, ADR-002): whom to mention for that team's work."""
    if not client.token:
        return []
    try:
        return await roster_in(client, await client.work_channel_id(), client.work_channel_name)
    except Exception as exc:  # noqa: BLE001 — an empty roster
        log.debug("roster: %s", exc)
        return []


async def roster_in(client: ChatPort, channel_id: str, name: str) -> list[RosterEntry]:
    """roster() for the channel `name` (id channel_id) as somebody's home: its
    reachable agents, coordinator first, then the ambassadors (RT-6.14 step
    9). Raises when the channel cannot be read."""
    rows = await _rows(client, channel_id)
    guests = [entry(dict(r, crown=False), crown_of=str(r["home"]))
              for r in rows if r["home"] not in (None, name) and r["crown"]]
    here = _at_home(rows, name)
    standing = None if any(r["crown"] for r in here) else (await _election(client, here)).ids
    return [entry(r) for r in _coordinator_first(here, standing)] + guests


def crown_in(rows: list[RosterEntry]) -> str:
    """The crown holder in roster_in's rows, as coordinator_of reads it: the
    live 👑 of the home ('' none); an ambassador's is its own home's."""
    return next((r.username for r in rows if r.crown and not r.crown_of), "")


async def coordinator_of(client: ChatPort, channel_id: str) -> str:
    """The crown holder of channel_id as somebody's home (RT-6.22): the agent
    with a live 👑 among those whose status names that channel (or no home,
    an older plugin); '' for none. A guest's 👑 of its own home is none."""
    name = (await client.get_channel(channel_id)).name
    rows = _at_home(await _rows(client, channel_id), name)
    return next((str(r["username"]) for r in rows if r["crown"]), "")


def entry(row: Mapping[str, Any], crown_of: str = "") -> RosterEntry:
    """One roster row as a bus type: the status word from the status text
    (after "coordinator"), else from its emoji. crown_of: the other home
    whose crown it holds."""
    text = [w.strip() for w in str(row.get("text", "")).split(" · ")]
    if text and text[0] == "coordinator":
        text = text[1:]
    status = text[0] if text and text[0] else _state_word(str(row.get("emoji", "")))
    return RosterEntry(username=str(row.get("username") or ""), status=status,
                       coordinator=bool(row.get("coordinator")), crown=bool(row.get("crown")),
                       crown_of=crown_of, user_id=str(row.get("id") or ""),
                       usage=usage.from_status(str(row.get("text", ""))))


# --- the crown --------------------------------------------------------------------------

async def command(client: ChatPort, action: str) -> str:
    """The coordinator crown (RT-6.6): who (the coordinator's username), take
    (now, or as soon as the holder releases or its lease runs out), release
    (after a HANDOVER is ACKed), tick (the heartbeat: renew the own status,
    then claim or yield). '' when the bus cannot be read."""
    if action not in ACTIONS:
        raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
    if not client.token:
        return ""
    passed: list[bool] = []
    try:
        async with _lock():
            out = await _command(client, action, passed)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — fail open, like the status
        log.debug("lease %s: %s", action, exc)
        return ""
    if passed:
        await _crown_came()
    return out


# Called when the crown passes to this agent (RT-6.12 step 3): the server
# re-checks the posts its pusher holds for the digest.
_on_crown: list[Callable[[], Awaitable[None]]] = []


def on_crown(fn: Callable[[], Awaitable[None]]) -> None:
    """Call fn each time the crown passes to this agent (it held none
    before: a sleep past the lease, a claim, a take), after the lease lock
    is released."""
    if fn not in _on_crown:
        _on_crown.append(fn)


async def _crown_came() -> None:
    for fn in list(_on_crown):
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — the digest still carries them
            log.info("held posts not re-checked for the crown: %s", exc)


@dataclass(frozen=True)
class Election:
    """What this server's view of the home's election is (RT-6.6 step 2).
    ids: the reachable agents that stand, oldest first ([]: nobody may be
    crowned on its own: circles foreign to each other, this bot unbound beside
    an agent with an owner, a description not readable); unbound: this bot has
    no owner while an agent with an owner is reachable; owned: the reachable
    agents that have one."""

    ids: list[str]
    unbound: bool = False
    owned: frozenset[str] = frozenset()


async def _election(client: ChatPort, rows: list[dict[str, Any]]) -> Election:
    """RT-6.6 step 2 (ADR-007 amendment step 4, the crown only for peers):
    who stands for the crown in the home of `rows`, as this server sees it.
    Without trusted humans no peer can be told (the owner split is off) and
    every reachable agent stands, as before. Else the reachable agents with an
    owner (owners.read) stand if this bot is bound and a peer of each of them;
    the ones without an owner are foreign to them and stand only where no agent
    has one. A description that cannot be read blocks the election (nothing is
    assumed): `owners.read` is None for it, '' for no owner. This bot's own
    description too: with trusted humans only their absence makes parity."""
    oldest = [r["id"] for r in rows if r["reachable"]]
    try:
        names = await owners.trusted_names(client)
        me = (await client.get_me()).id if names else ""
    except Exception as exc:  # noqa: BLE001 — the owner split is off, as in owners.mine
        log.info("crown: trusted humans or own account not read, every agent stands: %s", exc)
        return Election(oldest)
    if not names:
        return Election(oldest)
    ids = oldest if me in oldest else [*oldest, me]
    agents = {i: await owners.read(client, i) for i in ids}  # never raises: None is unreadable
    if any(a is None for a in agents.values()):
        log.info("crown: a description is not readable, nobody stands")
        return Election([])
    owned = {i: a for i, a in agents.items() if a is not None and a.owner and (i in oldest or i == me)}
    held = frozenset(i for i in owned if i in oldest)
    if not held:
        return Election(oldest)  # no owners: every agent is a peer
    if me not in owned:
        return Election([], unbound=True, owned=held)
    if any(not owners.is_peer(a, names) for i, a in owned.items() if i != me and i in oldest):
        log.info("crown: the home has an agent of a foreign owner circle: nobody stands")
        return Election([], owned=held)
    return Election([i for i in oldest if i in owned], owned=held)


async def _elected(client: ChatPort, uid: str, rows: list[dict[str, Any]]) -> bool:
    """This agent is the oldest reachable one that stands (_election): it may claim."""
    ids = (await _election(client, rows)).ids
    return bool(ids) and ids[0] == uid


async def _command(client: ChatPort, action: str, passed: list[bool] | None = None) -> str:
    uid = await _user_id(client)
    if not _UID.fullmatch(uid):
        return ""
    now, lease = int(time.time()), lease_sec()
    state = _coord(uid)
    crown, pending, yield_until = (state.get(k, 0) for k in ("CROWN_UNTIL", "PENDING", "YIELD_UNTIL"))
    cid = await client.work_channel_id()
    if action == "tick":
        await _render(client)
        # Re-read: the renewal may have let an idle crown run out.
        crown = _coord(uid).get("CROWN_UNTIL", crown)

    # A crown per home (ADR-002): only the agents whose home this is count.
    rows = _at_home(await _rows(client, cid), client.work_channel_name)
    others = [r for r in rows if r["crown"] and r["id"] != uid]  # oldest first
    global _took_from, _crown_since, _ousted
    if crown <= now:
        _took_from, _crown_since, _ousted = "", 0.0, None
    elif _took_from:
        # ADR-007 step 4: the holder this agent took the crown over from does
        # not get it back by being older; it yields when it sees crown_taken.
        others = [r for r in others if r["id"] != _took_from]

    async def take_now() -> None:
        global _crown_since
        _save_coord(uid, now + crown_lease_sec(), 0, 0)
        _crown_since = now
        await _render(client)
        if crown <= now:
            # The crown passed to this agent: is the header's bus part there (step 7)?
            if passed is not None:
                passed.append(True)
            await _header(client, cid)

    if action == "who":
        first = [r for r in rows if r["crown"]] + [r for r in rows if r["reachable"]]
        return first[0]["username"] if first else ""
    if action == "take":
        if not others:
            await take_now()
            return "taken"
        _save_coord(uid, crown, 1, yield_until)
        return (f"pending: @{others[0]['username']} still holds the crown; "
                "it passes to you once released or expired")
    if action == "release":
        _save_coord(uid, 0, 0, now + 2 * lease)
        await _render(client)
        return "released"

    holding = crown > now
    if holding and (by := await _taken_from_me(client, cid, uid, rows)):
        # ADR-007 step 4: an agent took the crown over from this one (no 👀 in
        # time): it yields, and does not claim for two leases, as after a release.
        _save_coord(uid, 0, 0, now + 2 * lease)
        _took_from, _crown_since, _ousted = "", 0.0, None
        await _render(client)
        log.info("crown yielded: %s took it over (crown_taken, ADR-007 step 4)", by)
    elif holding and others:
        # Two crowns in one home (a race): the older agent keeps it, among
        # peers; a holder that does not stand yields to one that does (an
        # unbound one to a bound one, ADR-007 amendment step 4).
        mine = next((r["create_at"] for r in rows if r["id"] == uid), None)
        el = await _election(client, rows)
        other = others[0]["id"]
        if el.unbound and other in el.owned:
            yields = True
        elif el.ids and uid in el.ids and other not in el.ids:
            yields = False
        elif el.ids and other in el.ids and uid not in el.ids:
            yields = True
        else:
            yields = mine is not None and others[0]["create_at"] < mine
        if yields:
            _save_coord(uid, 0, pending, yield_until)
            await _render(client)
    elif not holding and not others and yield_until <= now:
        me_reachable = any(r["id"] == uid and r["reachable"] for r in rows) and not _silent(uid)
        # An explicit take (pending) is allowed; the election only among peers.
        if me_reachable and (pending == 1 or await _elected(client, uid, rows)):
            await take_now()
    if cid not in _header_seen and _coord(uid).get("CROWN_UNTIL", 0) > now:
        # Once per process while holding: an upgrade finds an older header (step 7).
        await _header(client, cid)
    _plan(uid, rows)
    return ""


# --- the slot (ADR-007 step 2) -----------------------------------------------------------

# When this server looks again for a free crown (epoch s; 0: no look planned);
# the holder it last saw, and the end of that holder's status: the one time
# every server of the home shares, so the slots keep their order.
_look_at = 0.0
_vacant_at = 0.0
_holder_id = ""
# The holder's name and whether its status shows the receipt marker (receipt.py).
_holder_name = ""
_holder_marked = False
# ADR-007 step 4: the holder this agent took the crown over from (no yield to it),
# since when this agent holds its crown (epoch s; 0 unknown), and the agent a
# crown_taken naming this one came from, with when (until the next tick judges it).
_took_from = ""
_crown_since = 0.0
_ousted: tuple[str, float] | None = None


def holder() -> tuple[str, str, bool]:
    """The home's crown holder this server last saw (id, username, whether it
    sets receipts); ('', '', False) while it saw none or holds the crown itself."""
    return (_holder_id, _holder_name, _holder_marked) if _holder_name else ("", "", False)


def ousted(by: str, at: float) -> None:
    """A crown_taken post naming this agent came from `by` (ADR-007 step 4): the
    next tick yields if `by` wears a live crown in the home."""
    global _ousted
    _ousted = (by, at)


def ousted_pending() -> bool:
    return _ousted is not None


async def _taken_from_me(client: ChatPort, cid: str, uid: str, rows: list[dict[str, Any]]) -> str:
    """The agent that took this holder's crown over (ADR-007 step 4): one with a
    live crown in the home whose crown_taken names this agent; '' for none.
    Heard on the WebSocket (ousted), or, while a second crown shows, read from
    the home's posts since this agent holds the crown: a holder whose WebSocket
    is dead hears nothing. Only a crowned agent's post counts; a heard one is
    judged once (its author wears no crown: nothing, and no tick at once again)."""
    global _ousted
    heard, _ousted = (_ousted[0] if _ousted else ""), None
    crowned = {str(r["id"]) for r in rows if r["crown"] and r["id"] != uid}
    if not crowned:
        return ""
    if heard in crowned:
        return heard
    since = _crown_since or time.time() - lease_sec()
    try:
        posts = await client.channel_posts(cid, since_ms=int(since * 1000))
    except Exception as exc:  # noqa: BLE001 — unread: the rules of two crowns, as before
        log.info("crown_taken not read: %s", exc)
        return ""
    return next((p.user_id for p in posts if p.kind == a2a.CROWN_TAKEN and p.user_id in crowned
                 and a2a_holder(p.props) == uid), "")


def a2a_holder(props: Mapping[str, Any]) -> str:
    """props.a2a.holder of a crown_taken post: the holder it was taken from."""
    note = props.get("a2a") if isinstance(props, Mapping) else None
    return str(note.get("holder") or "") if isinstance(note, Mapping) else ""


def look_due() -> float:
    """When the next look (ADR-007 step 2) is due, epoch seconds; 0 none."""
    return _look_at


def _plan(uid: str, rows: list[dict[str, Any]]) -> None:
    """ADR-007 step 2: when this agent looks again for a free crown: the end
    of the holder's status plus this agent's slot (SLOT_SEC times its place
    among the reachable agents, the holder left out, oldest first), not before
    a release's YIELD_UNTIL. Whether it stands (_election, descriptions read)
    is asked only at the claim (look), so a plan costs no read. Never while it
    holds the crown or is not reachable, nor for a crown without an end (its
    lease rules, as before). With no holder seen yet only the oldest plans, a
    slot from now; the others wait for a holder."""
    global _look_at, _vacant_at, _holder_id, _holder_name, _holder_marked
    now = time.time()
    state = _coord(uid)
    holder = next((r for r in rows if r["crown"] and r["id"] != uid), None)
    if holder is not None:
        _vacant_at, _holder_id = holder["until_ms"] / 1000, str(holder["id"])
        _holder_name, _holder_marked = str(holder["username"]), sets_receipts(holder["text"])
    elif not any(r["id"] == _holder_id and r["crown"] for r in rows):
        _holder_name, _holder_marked = "", False  # no live crown: nothing to watch
    _look_at = 0.0
    ids = [str(r["id"]) for r in rows if r["reachable"] and (holder is None or r["id"] != holder["id"])]
    if (state.get("CROWN_UNTIL", 0) > now or uid not in ids
            or (holder is not None and not _vacant_at)):
        return
    slot = SLOT_SEC * (ids.index(uid) + 1)
    if _vacant_at:
        _look_at = max(_vacant_at + slot, float(state.get("YIELD_UNTIL", 0)))
    elif ids[0] == uid:
        _look_at = max(now + slot, float(state.get("YIELD_UNTIL", 0)))


async def look(client: ChatPort) -> bool:
    """ADR-007 step 2: at this agent's slot, read the roster; the crown still
    free because its holder is gone (its status ended, idle or offline),
    claim it by the rules of RT-6.6 step 2 save the oldest-first one: the
    agents before this one had their slots. A holder still reachable without
    the 👑 released or yielded it: that is the tick's election and a pending
    take's (RT-6.6 step 4), not a slot's. Plans the next look; a look that
    could not claim plans none until the next tick. True when it claimed (the
    next tick re-checks for a second crown soon). Before its time it reads
    nothing. Never raises."""
    global _look_at, _crown_since
    if not client.token or not 0 < _look_at <= time.time():
        return False
    claimed: list[bool] = []
    silent = None
    try:
        async with _lock():
            uid = await _user_id(client)
            if not _UID.fullmatch(uid):
                return False
            now = int(time.time())
            state = _coord(uid)
            cid = await client.work_channel_id()
            rows = _at_home(await _rows(client, cid), client.work_channel_name)
            free = not any(r["crown"] for r in rows)
            gone = not any(r["id"] == _holder_id and r["reachable"] for r in rows)
            mine = any(r["id"] == uid and r["reachable"] for r in rows) and not _silent(uid)
            if (free and gone and mine and state.get("CROWN_UNTIL", 0) <= now
                    and state.get("YIELD_UNTIL", 0) <= now and uid in (await _election(client, rows)).ids):
                _save_coord(uid, now + crown_lease_sec(), 0, 0)
                await _render(client)
                claimed.append(_coord(uid).get("CROWN_UNTIL", 0) > now)
                log.info("crown claimed at this agent's slot (ADR-007 step 2)")
                if any(claimed):
                    _crown_since = now
                    old = next((r for r in rows if r["id"] == _holder_id), None)
                    silent = (old, m) if old and (m := _SILENT_SINCE.match(str(old["text"]))) else None
                await _header(client, cid)
            _plan(uid, rows)
            if free and not any(claimed):
                _look_at = 0.0  # nothing to claim here now: the tick plans again
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — fail open, like the tick
        log.debug("lease look: %s", exc)
        return False
    if silent is not None:
        # The amendment's step 1: the dropped crown was a silent holder's; the
        # line names the silence (RT-6.6 step 10).
        old, m = silent
        await _crown_taken(client, cid, uid, str(old["id"]), f"`{old['username']}` silent since {m[1]}")
    if any(claimed):
        await _crown_came()
    return any(claimed)


# --- the claim over a missing receipt (ADR-007 steps 3 and 4) ---------------------------

async def slot_over(client: ChatPort, holder_id: str) -> int:
    """ADR-007 step 3: this agent's place (1 the oldest) among the reachable
    agents of the home that stand, the holder left out, for a claim over the
    holder `holder_id`; 0 when it has none or the holder wears no crown that
    sets receipts any more. Raises when the roster cannot be read."""
    uid = await _user_id(client)
    cid = await client.work_channel_id()
    rows = _at_home(await _rows(client, cid), client.work_channel_name)
    held = next((r for r in rows if r["id"] == holder_id), None)
    if held is None or not held["crown"] or not sets_receipts(held["text"]) or _silent(uid):
        return 0
    ids = (await _election(client, [r for r in rows if r["id"] != holder_id])).ids
    return ids.index(uid) + 1 if uid in ids else 0


async def take_over(client: ChatPort, holder_id: str, post_id: str) -> bool:
    """ADR-007 steps 3 and 4: no receipt of the holder for `post_id` in time, so
    this agent claims as if the holder's status had ended: when the holder
    still wears its crown with the marker, no other crown is live in the home
    (an earlier slot took it), this agent is reachable, not silent, not
    yielding, and stands. Then the crown is this agent's, it does not yield to
    that holder (_took_from) and posts crown_taken. A race of two such claims
    ends at the next tick: the younger yields. Never raises."""
    global _took_from, _crown_since
    try:
        async with _lock():
            uid = await _user_id(client)
            if not _UID.fullmatch(uid):
                return False
            now = int(time.time())
            state = _coord(uid)
            cid = await client.work_channel_id()
            rows = _at_home(await _rows(client, cid), client.work_channel_name)
            held = next((r for r in rows if r["id"] == holder_id), None)
            if (held is None or not held["crown"] or not sets_receipts(held["text"])
                    or any(r["crown"] for r in rows if r["id"] != holder_id)
                    or not any(r["id"] == uid and r["reachable"] for r in rows) or _silent(uid)
                    or state.get("CROWN_UNTIL", 0) > now or state.get("YIELD_UNTIL", 0) > now
                    or uid not in (await _election(client, [r for r in rows if r["id"] != holder_id])).ids):
                return False
            _save_coord(uid, now + crown_lease_sec(), 0, 0)
            await _render(client)
            if _coord(uid).get("CROWN_UNTIL", 0) <= now:
                return False
            _took_from, _crown_since = holder_id, now
            log.info("crown taken over from %s: no receipt for %s (ADR-007 step 3)", held["username"], post_id)
            await _header(client, cid)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — fail open, like the tick
        log.info("crown not taken over: %s", exc)
        return False
    seen = a2a.symbol(Mark.SEEN)
    await _crown_taken(client, cid, uid, holder_id,
                       f"no {seen} for `{post_id}` within {life.ack_wait_sec()} s")
    await _crown_came()
    return True


async def _crown_taken(client: ChatPort, cid: str, uid: str, holder_id: str, why: str) -> None:
    """The one line in the home after a takeover (ADR-007 step 4, RT-6.6 step 10):
    `👑 taken over by <bot>: <why>`, kind crown_taken, intent fyi (it wakes
    nobody, no digest: a2a.QUIET), props.a2a.holder the holder it came from,
    which yields when it reads it. Never raises."""
    try:
        me = (await client.get_me()).username
        message = with_crown(f"taken over by `{me}`: {why}", uid)
        props = a2a.props(kind=a2a.CROWN_TAKEN) or {}
        props["a2a"] = {**props.get("a2a", {}), "holder": holder_id}
        stats.record("post", chars=len(message))
        await client.post(message, channel_id=cid, props=props)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — the crown is taken all the same
        log.info("crown_taken not posted: %s", exc)


async def receipt_holder(client: ChatPort, username: str) -> tuple[str, str]:
    """RT-6.25 step 6 (ADR-007 step 5): the home's agent `username` as a question's
    👑: (its id, 'marked') when it wears a crown that sets receipts, (its id,
    'silent') when it turned silent, ('', '') else (an older plugin, gone: judged
    as before). Raises when the roster cannot be read."""
    cid = await client.work_channel_id()
    rows = _at_home(await _rows(client, cid), client.work_channel_name)
    row = next((r for r in rows if str(r["username"]).lower() == username.lower()), None)
    if row is None:
        return "", ""
    if row["crown"] and sets_receipts(row["text"]):
        return str(row["id"]), "marked"
    if _SILENT_SINCE.match(str(row["text"])):
        return str(row["id"]), "silent"
    return "", ""


# --- the work channel's header (RT-6.6 step 7) ---------------------------------------

HEADER_MAX = 1024  # Mattermost's limit, in characters
HEADER_MARK = f"{a2a.symbol(Mark.HEADER)} agent bus"
HEADER_RULE = (f"plain post → {a2a.symbol(Mark.CROWN)} routes it · @agent → that agent · "
               f"{a2a.symbol(Mark.ASK)} = a question for you")
# The bus part: how to address the agents. It names no holder, so a crown
# change leaves it alone (the 👑 shows on posts and in the custom status).
BUS_HEADER = f"{HEADER_MARK} · {HEADER_RULE}"
# The bus part as found: the mark's line, and a rule's line after it (before
# 0.65.4 the mark's line named the holder and the time, the rule followed).
_BUS_PART = re.compile(rf"\n*{re.escape(HEADER_MARK)}[^\n]*(?:\n{re.escape(HEADER_RULE[:12])}[^\n]*)?")
_header_logged = False
_header_seen: set[str] = set()  # homes whose header this process has checked


def _cut(text: str, room: int) -> str:
    if len(text) <= room:
        return text
    return text[:room - 1].rstrip() + "…" if room > 1 else ""


def with_bus_header(header: str, bus: str) -> str:
    """`header` with its bus part replaced by `bus`, or `bus` appended. What
    people wrote stays; past HEADER_MAX it is cut, never the bus part."""
    m = _BUS_PART.search(header)
    before, after = (header[:m.start()], header[m.end():]) if m else (header, "")
    before = before.rstrip()
    before = _cut(before, HEADER_MAX - len(bus) - len(after) - 2)
    sep = "\n\n" if before else ""
    after = _cut(after, HEADER_MAX - len(before) - len(sep) - len(bus))
    return (before + sep + bus + after)[:HEADER_MAX]


async def _header(client: ChatPort, cid: str) -> None:
    """Put the static bus part into the work channel's header (RT-6.6 step 7)
    when it is missing or differs. Mattermost posts a system message for every
    change, so a header that says it already gets no write. The crown holder
    checks when the crown passes to it and once per process. Fails open:
    without permission it logs once, the crown works."""
    global _header_logged
    _header_seen.add(cid)
    try:
        current = await client.channel_header(cid)
        new = with_bus_header(current, BUS_HEADER)
        if new != current:
            await client.set_channel_header(cid, new)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — cosmetic, like the status
        if not _header_logged:
            _header_logged = True
            log.warning("channel header not set (the crown works without it): %s", exc)
        else:
            log.debug("channel header: %s", exc)


async def usage_crossed(client: ChatPort) -> None:
    """A report crossed a band (ADR-008 step 2): the status text now, once;
    usage.take_band keeps it rare. A normal beat never comes here
    (the heartbeat's own renewal carries the numbers). Never raises."""
    try:
        if not client.token or not usage.take_band():
            return
        uid = await _user_id(client)
        if not _read(_file("presence", uid)).get("STATE"):
            return  # no status to renew (offline, or none yet)
        await set_status(client, "renew")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.debug("usage band: %s", exc)


def _judged(uid: str, state: str = "") -> tuple[bool, bool]:
    """For ADR-007 amendment step 1, from `state` or the one presence-<uid>.env
    holds (a listen window that ran out counts as idle, as at a renewal):
    whether the state would be reachable (not offline, not 💤 idle), and
    whether it is idle in a session started as a channel (RT-6.12), which
    has nothing to prove while nothing waits untaken."""
    if not state:
        saved = _read(_file("presence", uid))
        state, label = saved.get("STATE") or "bus", saved.get("LABEL", "")
        if state == "listening" and not (label.isdigit() and int(label) > time.time()):
            state = "idle"
    listening = state == "idle" and channel.confirmed_file(uid).exists()
    return state != "offline" and (state != "idle" or listening), listening


def _silent(uid: str, state: str = "") -> float:
    """Since when the agent, reachable by its state, shows no sign (epoch s,
    ADR-007 amendment step 1); 0 when it does, or is not reachable anyway."""
    counts, listening = _judged(uid, state)
    return life.silent_since(listening=listening) if counts else 0.0


async def ctx_line(client: ChatPort) -> None:
    """The context reached usage.CTX_LINE_AT (ADR-008 step 4, RT-6.6 step 9): one
    fyi line in the agent's HELLO thread, once per context window, no model turn.
    Not before the agent announced itself (no HELLO thread: the line waits).
    Never raises. A transport error or a 5xx is tried again with the next report;
    a 4xx (the HELLO deleted, the channel archived) comes back the same: no line
    in this context window, logged once."""
    try:
        if not client.token:
            return
        uid = await _user_id(client)
        rec = ownpost.read(uid)
        hello, channel_id = rec.get("HELLO"), rec.get("CHANNEL")
        if not (hello and channel_id):
            return
        ctx = usage.take_ctx_line()
        if ctx is None:
            return
        message = with_crown(f"ctx {round(ctx)}% · compaction soon", uid)
        stats.record("post", chars=len(message))
        await client.post(message, channel_id=channel_id, root_id=hello,
                          props=a2a.props(kind=a2a.USAGE))
    except asyncio.CancelledError:
        raise
    except Refused as exc:
        if not 400 <= exc.status < 500:
            usage.ctx_line_failed()
        log.info("ctx line not posted: %s", exc)
    except Exception as exc:  # noqa: BLE001
        usage.ctx_line_failed()
        log.info("ctx line not posted: %s", exc)


async def heartbeat(get_client: Callable[[], Awaitable[ChatPort | None]],
                    watch: Callable[[ChatPort], Awaitable[float | None]] | None = None) -> None:
    """Renew the lease until cancelled. Never raises: presence is cosmetic.
    `watch` runs after each tick (the stall check and the ask reminders,
    RT-6.19, stall.watch) and may bring the next tick forward to when it is
    due, PENDING_POLL_SEC apart at the least. Between ticks (ADR-007 phase 1)
    the crown's holder renews its status every crown_beat_sec(), and the
    others look for a free crown at their slot (look); a claim brings the next
    tick forward, which yields to an older crown of a race. It wakes at least
    every beat, so a crown taken between ticks (coordinator("take"), a tick
    at a reconnect) is beaten in time and a look planned there is not late; a
    wake with nothing due reads one file. A crown that lapsed gets its plain
    status at once, and so does an agent that turns silent or shows a sign
    again (ADR-007 amendment step 1): a silent holder drops its crown with
    that write. It wakes when the agent would turn silent. A member that
    watches a post for the holder's receipt (receipt.py, ADR-007 step 3) looks
    when its wait is over, and a holder that heard a crown_taken naming it
    ticks at once (step 4)."""
    from . import receipt  # receipt.py claims through this module

    next_tick, held, seeded = 0.0, False, False
    while True:
        delay = heartbeat_sec()
        try:
            client = await get_client()
            if client is not None and client.token:
                uid = await _user_id(client)
                if not seeded:
                    seeded = True
                    if _read(_file("presence", uid)).get("STATE") == "working":
                        # A server that restarts during a turn (a reconnect,
                        # /reload-plugins) keeps it: the Stop hook ends it.
                        life.turn(True)
                if time.time() >= next_tick or (_ousted is not None and crown_held(uid)):
                    await command(client, "tick")
                    if take_pending(uid):
                        delay = PENDING_POLL_SEC
                    if watch is not None:
                        due = await watch(client)
                        if due is not None:
                            delay = min(delay, max(PENDING_POLL_SEC, due))
                    next_tick = time.time() + delay
                elif crown_held(uid):
                    await set_status(client, "renew")  # the beat: no roster read, no watch
                elif held:
                    await set_status(client, "renew")  # the crown lapsed: the plain lease now
                elif bool(_silent(uid)) != life.is_shown():
                    await set_status(client, "renew")  # silent, or a sign again
                elif await receipt.check(client) or await look(client):
                    next_tick = min(next_tick, time.time() + PENDING_POLL_SEC)
                held = crown_held(uid)
                now = time.time()
                delay = next_tick - now
                if _look_at and not held:
                    delay = min(delay, max(1.0, _look_at - now))
                if (watched := receipt.due()) and not held:
                    delay = min(delay, max(1.0, watched - now))
                counts, listening = _judged(uid)
                if counts and (silent_at := life.due(listening=listening)):
                    delay = min(delay, max(1.0, silent_at - now))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("lease heartbeat skipped: %s", exc)
        await asyncio.sleep(max(0.0, min(delay, crown_beat_sec())))

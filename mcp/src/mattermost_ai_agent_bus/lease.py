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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import channel, session_file
from .model import RosterEntry

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

CROWN = "👑"
PENDING_POLL_SEC = 30.0
STATES = ("working", "listening", "idle", "bus", "offline")
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


# The lease's status emoji as a word, for a status without text.
_STATUS = {"hammer_and_wrench": "working", "ear": "listening", "zzz": "idle",
           "satellite_antenna": "bus"}


def heartbeat_sec() -> float:
    try:
        return max(30.0, float(os.environ.get("MM_HEARTBEAT_SEC", "300")))
    except ValueError:
        return 300.0


def lease_sec() -> int:
    """How long a status lives without renewal (MM_LEASE_SEC, default 900 s)."""
    s = os.environ.get("MM_LEASE_SEC", "")
    return int(s) if s.isdigit() and int(s) > 0 else 900


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
    if here and user_id and crown_held(user_id) and not message.startswith(CROWN):
        return f"{CROWN} {message}"
    return message


# Channel types that are no team channel: a DM, a group message.
_PRIVATE = ("D", "G")
_TYPES: dict[str, str] = {}


async def _channel_type(client: ChatPort, channel_id: str) -> str:
    if channel_id not in _TYPES:
        _TYPES[channel_id] = (await client.get_channel(channel_id)).type
    return _TYPES[channel_id]


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
        return await _channel_type(client, channel_id) in _PRIVATE
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
    try:
        return await _channel_type(client, channel_id) in _PRIVATE
    except Exception as exc:  # noqa: BLE001
        log.debug("coordinates in: %s", exc)
        return True


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
    coordinator loses it when its lease runs out. Idle in a session started as
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

    until, reachable, presence = now + lease, True, "online"
    if state == "listening":
        if not label.isdigit():
            raise ValueError("listening needs the window's end as epoch seconds")
        emoji = "ear"
        text = f"listening · until {time.strftime('%H:%M', time.localtime(int(label)))}, then idle"
        # The window may end without the Stop hook running, so the status ends with it.
        until = min(until, int(label))
    elif state == "working":
        emoji, text = "hammer_and_wrench", f"working · {label}"
    elif state == "idle" and channel.confirmed_file(uid).exists():
        # Started as a channel (RT-6.12): this server pushes what comes in, so
        # the agent stays reachable, and keeps a crown, between its turns.
        emoji, text = "ear", "listening · the bus wakes me"
    elif state == "idle":
        emoji, text, reachable = "zzz", "idle · until my human resumes", False
    elif state == "bus":
        emoji, text = "satellite_antenna", f"on the bus · {label}"
    else:
        presence, emoji, text = "offline", "", ""

    if state == "offline":
        for f in (pres, coord):
            if f is not None:
                f.unlink(missing_ok=True)
    else:
        _write(pres, f"STATE={state}\nLABEL={label}\n")

    crown = _coord(uid).get("CROWN_UNTIL", 0)
    if state != "offline" and crown > now:
        if reachable:
            crown = now + lease
        emoji, text, until = "crown", f"coordinator · {text}", crown
    else:
        crown = 0
    if text:
        text = homed(text, client.work_channel_name)
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
        live = bool(emoji) and bool(custom.get("duration")) and not _expired(custom.get("expires_at"), now)
        presence = status.get(u.id) or "offline"
        reachable = presence != "offline" and live and emoji != "zzz"
        until = _at(custom.get("expires_at")) if custom.get("duration") else None
        text = str(custom.get("text") or "")
        rows.append({"id": u.id, "username": u.username,
                     "create_at": u.create_at, "presence": presence,
                     "emoji": emoji, "text": text, "home": home_of(text), "live": live,
                     "until_ms": int(until.timestamp() * 1000) if until else 0,
                     "reachable": reachable, "crown": reachable and emoji == "crown"})
    return rows


def _at_home(rows: list[dict[str, Any]], home: str) -> list[dict[str, Any]]:
    """The rows that count in the roster and the election of the channel
    `home` (RT-6.6, ADR-002): the agents whose status names it, and those
    whose status names no home (an older plugin), as before."""
    return [r for r in rows if r["home"] in (None, home)]


def _coordinator_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The reachable rows, the coordinator first and marked: the agent with a
    live 👑, else the oldest reachable one, which the lease hands it to next."""
    coord = next((r["id"] for r in rows if r["crown"]), "") or next(
        (r["id"] for r in rows if r["reachable"]), "")
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
        rows = await _rows(client, await client.work_channel_id())
    except Exception as exc:  # noqa: BLE001 — an empty roster
        log.debug("roster: %s", exc)
        return []
    home = client.work_channel_name
    guests = [entry(dict(r, crown=False), crown_of=str(r["home"]))
              for r in rows if r["home"] not in (None, home) and r["crown"]]
    return [entry(r) for r in _coordinator_first(_at_home(rows, home))] + guests


def entry(row: Mapping[str, Any], crown_of: str = "") -> RosterEntry:
    """One roster row as a bus type: the status word from the status text
    (after "coordinator"), else from its emoji. crown_of: the other home
    whose crown it holds."""
    text = [w.strip() for w in str(row.get("text", "")).split(" · ")]
    if text and text[0] == "coordinator":
        text = text[1:]
    status = text[0] if text and text[0] else _STATUS.get(str(row.get("emoji", "")), "")
    return RosterEntry(username=str(row.get("username") or ""), status=status,
                       coordinator=bool(row.get("coordinator")), crown=bool(row.get("crown")),
                       crown_of=crown_of)


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
    try:
        async with _lock():
            return await _command(client, action)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — fail open, like the status
        log.debug("lease %s: %s", action, exc)
        return ""


async def _command(client: ChatPort, action: str) -> str:
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

    async def take_now() -> None:
        _save_coord(uid, now + lease, 0, 0)
        await _render(client)
        if crown <= now:
            # The crown changed hands: say so in the channel's header (step 7).
            await _header(client, cid, _name(rows, uid), now)

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
    if holding and others:
        # Two crowns in one home (a race): the older agent keeps it.
        mine = next((r["create_at"] for r in rows if r["id"] == uid), None)
        if mine is not None and others[0]["create_at"] < mine:
            _save_coord(uid, 0, pending, yield_until)
            await _render(client)
        elif mine is not None:
            # The older keeps it: the younger may have named itself in the header.
            await _header(client, cid, _name(rows, uid), now, unless_named=True)
    elif not holding and not others and yield_until <= now:
        me_reachable = any(r["id"] == uid and r["reachable"] for r in rows)
        oldest = next((r["id"] for r in rows if r["reachable"]), "")
        if me_reachable and (pending == 1 or oldest == uid):
            await take_now()
    return ""


# --- the work channel's header (RT-6.6 step 7) ---------------------------------------

HEADER_MAX = 1024  # Mattermost's limit, in characters
HEADER_MARK = "🚌 agent bus"
HEADER_RULE = "plain post → 👑 routes it · @agent → that agent · 🙋 = a question for you"
# The bus part: the mark's line, and the rule's line after it.
_BUS_PART = re.compile(rf"\n*{re.escape(HEADER_MARK)}[^\n]*(?:\n{re.escape(HEADER_RULE[:12])}[^\n]*)?")
_header_logged = False


def _name(rows: list[dict[str, Any]], uid: str) -> str:
    return next((str(r["username"]) for r in rows if r["id"] == uid), "")


def bus_header(username: str, since: int) -> str:
    """The bus part of the work channel's header: who holds the crown since
    when, and how to address the agents."""
    at = time.strftime("%H:%M", time.localtime(since))
    return f"{HEADER_MARK} · {CROWN} @{username} since {at}\n{HEADER_RULE}"


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


async def _header(client: ChatPort, cid: str, username: str, since: int, *,
                  unless_named: bool = False) -> None:
    """Name the new crown holder in the work channel's header (RT-6.6 step 7).
    Mattermost posts a system message for every change, so this runs only
    when the crown changes hands, and writes nothing when the header already
    says it. Fails open: without permission it logs once, the crown works."""
    global _header_logged
    try:
        username = username or (await client.get_me()).username
        current = await client.channel_header(cid)
        if unless_named and f"{CROWN} @{username} since" in current:
            return
        new = with_bus_header(current, bus_header(username, since))
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


async def heartbeat(get_client: Callable[[], Awaitable[ChatPort | None]],
                    watch: Callable[[ChatPort], Awaitable[float | None]] | None = None) -> None:
    """Renew the lease until cancelled. Never raises: presence is cosmetic.
    `watch` runs after each tick (the stall check and the ask reminders,
    RT-6.19, stall.watch) and may bring the next tick forward to when it is
    due, PENDING_POLL_SEC apart at the least."""
    while True:
        delay = heartbeat_sec()
        try:
            client = await get_client()
            if client is not None and client.token:
                await command(client, "tick")
                if take_pending(await _user_id(client)):
                    delay = PENDING_POLL_SEC
                if watch is not None:
                    due = await watch(client)
                    if due is not None:
                        delay = min(delay, max(PENDING_POLL_SEC, due))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("lease heartbeat skipped: %s", exc)
        await asyncio.sleep(delay)

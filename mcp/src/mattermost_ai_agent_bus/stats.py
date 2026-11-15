"""Coordination overhead counters (RT-6.9 in docs/arc42/06-runtime-view.md).

What the bus adds to a session, not what the work costs: turns the plugin
caused (listener wake-ups, rounds the Stop hook forced), bus data put into the
context (tool results, the handoff after compaction), the agent's own posts,
the fixed text every turn carries (instructions, tool docs) and the skill's
size, in the context only once loaded, how long turns last, how long humans
take to answer the agent's asks, the waiting an answer on the bus saved, and
how long the session held the crown. report() sums the STATS lines up per
version, as bin/mm-agent-stats.sh does.

Hooks, listener and MCP server append one JSON line per event to
stats-<uid>.jsonl; every PART_SEC of session time (tick) and at session end
(summary, final) the MCP server posts one summary line to MM_STATS_CHANNEL.
A day's part carries what is still open (a turn, an ask, an answer whose
saving is not known yet, the crown) into the next one, so nothing counts
twice. No model is involved anywhere, and no content is recorded: no names,
no text; an ask's post id stays in the local counter file, never in the
summary, whose `sid` is random.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import secrets
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import overhead, session_file

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

_uid = ""
_client_name = ""
DEFAULT_CHANNEL = "agents-stats"
# RT-6.9 step 2: a session posts what it counted every day of session time, not
# only at its end: long sessions (days, resumed) rarely end. A constant, no switch.
PART_SEC = 24 * 3600
# Version and fixed text for a day's part: the server's (set_source).
_source: Callable[[], tuple[str, overhead.Fixed | None]] | None = None


def channel() -> str:
    """MM_STATS_CHANNEL, default agents-stats; "" when switched off."""
    name = os.environ.get("MM_STATS_CHANNEL", DEFAULT_CHANNEL).strip().lstrip("~")
    return "" if name.lower() in ("", "off", "none", "0", "false") else name


def set_user(uid: str) -> None:
    global _uid
    _uid = uid


def set_client(name: str) -> None:
    """The MCP client's name from the initialize handshake (clientInfo.name)."""
    global _client_name
    _client_name = name


def set_source(fn: Callable[[], tuple[str, overhead.Fixed | None]]) -> None:
    """Where tick() gets the version and the fixed text of a day's part."""
    global _source
    _source = fn


def harness(name: str | None = None) -> str:
    """The coding harness as a short label (RT-6.9): claude-code, cursor,
    opencode, codex; any other client by its own name, lowercased; without a
    name Claude Code from its environment, else other."""
    n = (_client_name if name is None else name).strip().lower()[:32]
    for label in ("claude-code", "cursor", "opencode", "codex"):
        if n.startswith(label):
            return label
    if n:
        return n
    return "claude-code" if os.environ.get("CLAUDECODE") else "other"


def _me() -> str:
    return os.environ.get("MM_BOT_USER_ID") or _uid


def _path() -> Path | None:
    uid = _me()
    if not uid:
        return None
    return session_file.state_dir() / f"stats-{uid}.jsonl"


def record(kind: str, /, **values: Any) -> None:
    """Append one event. Never raises: a counter must not disturb a session.
    kind is positional only: a block's own kind= is one of the values (RT-6.9)."""
    f = _path()
    if f is None or not channel():
        return
    try:
        session_file.append_private(f, json.dumps({"k": kind, "t": int(time.time()), **values}) + "\n")
    except OSError:
        pass


def transcript(path: str) -> str | None:
    """The transcript a hook noted, if this process may read it (RT-6.9).

    The host process of a keyless session (RT-6.18) got the path from the
    agent's side: it reads only a file whose real path lies under the host's
    ~/.claude/projects/, else measures nothing. session_file.read_transcript
    checks the opened file once more.
    """
    if not session_file.hosted():
        return path
    try:
        real = os.path.realpath(path)
        root = session_file.claude_projects()
    except (OSError, ValueError):
        return None
    return real if real.startswith(root + os.sep) else None


def _t(e: dict[str, Any]) -> int:
    try:
        return int(e.get("t", 0))
    except (TypeError, ValueError):
        return 0


def _load(f: Path) -> list[dict[str, Any]]:
    """The counter file's events in time order: a carried event keeps its time
    and may land after newer ones (a hook appending during a cut). Stable, so
    events of one second keep the file's order."""
    try:
        text = f.read_text()
    except OSError:
        return []
    return _parse(text)


def _parse(text: str) -> list[dict[str, Any]]:
    events = []
    for line in text.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if isinstance(e, dict):
            events.append(e)
    return sorted(events, key=_t)


def _part(events: list[dict[str, Any]]) -> dict[str, Any]:
    """This part's record (RT-6.9): the session's random id, the part's number
    and start, whether the runtime sends turn events. A file without one is
    the first part; it starts with its first event."""
    meta = next((e for e in reversed(events) if e.get("k") == "part"), None)
    if meta is None:
        return {"sid": "", "n": 1, "start": min(_t(e) for e in events), "hooks": False}
    return {"sid": str(meta.get("sid") or ""), "n": int(meta.get("n") or 1), "start": _t(meta),
            "hooks": bool(meta.get("hooks"))}


def _turns(events: list[dict[str, Any]]) -> tuple[list[tuple[bool, int]], dict[str, Any] | None]:
    """(started by the bus, seconds) per turn that ran from its start to the
    Stop hook that ended it, and the start still open at the end. A start no
    end follows (interrupted, hook missing) gets no duration rather than a
    wrong one (RT-6.9)."""
    out: list[tuple[bool, int]] = []
    began: dict[str, Any] | None = None
    for e in events:
        if e.get("k") == "turn":
            began = e
        elif e.get("k") == "turn_end" and began is not None:
            out.append((began.get("by") == "bus", max(0, _t(e) - _t(began))))
            began = None
    return out, began


def _asks(events: list[dict[str, Any]], now: float) -> dict[str, int]:
    """Asks posted, answered (waits), still unanswered at session end (RT-6.9),
    and the reminders sent (RT-6.19)."""
    posted = {e["id"]: _t(e) for e in events if e.get("k") == "ask" and e.get("id")}
    waits: dict[str, int] = {}
    for e in events:
        if e.get("k") == "answer" and e.get("id") and e["id"] not in waits:
            waits[e["id"]] = int(e.get("wait", 0))
    open_at = [t for pid, t in posted.items() if pid not in waits]
    return {
        "asks": len(set(posted) | set(waits)),
        "asks_answered": len(waits),
        "ask_wait_sec": sum(waits.values()),
        "ask_wait_max_sec": max(waits.values(), default=0),
        "asks_open": len(open_at),
        "ask_open_sec": sum(max(0, round(now - t)) for t in open_at),
        "ask_open_max_sec": max(0, round(now - min(open_at))) if open_at else 0,
        # Reminders the server sent for them (RT-6.19).
        "ask_reminders": sum(1 for e in events if e.get("k") == "remind"),
    }


def _human_turns(events: list[dict[str, Any]]) -> list[int]:
    """When a turn started that the bus did not start: the human at the terminal."""
    return sorted(_t(e) for e in events if e.get("k") == "turn" and e.get("by") != "bus")


def _answered_at(e: dict[str, Any]) -> int:
    """The answer's own time (its post's create_at); before 0.91.0 when counted."""
    return int(e.get("at") or _t(e))


def _saved(events: list[dict[str, Any]]) -> tuple[dict[str, int], list[int]]:
    """Saved waiting (RT-6.9 step 1): of the asks a trusted human answered on
    the bus, those answered while the terminal was idle (no turn a human
    started between the ask and the answer; a carried ask's `busy` remembers
    one before the cut), and for each the seconds from the answer to the next
    turn a human started (`saved`). Also the answers no such turn followed
    yet: the caller carries them on or counts them open."""
    human = _human_turns(events)
    asked: dict[str, dict[str, Any]] = {}
    for e in events:
        if e.get("k") == "ask" and e.get("id") and e["id"] not in asked:
            asked[e["id"]] = e
    seen: set[str] = set()
    idle: list[int] = []
    for e in events:
        if e.get("k") != "answer" or not e.get("id") or e["id"] in seen:
            continue
        seen.add(e["id"])
        ask, at = asked.get(e["id"]), _answered_at(e)
        if ask is None or ask.get("busy") or any(_t(ask) < h < at for h in human):
            continue
        idle.append(at)
    waiting = idle + [_answered_at(e) for e in events if e.get("k") == "saving"]
    saved: list[int] = []
    pending: list[int] = []
    for at in waiting:
        back = next((h for h in human if h >= at), None)
        if back is None:
            pending.append(at)
        else:
            saved.append(back - at)
    return {"asks_idle": len(idle), "saved_n": len(saved), "saved_sec": sum(saved),
            "saved_max_sec": max(saved, default=0)}, pending


def _crown_until() -> int:
    from . import lease  # lease records the crown's changes here
    return lease.crown_until(_me()) if _me() else 0


def _crown(events: list[dict[str, Any]], start: int, now: float) -> tuple[int, bool]:
    """Seconds this session held the crown in the part (RT-6.9, RT-6.6), and
    whether it still holds it. The heartbeat notes each change; a crown still
    on counts until now or, when it lapsed meanwhile (a server that was down),
    until its lease ran out."""
    total, on_at = 0, None
    for e in events:
        if e.get("k") != "crown":
            continue
        if e.get("on"):
            if on_at is None:
                on_at = max(_t(e), start)
        elif on_at is not None:
            total += max(0, _t(e) - on_at)
            on_at = None
    if on_at is None:
        return total, False
    until = _crown_until()
    end = min(now, until) if until > 0 else now
    return total + max(0, round(end - on_at)), until > now


def crown_open() -> bool:
    """Whether the counter file's last crown event says held: the heartbeat's
    start, so a crown that lapsed while no server ran gets its end."""
    f = _path()
    if f is None:
        return False
    last = [e for e in _load(f) if e.get("k") == "crown"]
    return bool(last and last[-1].get("on"))


# --- what woke a turn (RT-6.9 step 1, settled without a human) ----------------
# The server sees each turn's start and end (the hooks' presence, RT-6.17) and
# everything it hands the agent (pushed or returned by wait_for_events), with
# the server-verified author class (RT-6.8). What woke a turn: what it handed
# over while no turn ran (a push the turn takes up, RT-6.12 step 4), else the
# first hand-over inside the turn (what a listener that ended woke it for,
# collected with wait_for_events, RT-6.2). Noted at the turn's end as `woke`;
# the summary pairs it with the turn the bus started.
_waking: set[str] = set()       # classes handed over while no turn ran
_woke: set[str] | None = None   # the running turn's; None while unknown
_running = False


def delivered(classes: set[str], trusted: int = 0, other: int = 0) -> None:
    """The agent got posts or reactions (RT-6.9): their authors' classes
    ("agent", "human"), and how many were posts by a trusted human and by one
    not trusted. Never raises."""
    global _woke
    classes = classes & {"agent", "human"}
    if not _running:
        _waking.update(classes)
    elif _woke is None and classes:
        _woke = set(classes)
    if trusted or other:
        record("heard", trusted=trusted, other=other)


def turn(running: bool) -> None:
    """A turn begins or ends (the hooks' presence working / idle). A turn that
    began again without an end (interrupted) notes nothing, like its duration."""
    global _woke, _running
    if running:
        _woke = set(_waking) or None
        _waking.clear()
    elif _running and _woke:
        record("woke", agent=int("agent" in _woke), human=int("human" in _woke))
    if not running:
        _woke = None
    _running = running


def _woken(events: list[dict[str, Any]], out: set[int]) -> dict[str, int]:
    """bus_turns_agent / bus_turns_human: the turns the bus started, by what
    woke them: only agents' posts, or a human's among them. A `woke` belongs
    to the turn last started before it; one the bus did not start counts
    nothing, nor does a bus turn nothing classified (a listen window that ran
    out, a stall event)."""
    agent = human = 0
    last: dict[str, Any] | None = None
    paired: set[int] = set()
    for e in events:
        if e.get("k") == "turn":
            last = e
        elif (e.get("k") == "woke" and last is not None and last.get("by") == "bus"
              and id(last) not in paired and id(last) not in out):
            paired.add(id(last))
            if e.get("human"):
                human += 1
            elif e.get("agent"):
                agent += 1
    return {"bus_turns_agent": agent, "bus_turns_human": human}


def _settled(counted: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, int]:
    """Questions to the 👑 and to humans (counted where they close, as `asks`),
    those the 👑 answered (advice, RT-6.25 step 4), tasks this agent closed
    (DONE) with or without a human in the thread, and what humans did: posts
    that reached the agent, trusted or not, and answers to its asks."""
    advised = {e["id"] for e in events if e.get("k") == "advice" and e.get("id")}
    asks = {e["id"]: e for e in counted if e.get("k") == "ask" and e.get("id")}
    crown = [pid for pid, e in asks.items() if e.get("coord")]
    done = [e for e in counted if e.get("k") == "task_done"]
    heard = [e for e in counted if e.get("k") == "heard"]
    answers = {e["id"] for e in counted if e.get("k") == "answer" and e.get("id")}
    return {
        "asks_crown": len(crown),
        "asks_crown_answered": sum(1 for pid in crown if pid in advised),
        "asks_human": len(asks) - len(crown),
        "tasks_agent_only": sum(1 for e in done if not e.get("human")),
        "tasks_with_human": sum(1 for e in done if e.get("human")),
        "human_posts_trusted": sum(int(e.get("trusted", 0)) for e in heard),
        "human_posts_other": sum(int(e.get("other", 0)) for e in heard),
        "human_answers": len(answers),
    }


def _summarize(events: list[dict[str, Any]], version: str, fixed: overhead.Fixed | None,
               now: float, final: bool) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """One STATS line for the part in `events`, and, for a day's part (not
    final), the events to carry into the next one (RT-6.9 step 2): the turn
    still running, the asks still open (with `busy` once a human turn came
    after them), the answers still waiting for the human's next turn, the
    crown when held, the part's own record and the transcript's path. What is
    carried counts in the part where it ends, or at the session's end."""
    part = _part(events)
    start = part["start"]
    carry: list[dict[str, Any]] = []
    out: set[int] = set()  # events carried on: not counted in this part
    if not final:
        _, running = _turns(events)
        if running is not None:
            carry.append(running)
            out.add(id(running))
        # Closed: answered by a trusted human; a question to the 👑 also by its
        # advice or when it was passed on to the human (RT-6.25).
        closed = {e["id"] for e in events if e.get("k") in ("answer", "advice", "forwarded") and e.get("id")}
        human = _human_turns(events)
        for e in events:
            if e.get("k") == "ask" and e.get("id") and e["id"] not in closed:
                busy = bool(e.get("busy")) or any(h > _t(e) for h in human)
                carry.append(e | {"busy": 1} if busy else e)
                out.add(id(e))
    counted = [e for e in events if id(e) not in out]
    kinds = Counter(e.get("k") for e in counted)
    hooked = part["hooks"] or any(e.get("k") in ("turn", "turn_end") for e in events)
    tools: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in counted:
        if e.get("k") == "tool":
            tools[e.get("name", "?")][0] += 1
            tools[e.get("name", "?")][1] += int(e.get("chars", 0))
    posts = [e for e in counted if e.get("k") == "post"]
    compacts = [e for e in counted if e.get("k") == "compact"]
    paths = [e for e in events if e.get("k") == "transcript" and e.get("path")]
    tok = None
    path = transcript(str(paths[-1]["path"])) if paths else None
    if path and fixed is not None:
        try:
            tok = overhead.measure(path, start, fixed, None if final else now)
        except Exception:  # noqa: BLE001 — a transcript we cannot read costs only this field
            tok = None
    turns, _ = _turns(counted)
    bus_turns = [sec for by_bus, sec in turns if by_bus]
    saved, pending = _saved(events)
    crown_sec, crowned = _crown(events, start, now)
    sid = part["sid"] or secrets.token_hex(6)
    data = {
        "v": version,
        # The session (random, RT-6.9) and which of its parts this is: the
        # report counts a session once and sums its parts.
        "sid": sid,
        "part": part["n"],
        **({"final": True} if final else {}),
        "rt": harness(),
        "kind": "autonomous" if os.environ.get("MM_AGENT_KIND") == "autonomous" else "session",
        "min": max(0, round((now - start) / 60)),
        "turns": kinds["turn"],
        # Only a runtime with turn hooks (Claude Code) sends turn events; for
        # the others the fields stay absent, never zero (RT-6.9).
        **({
            "turns_timed": len(turns),
            "turn_sec": sum(sec for _, sec in turns),
            "turn_max_sec": max((sec for _, sec in turns), default=0),
            "bus_turns": sum(1 for e in counted if e.get("k") == "turn" and e.get("by") == "bus"),
            "bus_turns_timed": len(bus_turns),
            "bus_turn_sec": sum(bus_turns),
            "bus_turn_max_sec": max(bus_turns, default=0),
        } if hooked else {}),
        "wakes": dict(Counter(e.get("reason", "?") for e in counted if e.get("k") == "wake")),
        "blocks": dict(Counter(e.get("kind", "?") for e in counted if e.get("k") == "block")),
        "tools": dict(tools),
        "posts": [len(posts), sum(int(p.get("chars", 0)) for p in posts)],
        "compact": [len(compacts), sum(int(c.get("chars", 0)) for c in compacts)],
        # Per turn: instructions and tool docs. Per load: the skill, in the
        # context only when the agent loaded it; tok.skill says whether (RT-6.9).
        **({"static": fixed.instructions + fixed.tools, "skill": fixed.skill} if fixed else {}),
        **_asks(counted, now),
        # Saved waiting needs the human's turns: only with turn hooks. Answers
        # no human turn followed by the session's end are a lower bound.
        **(saved | {
            "saved_open_n": len(pending) if final else 0,
            "saved_open_sec": sum(max(0, round(now - at)) for at in pending) if final else 0,
        } if hooked else {}),
        "crown_sec": crown_sec,
        # Settled without a human (RT-6.9 step 1): who woke the bus's turns
        # (only with turn hooks), questions the 👑 settled, tasks closed
        # between agents, and how often humans stepped in.
        **(_woken(events, out) if hooked else {}),
        **_settled(counted, events),
        **({"tok": tok} if tok else {}),
    }
    if not final:
        cut = int(now)
        carry = [{"k": "part", "t": cut, "sid": sid, "n": part["n"] + 1, **({"hooks": 1} if hooked else {})},
                 *carry,
                 *([{"k": "crown", "t": cut, "on": 1}] if crowned else []),
                 *([{"k": "saving", "t": at, "at": at} for at in pending] if hooked else []),
                 *([paths[-1]] if paths else [])]
    return data, carry


def summary(version: str, fixed: overhead.Fixed | None = None) -> dict[str, Any] | None:
    """The session's last part (`final`): what the counter file holds,
    whatever is still open counted as open (RT-6.9 step 2)."""
    f = _path()
    if f is None:
        return None
    _recover(f)
    if not f.exists():
        return None
    events = _load(f)
    if not events:
        return None
    return _summarize(events, version, fixed, time.time(), final=True)[0]


def due(now: float | None = None) -> float | None:
    """Seconds until the current part is PART_SEC old (0 or less: due), None
    without counters. Reads only the part records and the first event: before
    the first cut the file is in the order the events came."""
    f = _path()
    if f is None:
        return None
    try:
        text = f.read_text()
    except OSError:
        return None
    first: int | None = None
    part: int | None = None
    for line in text.splitlines():
        if first is not None and '"part"' not in line:
            continue
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict):
            continue
        if first is None:
            first = _t(e)
        if e.get("k") == "part":
            part = _t(e)
    start = part if part is not None else first
    if start is None:
        return None
    return start + PART_SEC - (time.time() if now is None else now)


def _snap(f: Path) -> Path:
    return f.with_name(f.name + ".cut")


def _recover(f: Path) -> None:
    """A cut that died before it wrote the next part (a crash, a full disk)
    left its counters beside the file: they go back into it, to be counted at
    the next cut or the end (RT-6.9 step 2). Nothing of that cut was carried
    yet, so nothing counts twice."""
    snap = _snap(f)
    if not snap.exists() or snap.is_symlink():
        return
    try:
        session_file.append_private(f, snap.read_text())
        snap.unlink()
    except OSError as exc:
        log.info("overhead stats: %s not recovered: %s", snap.name, exc)


def cut(version: str, fixed: overhead.Fixed | None = None, now: float | None = None) -> dict[str, Any] | None:
    """End a day's part (RT-6.9 step 2): the counter file is moved aside (a
    hook appends to a new one from then on), summed up, and what is still
    open goes into the new file with the next part's record, in one write
    (a new file renamed in) together with what hooks appended meanwhile, to
    either file. The STATS line of the part, or None without counters."""
    f = _path()
    if f is None:
        return None
    _recover(f)
    if not f.exists() or f.is_symlink():
        return None
    now = time.time() if now is None else now
    snap = _snap(f)
    try:
        os.replace(f, snap)
        text = snap.read_text()
    except OSError:
        return None
    try:
        events = _parse(text)
        data, carry = _summarize(events, version, fixed, now, final=False) if events else (None, [])
    except Exception:
        _recover(f)  # counted at the next try or the end
        raise
    if data is None:
        _recover(f)
        return None
    # A hook that opened the old file before the move appends there: those
    # lines go on with the next part, as do those already in the new file.
    late = snap.read_text()[len(text):]
    try:
        fresh = f.read_text()
    except FileNotFoundError:
        fresh = ""
    session_file.write_private(f, "".join(json.dumps(e) + "\n" for e in carry) + late + fresh)
    snap.unlink(missing_ok=True)
    return data


def _lock(uid: str) -> int | None:
    """stats-<uid>.lock, so two servers of one bot cut a part once."""
    path = session_file.state_dir() / f"stats-{uid}.lock"
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


# Day's parts cut but not posted yet (the post failed): posted before the next
# part and before the session's last line, so no day is lost to a blip.
_unposted: list[str] = []


def _line(data: dict[str, Any]) -> str:
    return "STATS " + json.dumps(data, separators=(",", ":"))


async def tick(client: ChatPort, uid: str) -> float | None:
    """The heartbeat's part check (stall.watch, RT-6.9 step 2): once the
    current part is a day old, post its STATS line and start the next.
    The channel is joined first, so a server that cannot reach it cuts
    nothing and tries again at the next tick; a part whose post failed is
    posted first then. Seconds until the next part is due; never raises."""
    name = channel()
    if not name or _source is None or not client.token or not uid:
        return None
    set_user(uid)
    left = due()
    if (left is None or left > 0) and not _unposted:
        return left
    fd = _lock(uid)
    if fd is None:
        return None
    try:
        cid = await client.join_channel(name)
        while _unposted:
            await client.post(_unposted[0], channel_id=cid)
            _unposted.pop(0)
        left = due()  # another server may have cut it meanwhile
        if left is None or left > 0:
            return left
        version, fixed = _source()
        data = cut(version, fixed)
        if data:
            _unposted.append(_line(data))
            await client.post(_unposted[-1], channel_id=cid)
            _unposted.pop()
            log.info("overhead stats: part %s posted", data["part"])
        return PART_SEC
    except Exception as exc:  # noqa: BLE001 — the heartbeat goes on
        log.info("overhead stats part skipped: %s", exc)
        return None
    finally:
        os.close(fd)


def post_sync(client: ChatPort, data: dict[str, Any]) -> str:
    """Post the summary to the stats channel, synchronously (exit handler),
    after any day's part whose post had failed.

    The counter file and its lock go either way: on a server without the
    channel the counts would only pile up.
    """
    name = channel()
    if not name or not client.token:
        return "off"
    lines = [*_unposted, _line(data)]
    _unposted.clear()
    try:
        for msg in lines:
            if not client.post_by_name_sync(name, msg, join=_me()):
                return f"no channel ~{name}; nothing posted"
        return "posted"
    except Exception as exc:  # noqa: BLE001 — exit handler: fail open
        return f"failed: {type(exc).__name__}"
    finally:
        f = _path()
        if f is not None:
            f.unlink(missing_ok=True)
            (f.parent / f"stats-{_me()}.lock").unlink(missing_ok=True)


# --- the report: what bin/mm-agent-stats.sh report --json computes in jq -----
# The same fields and arithmetic (jq's round is half away from zero, an absent
# value adds 0), so the shell report and the stats_report tool agree
# (tests/hooks/test_stats.py compares them).

def _round(x: float) -> int:
    return int(math.floor(abs(x) + 0.5)) * (1 if x >= 0 else -1)


def _sum(rows: list[dict[str, Any]], f: Callable[[dict[str, Any]], Any]) -> Any:
    return sum((f(r) or 0) for r in rows)


def _max(values: Iterable[Any]) -> Any:
    found = [v for v in values if v is not None]
    return max(found) if found else None


def _pct(a: float, b: float) -> int | None:
    return _round(a * 100 / b) if b > 0 else None


def _tenth(a: float, b: float) -> float | None:
    return _round(a / b * 10) / 10 if b > 0 else None


def _per(a: float, b: float) -> int | None:
    return _round(a / b) if b > 0 else None


def _eq(t: dict[str, Any]) -> float:
    """Input-token equivalents: what a token type costs against an uncached input token."""
    return ((t.get("in") or 0) + (t.get("read") or 0) * 0.1 + (t.get("w5m") or 0) * 1.25
            + (t.get("w1h") or 0) * 2 + (t.get("out") or 0) * 5)


def _vals(d: Any) -> int:
    return sum(d.values()) if isinstance(d, dict) and d else 0


def _sessions(rows: list[dict[str, Any]]) -> int:
    """Distinct sessions: a session's parts share its sid; a line without one
    (before 0.91.0) is a session of its own (report sets _s)."""
    return len({r["_s"] for r in rows})


def _turns_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [r for r in rows if r.get("turns_timed") is not None]
    if not rows:
        return None
    t: dict[str, Any] = {
        "sessions": _sessions(rows),
        "turns": _sum(rows, lambda r: r.get("turns")),
        "human_prompts": _sum(rows, lambda r: (r.get("turns") or 0) - (r.get("bus_turns") or 0)),
        "timed": _sum(rows, lambda r: r.get("turns_timed")),
        "sec": _sum(rows, lambda r: r.get("turn_sec")),
        "max_sec": max(r.get("turn_max_sec") or 0 for r in rows),
        "bus_turns": _sum(rows, lambda r: r.get("bus_turns")),
        "bus_timed": _sum(rows, lambda r: r.get("bus_turns_timed")),
        "bus_sec": _sum(rows, lambda r: r.get("bus_turn_sec")),
        "bus_max_sec": max(r.get("bus_turn_max_sec") or 0 for r in rows),
        "answered": _sum(rows, lambda r: r.get("asks_answered")),
    }
    return t | {
        "turns_per_prompt": _tenth(t["turns"], t["human_prompts"]),
        "mean_sec": _per(t["sec"], t["timed"]),
        "bus_mean_sec": _per(t["bus_sec"], t["bus_timed"]),
        "bus_share_pct": _pct(t["bus_sec"], t["sec"]),
        "sec_per_input": _per(t["sec"], t["human_prompts"] + t["answered"]),
    }


def _asks_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [r for r in rows if r.get("asks") is not None]
    if not rows:
        return None
    a: dict[str, Any] = {
        "sessions": _sessions(rows),
        "hours": _sum(rows, lambda r: r.get("min")) / 60,
        "asks": _sum(rows, lambda r: r.get("asks")),
        "answered": _sum(rows, lambda r: r.get("asks_answered")),
        "wait_sec": _sum(rows, lambda r: r.get("ask_wait_sec")),
        "max_wait_sec": max(r.get("ask_wait_max_sec") or 0 for r in rows),
        "open": _sum(rows, lambda r: r.get("asks_open")),
        "open_sec": _sum(rows, lambda r: r.get("ask_open_sec")),
        "open_max_sec": max(r.get("ask_open_max_sec") or 0 for r in rows),
    }
    h = a["hours"]
    return a | {
        "asks_per_h": _tenth(a["asks"], h),
        "answered_pct": _pct(a["answered"], a["asks"]),
        "mean_wait_sec": _per(a["wait_sec"], a["answered"]),
        "waiting_pct": _pct(a["wait_sec"] + a["open_sec"], h * 3600),
        "hours": _round(h * 10) / 10,
    }


def _saved_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Saved waiting (since 0.91.0, sessions with turn hooks): answers on the
    bus, those while the terminal was idle, and the time to the human's next
    turn there; `saved_open_*` a lower bound for answers no such turn followed."""
    rows = [r for r in rows if r.get("asks_idle") is not None]
    if not rows:
        return None
    s: dict[str, Any] = {
        "sessions": _sessions(rows),
        "answered": _sum(rows, lambda r: r.get("asks_answered")),
        "idle": _sum(rows, lambda r: r.get("asks_idle")),
        "saved_n": _sum(rows, lambda r: r.get("saved_n")),
        "saved_sec": _sum(rows, lambda r: r.get("saved_sec")),
        "saved_max_sec": max(r.get("saved_max_sec") or 0 for r in rows),
        "saved_open_n": _sum(rows, lambda r: r.get("saved_open_n")),
        "saved_open_sec": _sum(rows, lambda r: r.get("saved_open_sec")),
    }
    return s | {
        "idle_pct": _pct(s["idle"], s["answered"]),
        "mean_saved_sec": _per(s["saved_sec"], s["saved_n"]),
    }


def _settled_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Settled without a human (since 0.91.0): the share of the bus's turns
    only agents woke (with turn hooks), of the questions the 👑 answered, of
    the tasks closed with no human in the thread, and human posts and answers
    per agent-day (the parts' minutes / 1440). None without the fields."""
    rows = [r for r in rows if r.get("asks_crown") is not None]
    if not rows:
        return None
    hooked = [r for r in rows if r.get("bus_turns_agent") is not None]
    s: dict[str, Any] = {
        "sessions": _sessions(rows),
        "parts": len(rows),
        "days": _sum(rows, lambda r: r.get("min")) / 1440,
        "bus_turns_agent": _sum(hooked, lambda r: r.get("bus_turns_agent")) if hooked else None,
        "bus_turns_human": _sum(hooked, lambda r: r.get("bus_turns_human")) if hooked else None,
        "asks_crown": _sum(rows, lambda r: r.get("asks_crown")),
        "asks_crown_answered": _sum(rows, lambda r: r.get("asks_crown_answered")),
        "asks_human": _sum(rows, lambda r: r.get("asks_human")),
        "tasks_agent_only": _sum(rows, lambda r: r.get("tasks_agent_only")),
        "tasks_with_human": _sum(rows, lambda r: r.get("tasks_with_human")),
        "human_posts_trusted": _sum(rows, lambda r: r.get("human_posts_trusted")),
        "human_posts_other": _sum(rows, lambda r: r.get("human_posts_other")),
        "human_answers": _sum(rows, lambda r: r.get("human_answers")),
    }
    d = s["days"]
    return s | {
        "agent_turns_pct": (_pct(s["bus_turns_agent"], s["bus_turns_agent"] + s["bus_turns_human"])
                            if hooked else None),
        "crown_settled_pct": _pct(s["asks_crown_answered"], s["asks_crown"] + s["asks_human"]),
        "tasks_agent_pct": _pct(s["tasks_agent_only"], s["tasks_agent_only"] + s["tasks_with_human"]),
        "human_posts_per_day": _tenth(s["human_posts_trusted"] + s["human_posts_other"], d),
        "human_answers_per_day": _tenth(s["human_answers"], d),
        "days": _round(d * 10) / 10,
    }


def _tok_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [r for r in rows if r.get("tok")]
    if not rows:
        return None
    t: dict[str, Any] = {
        "sessions": _sessions(rows),
        "hours": _sum(rows, lambda r: r.get("min")) / 60,
        "req": _sum(rows, lambda r: r["tok"].get("req")),
        "bus_req": _sum(rows, lambda r: r["tok"].get("bus_req")),
        "fixed": _round(_sum(rows, lambda r: r["tok"].get("fixed")) / len(rows)),
        # A session that loaded the skill counts once, over all its parts.
        "skill_loaded": len({r["_s"] for r in rows if (r["tok"].get("skill") or 0) > 0}),
        "all": _sum(rows, lambda r: _eq(r["tok"].get("all") or {})),
        "bus": _sum(rows, lambda r: _eq(r["tok"].get("bus") or {})),
    }
    return t | {
        "req_per_h": _per(t["req"], t["hours"]),
        "bus_req_per_h": _tenth(t["bus_req"], t["hours"]),
        "bus_per_h": _per(t["bus"], t["hours"]),
        "all_per_h": _per(t["all"], t["hours"]),
        "bus_share_pct": _round(t["bus"] * 1000 / t["all"]) / 10 if t["all"] > 0 else None,
    }


def _version_key(v: str) -> list[int]:
    return [int(p) if p.isdigit() else 0 for p in v.split(".")]


def _role(r: dict[str, Any]) -> str:
    """A part's role (RT-6.9 step 3): coordinator when it held the crown more
    than half its time, else worker; unknown before 0.91.0 (no crown_sec). Per
    part, so a session that coordinates one day and works the next counts as
    both."""
    if r.get("crown_sec") is None:
        return "unknown"
    return "coordinator" if r["crown_sec"] * 2 > (r.get("min") or 0) * 60 else "worker"


def report(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The STATS lines summed up per version, oldest first (RT-6.9 step 3).
    A session's parts (one sid) count as one session."""
    lines = [r | {"_s": r["sid"] if r.get("sid") not in (None, False) else f"#{i}"}
             for i, r in enumerate(lines)]
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        groups[str(line.get("v"))].append(line)
    out = []
    for v in sorted(sorted(groups), key=_version_key):
        rows = groups[v]
        by_rt: dict[str, list[dict[str, Any]]] = defaultdict(list)
        by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_rt[str(r["rt"]) if r.get("rt") is not None else "other"].append(r)
            by_role[_role(r)].append(r)
        hours = _round(_sum(rows, lambda r: r.get("min")) / 60 * 10) / 10
        turns = _sum(rows, lambda r: r.get("turns"))
        forced = _sum(rows, lambda r: _vals(r.get("blocks")))
        # Before 0.36.0 (no bus_turns) the listener's wake-ups stand in.
        plugin = _sum(rows, lambda r: (r["bus_turns"] if r.get("bus_turns") is not None
                                       else _vals(r.get("wakes"))) + _vals(r.get("blocks")))
        bus_chars = _sum(rows, lambda r: sum(t[1] for t in (r.get("tools") or {}).values())
                         + (r.get("compact") or [0, 0])[1])
        post_chars = _sum(rows, lambda r: (r.get("posts") or [0, 0])[1])
        everything = turns + forced
        sessions = _sessions(rows)
        out.append({
            "version": v, "sessions": sessions, "parts": len(rows), "hours": hours, "turns": turns,
            "forced": forced, "plugin_turns": plugin, "bus_chars": bus_chars, "post_chars": post_chars,
            "static": _max(r.get("static") for r in rows),
            "skill": _max(r.get("skill") for r in rows),
            "tok": _tok_of(rows),
            "settled": _settled_of(rows),
            "saved": _saved_of(rows),
            "harness": [{"harness": rt, "sessions": _sessions(rs), "turns": _turns_of(rs),
                         "asks": _asks_of(rs), "saved": _saved_of(rs)}
                        for rt, rs in sorted(by_rt.items())],
            "roles": [{"role": role, "parts": len(rs), "sessions": _sessions(rs),
                       "hours": _round(_sum(rs, lambda r: r.get("min")) / 60 * 10) / 10,
                       "tok": _tok_of(rs), "turns": _turns_of(rs), "asks": _asks_of(rs),
                       "settled": _settled_of(rs)}
                      for role, rs in sorted(by_role.items())],
            "plugin_turns_per_h": _tenth(plugin, hours),
            "all_turns": everything,
            "plugin_share_pct": _pct(plugin, everything),
            "bus_chars_per_turn": _per(bus_chars, everything),
            "post_chars_per_session": _round(post_chars / sessions),
        })
    return out


async def read_report(client: ChatPort, name: str, max_pages: int = 10) -> list[dict[str, Any]]:
    """report() of the STATS lines among the last 2000 posts of the channel
    `name`, read with the bot's own session (RT-6.9 step 3)."""
    cid = await client.join_channel(name)
    lines = []
    for p in await client.posts_after(0, channel_id=cid, max_pages=max_pages):
        if p.message.startswith("STATS "):
            try:
                data = json.loads(p.message[len("STATS "):])
            except ValueError:
                continue
            if isinstance(data, dict):
                lines.append(data)
    return report(lines)

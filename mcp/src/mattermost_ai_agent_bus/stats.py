"""Coordination overhead counters (RT-6.9 in docs/arc42/06-runtime-view.md).

What the bus adds to a session, not what the work costs: turns the plugin
caused (listener wake-ups, rounds the Stop hook forced), bus data put into the
context (tool results, the handoff after compaction), the agent's own posts,
the fixed text every turn carries (instructions, tool docs) and the skill's
size, in the context only once loaded, how long turns last, and how long
humans take to answer the agent's asks. report() sums the STATS lines up per
version, as bin/mm-agent-stats.sh does.

Hooks, listener and MCP server append one JSON line per event to
stats-<uid>.jsonl; at session end the MCP server posts one summary line to
MM_STATS_CHANNEL. No model is involved anywhere, and no content is recorded:
no names, no text; an ask's post id stays in the local counter file, never in
the summary.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import overhead, session_file

if TYPE_CHECKING:
    from .port import ChatPort

_uid = ""
_client_name = ""
DEFAULT_CHANNEL = "agents-stats"


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


def _path() -> Path | None:
    uid = os.environ.get("MM_BOT_USER_ID") or _uid
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


def _turns(events: list[dict[str, Any]]) -> list[tuple[bool, int]]:
    """(started by the bus, seconds) per turn that ran from its start to the
    Stop hook that ended it. A start no end follows (interrupted, hook missing)
    gets no duration rather than a wrong one (RT-6.9)."""
    out: list[tuple[bool, int]] = []
    began: tuple[int, bool] | None = None
    for e in events:
        if e.get("k") == "turn":
            began = (int(e.get("t", 0)), e.get("by") == "bus")
        elif e.get("k") == "turn_end" and began:
            out.append((began[1], max(0, int(e.get("t", 0)) - began[0])))
            began = None
    return out


def _asks(events: list[dict[str, Any]], now: float) -> dict[str, int]:
    """Asks posted, answered (waits), still unanswered at session end (RT-6.9),
    and the reminders sent (RT-6.19)."""
    posted = {e["id"]: int(e.get("t", 0)) for e in events if e.get("k") == "ask" and e.get("id")}
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


def summary(version: str, fixed: overhead.Fixed | None = None) -> dict[str, Any] | None:
    f = _path()
    if f is None or not f.exists():
        return None
    events = []
    for line in f.read_text().splitlines():
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    if not events:
        return None
    kinds = Counter(e.get("k") for e in events)
    tools: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for e in events:
        if e.get("k") == "tool":
            tools[e.get("name", "?")][0] += 1
            tools[e.get("name", "?")][1] += int(e.get("chars", 0))
    posts = [e for e in events if e.get("k") == "post"]
    compacts = [e for e in events if e.get("k") == "compact"]
    start = min(e.get("t", 0) for e in events)
    paths = [e.get("path") for e in events if e.get("k") == "transcript" and e.get("path")]
    tok = None
    path = transcript(str(paths[-1])) if paths else None
    if path and fixed is not None:
        try:
            tok = overhead.measure(path, start, fixed)
        except Exception:  # noqa: BLE001 — a transcript we cannot read costs only this field
            tok = None
    turns = _turns(events)
    bus_turns = [sec for by_bus, sec in turns if by_bus]
    return {
        "v": version,
        "rt": harness(),
        "kind": "autonomous" if os.environ.get("MM_AGENT_KIND") == "autonomous" else "session",
        "min": max(0, round((time.time() - start) / 60)),
        "turns": kinds["turn"],
        # Only a runtime with turn hooks (Claude Code) sends turn events; for
        # the others the fields stay absent, never zero (RT-6.9).
        **({
            "turns_timed": len(turns),
            "turn_sec": sum(sec for _, sec in turns),
            "turn_max_sec": max((sec for _, sec in turns), default=0),
            "bus_turns": sum(1 for e in events if e.get("k") == "turn" and e.get("by") == "bus"),
            "bus_turns_timed": len(bus_turns),
            "bus_turn_sec": sum(bus_turns),
            "bus_turn_max_sec": max(bus_turns, default=0),
        } if kinds["turn"] else {}),
        "wakes": dict(Counter(e.get("reason", "?") for e in events if e.get("k") == "wake")),
        "blocks": dict(Counter(e.get("kind", "?") for e in events if e.get("k") == "block")),
        "tools": dict(tools),
        "posts": [len(posts), sum(int(p.get("chars", 0)) for p in posts)],
        "compact": [len(compacts), sum(int(c.get("chars", 0)) for c in compacts)],
        # Per turn: instructions and tool docs. Per load: the skill, in the
        # context only when the agent loaded it; tok.skill says whether (RT-6.9).
        **({"static": fixed.instructions + fixed.tools, "skill": fixed.skill} if fixed else {}),
        **_asks(events, time.time()),
        **({"tok": tok} if tok else {}),
    }


def post_sync(client: ChatPort, data: dict[str, Any]) -> str:
    """Post the summary to the stats channel, synchronously (exit handler).

    The counter file goes either way: on a server without the channel the
    counts would only pile up.
    """
    name = channel()
    if not name or not client.token:
        return "off"
    msg = "STATS " + json.dumps(data, separators=(",", ":"))
    try:
        if not client.post_by_name_sync(name, msg, join=os.environ.get("MM_BOT_USER_ID") or _uid):
            return f"no channel ~{name}; nothing posted"
        return "posted"
    except Exception as exc:  # noqa: BLE001 — exit handler: fail open
        return f"failed: {type(exc).__name__}"
    finally:
        f = _path()
        if f is not None:
            f.unlink(missing_ok=True)


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


def _turns_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [r for r in rows if r.get("turns_timed") is not None]
    if not rows:
        return None
    t: dict[str, Any] = {
        "sessions": len(rows),
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
        "sessions": len(rows),
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


def _tok_of(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    rows = [r for r in rows if r.get("tok")]
    if not rows:
        return None
    t: dict[str, Any] = {
        "sessions": len(rows),
        "hours": _sum(rows, lambda r: r.get("min")) / 60,
        "req": _sum(rows, lambda r: r["tok"].get("req")),
        "bus_req": _sum(rows, lambda r: r["tok"].get("bus_req")),
        "fixed": _round(_sum(rows, lambda r: r["tok"].get("fixed")) / len(rows)),
        "skill_loaded": _sum(rows, lambda r: r["tok"].get("skill")),
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


def report(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The STATS lines summed up per version, oldest first (RT-6.9 step 3)."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for line in lines:
        groups[str(line.get("v"))].append(line)
    out = []
    for v in sorted(sorted(groups), key=_version_key):
        rows = groups[v]
        by_rt: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_rt[str(r["rt"]) if r.get("rt") is not None else "other"].append(r)
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
        out.append({
            "version": v, "sessions": len(rows), "hours": hours, "turns": turns, "forced": forced,
            "plugin_turns": plugin, "bus_chars": bus_chars, "post_chars": post_chars,
            "static": _max(r.get("static") for r in rows),
            "skill": _max(r.get("skill") for r in rows),
            "tok": _tok_of(rows),
            "harness": [{"harness": rt, "sessions": len(rs), "turns": _turns_of(rs), "asks": _asks_of(rs)}
                        for rt, rs in sorted(by_rt.items())],
            "plugin_turns_per_h": _tenth(plugin, hours),
            "all_turns": everything,
            "plugin_share_pct": _pct(plugin, everything),
            "bus_chars_per_turn": _per(bus_chars, everything),
            "post_chars_per_session": _round(post_chars / len(rows)),
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

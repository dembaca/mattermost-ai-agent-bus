"""Coordination overhead counters (RT-6.9 in docs/arc42/06-runtime-view.md).

What the bus adds to a session, not what the work costs: turns the plugin
caused (listener wake-ups, rounds the Stop hook forced), bus data put into the
context (tool results, the handoff after compaction), the agent's own posts,
the fixed text every turn carries (skill, instructions, tool docs), how long
turns last, and how long humans take to answer the agent's asks.

Hooks, listener and MCP server append one JSON line per event to
stats-<uid>.jsonl; at session end the MCP server posts one summary line to
MM_STATS_CHANNEL. No model is involved anywhere, and no content is recorded:
no names, no text; an ask's post id stays in the local counter file, never in
the summary.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from . import overhead, session_file

if TYPE_CHECKING:
    from .client import MattermostClient

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


def record(kind: str, **values: Any) -> None:
    """Append one event. Never raises: a counter must not disturb a session."""
    f = _path()
    if f is None or not channel():
        return
    try:
        f.parent.mkdir(parents=True, exist_ok=True)
        with f.open("a") as out:
            out.write(json.dumps({"k": kind, "t": int(time.time()), **values}) + "\n")
    except OSError:
        pass


def static_chars(skill: Path, instructions: str, tool_docs: list[str]) -> int:
    """Text in the context of every turn, bus or no bus."""
    try:
        n = len(skill.read_text())
    except OSError:
        n = 0
    return n + len(instructions or "") + sum(len(d or "") for d in tool_docs)


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
    """Asks posted, answered (waits) and still unanswered at session end (RT-6.9)."""
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
    }


def summary(version: str, static: int,
            fixed: overhead.Fixed | None = None) -> dict[str, Any] | None:
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
    if paths and fixed is not None:
        try:
            tok = overhead.measure(paths[-1], start, fixed)
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
        "static": static,
        **_asks(events, time.time()),
        **({"tok": tok} if tok else {}),
    }


def post_sync(client: MattermostClient, data: dict[str, Any], http: httpx.Client | None = None) -> str:
    """Post the summary to the stats channel, synchronously (exit handler).

    The counter file goes either way: on a server without the channel the
    counts would only pile up.
    """
    name = channel()
    if not name or not client.token:
        return "off"
    headers = {"Authorization": f"Bearer {client.token}"}
    own = http is None
    http = http or httpx.Client(timeout=5.0)
    try:
        team = http.get(f"{client.base_url}/api/v4/teams/name/{client.team_name}", headers=headers)
        team.raise_for_status()
        ch = http.get(f"{client.base_url}/api/v4/teams/{team.json()['id']}/channels/name/{name}",
                      headers=headers)
        if ch.status_code == 404:
            return f"no channel ~{name}; nothing posted"
        ch.raise_for_status()
        cid = ch.json()["id"]
        me = os.environ.get("MM_BOT_USER_ID") or _uid
        if me:  # joining twice is harmless; a non-member cannot post
            http.post(f"{client.base_url}/api/v4/channels/{cid}/members",
                      headers=headers, json={"user_id": me})
        msg = "STATS " + json.dumps(data, separators=(",", ":"))
        resp = http.post(f"{client.base_url}/api/v4/posts", headers=headers,
                         json={"channel_id": cid, "message": msg})
        resp.raise_for_status()
        return "posted"
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return f"failed: {type(exc).__name__}"
    finally:
        f = _path()
        if f is not None:
            f.unlink(missing_ok=True)
        if own:
            http.close()

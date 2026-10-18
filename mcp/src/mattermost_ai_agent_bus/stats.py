"""Coordination overhead counters (RT-6.9 in docs/arc42/06-runtime-view.md).

What the bus adds to a session, not what the work costs: turns the plugin
caused (listener wake-ups, rounds the Stop hook forced), bus data put into the
context (tool results, the handoff after compaction), the agent's own posts,
and the fixed text every turn carries (skill, instructions, tool docs).

Hooks, listener and MCP server append one JSON line per event to
stats-<uid>.jsonl; at session end the MCP server posts one summary line to
MM_STATS_CHANNEL. No model is involved anywhere, and no content is recorded.
"""

from __future__ import annotations

import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from . import session_file

if TYPE_CHECKING:
    from .client import MattermostClient

_uid = ""


def set_user(uid: str) -> None:
    global _uid
    _uid = uid


def _path() -> Path | None:
    uid = os.environ.get("MM_BOT_USER_ID") or _uid
    if not uid:
        return None
    return session_file.state_dir() / f"stats-{uid}.jsonl"


def record(kind: str, **values: Any) -> None:
    """Append one event. Never raises: a counter must not disturb a session."""
    f = _path()
    if f is None:
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


def summary(version: str, static: int) -> dict[str, Any] | None:
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
    return {
        "v": version,
        "rt": "claude-code" if os.environ.get("CLAUDECODE") else "other",
        "kind": "autonomous" if os.environ.get("MM_AGENT_KIND") == "autonomous" else "session",
        "min": max(0, round((time.time() - start) / 60)),
        "turns": kinds["turn"],
        "wakes": dict(Counter(e.get("reason", "?") for e in events if e.get("k") == "wake")),
        "blocks": dict(Counter(e.get("kind", "?") for e in events if e.get("k") == "block")),
        "tools": dict(tools),
        "posts": [len(posts), sum(int(p.get("chars", 0)) for p in posts)],
        "compact": [len(compacts), sum(int(c.get("chars", 0)) for c in compacts)],
        "static": static,
    }


def post_sync(client: MattermostClient, data: dict[str, Any], http: httpx.Client | None = None) -> str:
    """Post the summary to MM_STATS_CHANNEL, synchronously (exit handler)."""
    name = os.environ.get("MM_STATS_CHANNEL", "").strip().lstrip("~")
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
        f = _path()
        if f is not None:
            f.unlink(missing_ok=True)
        return "posted"
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        return f"failed: {type(exc).__name__}"
    finally:
        if own:
            http.close()

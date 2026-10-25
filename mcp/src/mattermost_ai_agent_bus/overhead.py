"""Token overhead of the bus in a Claude Code session, from its transcript (RT-6.9).

The transcript books every model request with its exact usage (input, cache
read, cache write, output). Two kinds of cost are the bus's:

- requests that exist only because of the bus: turns an agent's message, the
  listener or the Stop hook started, and rounds whose only input is the
  result of bus tools. Their whole usage counts, exactly.
- the bus's share of every other request: the fixed text (instructions, tool
  docs, skill) and what the bus put into the context since the last
  compaction (bus tool calls and results, loaded tool schemas, pushed
  messages, hook texts). Characters at CHARS_PER_TOKEN, as a share of the
  request's input; output only for the bus tool calls the model wrote.

A turn a trusted human starts over the bus is work, not overhead: it stands for
a prompt typed in the terminal. The totals of all requests come along, since
the transcript has them anyway. Only the main thread counts (no subagents).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import session_file

BUS = "mattermost-agent-bus"          # in the MCP tool names
CHANNEL = '<channel source="plugin:mattermost-ai-agent-bus:'
TRUSTED = 'from_trusted_human="true"'
SKILL = "mattermost-ai-agent-bus"
LISTENER = "mm-agent-listen.sh"
CHARS_PER_TOKEN = 4
KEYS = ("in", "read", "w5m", "w1h", "out")
_FUNCTION = re.compile(r"<function>.*?</function>", re.S)
_TOOL_USE_ID = re.compile(r"<tool-use-id>([^<]+)</tool-use-id>")


@dataclass
class Fixed:
    """The bus's text in the context of every request, in characters."""

    instructions: int
    tools: int       # tool docs: in every request unless the tools are deferred
    skill: int       # once the skill is loaded


def _ts(entry: dict[str, Any]) -> float:
    try:
        return datetime.fromisoformat(str(entry.get("timestamp", "")).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def _usage(u: dict[str, Any]) -> dict[str, int]:
    w1h = int((u.get("cache_creation") or {}).get("ephemeral_1h_input_tokens") or 0)
    write = int(u.get("cache_creation_input_tokens") or 0)
    return {"in": int(u.get("input_tokens") or 0), "read": int(u.get("cache_read_input_tokens") or 0),
            "w5m": max(0, write - w1h), "w1h": w1h, "out": int(u.get("output_tokens") or 0)}


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_text(c.get("text", c.get("content", ""))) if isinstance(c, dict) else str(c)
                       for c in content)
    return ""


def _ours(text: str) -> bool:
    """A Stop hook's feedback or a hook text from this plugin."""
    return "Mattermost" in text or "HELLO thread" in text or LISTENER in text


class _Count:
    def __init__(self, since: float) -> None:
        self.since = since
        self.reqs = self.bus_reqs = 0
        self.all = dict.fromkeys(KEYS, 0)
        self.bus = dict.fromkeys(KEYS, 0.0)

    def add(self, r: dict[str, Any] | None) -> None:
        """One request: all of it if the bus caused it, else the bus's share."""
        if r is None or r["ts"] < self.since or not r["usage"]:
            return
        u = _usage(r["usage"])
        self.reqs += 1
        for k in KEYS:
            self.all[k] += u[k]
        if r["plugin"]:
            self.bus_reqs += 1
            for k in KEYS:
                self.bus[k] += u[k]
            return
        inputs = u["in"] + u["read"] + u["w5m"] + u["w1h"]
        share = min(1.0, r["context"] / CHARS_PER_TOKEN / inputs) if inputs else 0.0
        for k in ("in", "read", "w5m", "w1h"):
            self.bus[k] += u[k] * share
        self.bus["out"] += min(u["out"], r["out_bus"] / CHARS_PER_TOKEN)


def measure(path: str | Path, since: float, fixed: Fixed) -> dict[str, Any] | None:
    """Overhead and totals of the requests after `since`, or None without a transcript."""
    # Never through a symlink, a FIFO or out of ~/.claude/projects (RT-6.18).
    text = session_file.read_transcript(path)
    if text is None:
        return None
    lines = text.splitlines()
    resident = 0                      # bus characters in the context since the last compaction
    deferred = skill = by_bus = False
    pending: list[bool] = []          # tool results since the last request: bus or not
    ids: dict[str, str] = {}          # tool_use id -> "bus" | "search" | "listener"
    count = _Count(since)
    req: dict[str, Any] | None = None

    def base() -> int:
        return fixed.instructions + (0 if deferred else fixed.tools) + (fixed.skill if skill else 0)

    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not isinstance(e, dict) or e.get("isSidechain"):
            continue
        kind = e.get("type")
        if kind == "system" and e.get("subtype") == "compact_boundary":
            resident, pending = 0, []
        elif kind == "attachment":
            a = e.get("attachment") or {}
            if a.get("type") == "deferred_tools_delta" and any(BUS in n for n in a.get("addedNames", [])):
                deferred = True
            elif a.get("type") == "queued_command" and str(a.get("prompt", "")).startswith(CHANNEL):
                if TRUSTED not in a["prompt"]:
                    resident += len(a["prompt"])
            elif a.get("type") == "hook_additional_context":
                for c in a.get("content") or []:
                    c = str(c)
                    if c.startswith("You are running inside corral"):
                        resident += sum(len(x) for x in c.splitlines() if "Mattermost" in x)
                    elif _ours(c):
                        resident += len(c)
        elif kind == "user":
            content = (e.get("message") or {}).get("content")
            if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result"
                                                 for c in content):
                for c in content:
                    if not isinstance(c, dict) or c.get("type") != "tool_result":
                        continue
                    what = ids.get(c.get("tool_use_id", ""))
                    pending.append(what == "bus")
                    body = _text(c.get("content"))
                    if what in ("bus", "listener"):
                        resident += len(body)
                    elif what == "search":
                        resident += sum(len(f) for f in _FUNCTION.findall(body) if BUS in f)
                continue
            origin = e.get("origin")
            origin = origin.get("kind") if isinstance(origin, dict) else origin
            text = _text(content)
            if origin == "human":
                by_bus, pending = False, []
            elif origin == "channel" or text.startswith(CHANNEL):
                by_bus, pending = TRUSTED not in text, []
                if by_bus:
                    resident += len(text)
            elif origin == "task-notification":
                m = _TOOL_USE_ID.search(text)
                by_bus, pending = bool(m and ids.get(m[1]) == "listener"), []
                if by_bus:
                    resident += len(text)
            elif text.startswith("Stop hook feedback") and _ours(text):
                by_bus, pending = True, []
                resident += len(text)
        elif kind == "assistant":
            msg = e.get("message") or {}
            rid = e.get("requestId") or msg.get("id") or ""
            if req is None or req["id"] != rid:
                count.add(req)
                req = {"id": rid, "ts": _ts(e), "usage": msg.get("usage"), "out_bus": 0,
                       "plugin": by_bus or (bool(pending) and all(pending)),
                       "context": base() + resident}
                pending = []
            for c in msg.get("content") or []:
                if not isinstance(c, dict) or c.get("type") != "tool_use":
                    continue
                name, inp = c.get("name", ""), c.get("input") or {}
                if BUS in name:
                    ids[c.get("id", "")] = "bus"
                elif name == "Bash" and LISTENER in str(inp.get("command", "")):
                    ids[c.get("id", "")] = "listener"
                else:
                    if name == "ToolSearch":
                        ids[c.get("id", "")] = "search"
                    elif name == "Skill" and SKILL in str(inp.get("skill", "")):
                        skill = True
                    continue
                size = len(json.dumps(inp, ensure_ascii=False))
                req["out_bus"] += size
                resident += size
    count.add(req)
    if not count.reqs:
        return None
    return {"req": count.reqs, "bus_req": count.bus_reqs, "fixed": round(base() / CHARS_PER_TOKEN),
            "all": count.all, "bus": {k: round(v) for k, v in count.bus.items()}}

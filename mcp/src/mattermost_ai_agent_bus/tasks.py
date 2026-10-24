"""The work channel as tasks and agents, read from its posts (RT-6.14).

Pure functions over raw Mattermost posts: the coordinator gets the state of
the work in a few hundred characters instead of reading the channel, and
bystanders get other agents' status posts cut short. Nothing is stored; the
posts (and their props, a2a.py) are the record.
"""

from __future__ import annotations

import re
import time
from collections.abc import Mapping
from typing import Any

from . import a2a, asks, session_file

WINDOW_DAYS = 14
DONE_KEEP_MS = 24 * 3600 * 1000
TITLE = 90
LINE = 160
# Other agents' status posts: a bystander needs their first line at most.
COMPACT = frozenset({"IDLE", "BYE", "ACK", "PROGRESS", "ASSIGNMENTS"})
# What the assignee may say about its task; the task's author and the
# coordinator only close it.
_ASSIGNEE = frozenset({"ACK", "PROGRESS", "BLOCKED", "WAITING", "RESULT", "DONE", "NO"})
_CLOSERS = frozenset({"DONE", "CANCEL"})
_TAKES = frozenset({"ACK", "PROGRESS", "RESULT"})

_PR = re.compile(r"(?:\bPR\s*#|\bMR\s*!|/pull/|/merge_requests/)(\d+)", re.I)
_ISSUE = re.compile(r"(?<![\w/&])#(\d+)\b")
_SPEC = re.compile(r"\bRT-\d+\.\d+\b|\b[A-Z][A-Z0-9]+-\d+\b(?!\.\d)")
_BRANCH = re.compile(r"\bbranch(?:/PR)?:?\s+([\w.]*[-/][\w./-]*\w)")
_LEAD = re.compile(r"^[^\w@#]*(TASK|DELEGATE)\b[\s·:–-]*")


def _mentions(message: str) -> list[str]:
    """Agents a post @-mentions: names with the registrar's prefix (arc42 §7.1)."""
    return re.findall(rf"@({re.escape(session_file.agent_prefix())}[\w.-]*\w)", message)


def since_ms(now: float | None = None) -> int:
    return int(((now or time.time()) - WINDOW_DAYS * 86400) * 1000)


def _when(ms: int) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000)) if ms else ""


def _first_line(message: str, limit: int) -> str:
    lines = (message or "").strip().splitlines()
    line = " ".join(lines[0].split()) if lines else ""
    if len(line) > limit or len(lines) > 1:
        return line[: limit - 1].rstrip() + "…"
    return line


def compact(message: str) -> str:
    """A bystander's view of a post: other agents' status posts cut to their
    first line (LINE characters), everything else as it is."""
    if a2a.word(message) in COMPACT:
        return _first_line(message, LINE)
    return message


def title(message: str) -> str:
    first = (message or "").strip().splitlines()[0] if (message or "").strip() else ""
    return _first_line(_LEAD.sub("", first), TITLE)


def _live(posts: list[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Ordinary posts, oldest first: no deleted or system posts."""
    return sorted((p for p in posts if not p.get("delete_at") and not p.get("type")),
                  key=lambda p: p.get("create_at", 0))


def build(
    posts: list[Mapping[str, Any]],
    names: Mapping[str, str],
    *,
    now_ms: int | None = None,
) -> list[dict[str, Any]]:
    """The tasks of the work channel: top-level TASK / DELEGATE posts and
    their threads, newest change last. Only the task's own thread moves it:
    an agent's HELLO thread speaks of whatever it does next, too. Rows leave
    out what is unknown."""
    now_ms = now_ms or int(time.time() * 1000)
    order = _live(posts)

    def who(p: Mapping[str, Any]) -> str:
        uid = p.get("user_id", "")
        return names.get(uid) or uid

    tasks: dict[str, dict[str, Any]] = {}
    last_assignments = 0
    for p in order:
        kind = a2a.kind_of(p)
        if p.get("root_id"):
            continue
        if kind == "ASSIGNMENTS":
            last_assignments = p.get("create_at", 0)
        elif kind in ("TASK", "DELEGATE"):
            msg = str(p.get("message", ""))
            refs = [*_SPEC.findall(msg), *(f"#{n}" for n in _ISSUE.findall(msg))]
            tasks[p["id"]] = {
                "task": p["id"], "title": title(msg), "by": who(p), "agent": None,
                "status": "open", "spec": ", ".join(dict.fromkeys(refs).keys()) or None,
                "pr": None, "branch": None, "updated": p.get("create_at", 0),
                "_candidates": [m for m in _mentions(msg) if m != who(p)],
            }
    for p in order:
        if p.get("root_id") in tasks:
            _apply(tasks[p["root_id"]], p, who(p))

    out = []
    for t in tasks.values():
        if t["status"] == "canceled":
            continue
        if t["status"] == "done" and (t["updated"] < last_assignments
                                      or now_ms - t["updated"] > DONE_KEEP_MS):
            continue
        row = {k: v for k, v in t.items() if not k.startswith("_") and k != "updated" and v}
        row["state"] = _STATE[t["status"]]
        row["at"] = _when(t["updated"])
        out.append(row)
    return sorted(out, key=lambda r: tasks[r["task"]]["updated"])


# Bus status → A2A TaskState (a2a.WORDS, read the other way round).
_STATE = {"open": "submitted", "active": "working", "blocked": "input-required",
          "review": "input-required", "done": "completed"}


def _apply(t: dict[str, Any], p: Mapping[str, Any], author: str) -> None:
    msg = str(p.get("message", ""))
    kind = a2a.kind_of(p) or ""
    crowned = msg.lstrip().startswith("👑")
    if t["agent"] is None and kind in _TAKES and author != t["by"]:
        if not t["_candidates"] or author in t["_candidates"]:
            t["agent"] = author
    if author == t["agent"]:
        if kind == "NO":
            t["agent"], t["status"] = None, "open"
            t["_candidates"] = [c for c in t["_candidates"] if c != author]
        elif kind in _ASSIGNEE:
            t["status"] = a2a.status(kind)
        pr = _PR.findall(msg)
        if pr:
            t["pr"] = f"#{pr[-1]}"
        branch = _BRANCH.findall(msg)
        if branch:
            t["branch"] = branch[-1]
    elif kind == "NO" and author in t["_candidates"]:
        t["_candidates"] = [c for c in t["_candidates"] if c != author]
    elif kind in _CLOSERS and (author == t["by"] or crowned):
        t["status"] = a2a.status(kind)
    else:
        return
    t["updated"] = p.get("create_at", t["updated"])


def open_asks(posts: list[Mapping[str, Any]], names: Mapping[str, str]) -> list[dict[str, Any]]:
    """Asks to humans still waiting: no yes/no reaction and no reply from the
    human asked (RT-6.11)."""
    order = _live(posts)
    replied = {(p.get("root_id"), names.get(p.get("user_id", ""), "")) for p in order}
    out = []
    for p in order:
        if a2a.kind_of(p) != a2a.ASK:
            continue
        msg = str(p.get("message", ""))
        human = re.search(r"🙋\s*@([\w.-]*\w)", msg)
        need = re.search(r"\*\*(.+?)\*\*", msg)
        reactions = (p.get("metadata") or {}).get("reactions") or []
        if any(asks.answer_of(r.get("emoji_name", "")) for r in reactions):
            continue
        if human and (p["id"], human[1]) in replied:
            continue
        row = {"ask": p["id"], "by": names.get(p.get("user_id", ""), ""),
               "human": human[1] if human else None,
               "need": _first_line(need[1] if need else msg, TITLE),
               "at": _when(p.get("create_at", 0))}
        out.append({k: v for k, v in row.items() if v})
    return out


_STATUS = {"hammer_and_wrench": "working", "ear": "listening", "zzz": "idle",
           "satellite_antenna": "bus"}


def roster(rows: list[Mapping[str, Any]], posts: list[Mapping[str, Any]],
           names: Mapping[str, str]) -> list[dict[str, Any]]:
    """One row per reachable agent: its lease status (mm-agent-roster.sh) and
    its newest card (HELLO props, else the HELLO text)."""
    cards: dict[str, dict[str, Any]] = {}
    for p in _live(posts):
        if p.get("root_id"):
            continue
        name = names.get(p.get("user_id", ""), "")
        card = a2a.card_of(p, name)
        if card:
            cards[card.get("name") or name] = card
    out = []
    for r in rows:
        text = [w.strip() for w in str(r.get("text", "")).split(" · ")]
        if text and text[0] == "coordinator":
            text = text[1:]
        status = text[0] if text and text[0] else _STATUS.get(str(r.get("emoji", "")), "")
        card = cards.get(str(r.get("username", ""))) or {}
        meta = card.get("meta") or {}
        row = {"agent": r.get("username"), "status": status,
               "coordinator": bool(r.get("coordinator")) or None,
               "model": meta.get("model"), "role": meta.get("role"),
               "task": _first_line(str(card.get("description", "")), TITLE),
               "branch": meta.get("branch"),
               "skills": [s.get("name") for s in card.get("skills") or []] or None}
        out.append({k: v for k, v in row.items() if v})
    return out


def table(rows: list[Mapping[str, Any]]) -> str:
    """The ASSIGNMENTS table (coordinator.md). Names without @: a table is no
    request, and a mention would wake every agent in it."""
    lines = ["| Agent | Task | Status | Spec / issue | Branch / PR |", "|---|---|---|---|---|"]
    for r in rows:
        cells = [r.get("agent") or "–", r.get("title") or "–", r.get("status", "open"),
                 r.get("spec") or "–", r.get("pr") or r.get("branch") or "–"]
        lines.append("| " + " | ".join(str(c).replace("|", "/") for c in cells) + " |")
    if not rows:
        lines.append("| – | no open tasks | – | – | – |")
    return "\n".join(lines)

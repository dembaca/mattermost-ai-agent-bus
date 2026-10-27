"""The work channel as tasks and agents, read from its posts (RT-6.14).

Pure functions over raw Mattermost posts: the coordinator gets the state of
the work in a few hundred characters instead of reading the channel, and
bystanders get other agents' status posts cut short. Nothing is stored; the
posts (and their props, a2a.py) are the record.
"""

from __future__ import annotations

import re
import time
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from . import a2a, asks, session_file
from .model import RosterEntry

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
# The words that claim a task (RT-6.14 step 3).
TAKES = frozenset({"ACK", "PROGRESS", "RESULT"})

_ISSUE = re.compile(r"(?<![\w/&])#(\d+)\b")
_SPEC = re.compile(r"\bRT-\d+\.\d+\b|\b[A-Z][A-Z0-9]+-\d+\b(?!\.\d)")
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


def _live(posts: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Ordinary posts, oldest first: no deleted or system posts."""
    return sorted((p for p in posts if not p.get("delete_at") and not p.get("type")),
                  key=lambda p: p.get("create_at", 0))


def build(
    posts: Sequence[Mapping[str, Any]],
    names: Mapping[str, str],
    *,
    now_ms: int | None = None,
    untrusted: Collection[str] = (),
    coordinators: Collection[str] = (),
) -> list[dict[str, Any]]:
    """The tasks of the work channel: top-level TASK / DELEGATE posts and
    their threads, newest change last. Only the task's own thread moves it:
    an agent's HELLO thread speaks of whatever it does next, too. Rows leave
    out what is unknown. A root in `untrusted` (post ids by someone the
    agent does not trust, RT-6.16) is no task. `coordinators`: usernames of
    the crown holder now (lease.roster), who may close any task; an agent's
    close written while it held the crown (props.a2a.crown) counts too."""
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
        elif kind in ("TASK", "DELEGATE") and p.get("id") not in untrusted:
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
            _apply(tasks[p["root_id"]], p, who(p), coordinators)

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


def _crown(p: Mapping[str, Any], author: str, coordinators: Collection[str]) -> bool:
    """A close by someone other than the task's author counts when the author
    holds the crown now, or is an agent that held it when it posted: the server
    wrote props.a2a.crown (RT-6.14 step 3). A 👑 in the text is no authority."""
    return author in coordinators or (
        a2a.crowned(p) and author.startswith(session_file.agent_prefix()))


def _apply(t: dict[str, Any], p: Mapping[str, Any], author: str,
           coordinators: Collection[str] = ()) -> None:
    kind = a2a.kind_of(p) or ""
    if t["agent"] is None and kind in TAKES and author != t["by"]:
        if not t["_candidates"] or author in t["_candidates"]:
            t["agent"] = author
    if author == t["agent"]:
        if kind == "NO":
            t["agent"], t["status"] = None, "open"
            t["_candidates"] = [c for c in t["_candidates"] if c != author]
        elif kind in _ASSIGNEE:
            t["status"] = a2a.status(kind)
        pr = a2a.pr_of(p)
        if pr is not None:
            t["pr"] = f"#{pr}"
        branch = a2a.branch_of(p)
        if branch:
            t["branch"] = branch
    elif kind == "NO" and author in t["_candidates"]:
        t["_candidates"] = [c for c in t["_candidates"] if c != author]
    elif kind in _CLOSERS and (author == t["by"] or _crown(p, author, coordinators)):
        t["status"] = a2a.status(kind)
    else:
        return
    t["updated"] = p.get("create_at", t["updated"])


def ask_answered(ask: Mapping[str, Any], thread: Collection[Mapping[str, Any]],
                 reactions: Collection[Mapping[str, Any]], names: Mapping[str, str],
                 human: str, trusted: Collection[str] = ()) -> bool:
    """Whether an ask to `human` is answered on Mattermost (RT-6.11, RT-6.19): a
    yes/no reaction by that human or a trusted human (ids in `trusted`), or a
    reply by that human in the ask's thread after it. A reaction by an agent or
    anyone else changes nothing. `names` maps user ids, the reacting accounts
    included, to usernames."""
    human = human.lower()

    def by_human(uid: str) -> bool:
        return bool(human) and names.get(uid, "").lower() == human

    for r in reactions:
        u = str(r.get("user_id") or "")
        if asks.answer_of(str(r.get("emoji_name") or "")) and (by_human(u) or u in trusted):
            return True
    root, at = str(ask.get("root_id") or ask.get("id")), int(ask.get("create_at") or 0)
    return any(str(p.get("root_id") or p.get("id")) == root and int(p.get("create_at") or 0) > at
               and not p.get("delete_at") and by_human(str(p.get("user_id") or "")) for p in thread)


def resolved(reactions: Collection[Mapping[str, Any]], names: Mapping[str, str]) -> bool:
    """An agent closed the ask with resolve_ask: its ✅ (RT-6.11). Any bus agent
    may, also one that did not ask: a successor session closes its
    predecessor's asks. It answers nothing: no approval comes of it."""
    prefix = session_file.agent_prefix()
    return any(r.get("emoji_name") == "white_check_mark"
               and names.get(str(r.get("user_id") or ""), "").startswith(prefix) for r in reactions)


def open_asks(posts: Sequence[Mapping[str, Any]], names: Mapping[str, str],
              trusted: Collection[str] = ()) -> list[dict[str, Any]]:
    """Asks to humans still waiting: not answered by the human asked or a
    trusted human (ask_answered), and not closed by an agent (resolved)."""
    order = _live(posts)
    out = []
    for p in order:
        if a2a.kind_of(p) != a2a.ASK:
            continue
        msg = str(p.get("message", ""))
        human = re.search(r"🙋\s*@([\w.-]*\w)", msg)
        need = re.search(r"\*\*(.+?)\*\*", msg)
        reactions = (p.get("metadata") or {}).get("reactions") or []
        if (ask_answered(p, order, reactions, names, human[1] if human else "", trusted)
                or resolved(reactions, names)):
            continue
        row = {"ask": p["id"], "by": names.get(p.get("user_id", ""), ""),
               "human": human[1] if human else None,
               "need": _first_line(need[1] if need else msg, TITLE),
               "at": _when(p.get("create_at", 0))}
        out.append({k: v for k, v in row.items() if v})
    return out


def roster(rows: list[RosterEntry], posts: Sequence[Mapping[str, Any]],
           names: Mapping[str, str]) -> list[dict[str, Any]]:
    """One row per reachable agent: its lease status (lease.roster, RT-6.6) and
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
        card = cards.get(r.username) or {}
        meta = card.get("meta") or {}
        row = {"agent": r.username, "status": r.status,
               "coordinator": r.coordinator or None,
               "model": meta.get("model"), "role": meta.get("role"),
               "task": _first_line(str(card.get("description", "")), TITLE),
               "branch": meta.get("branch"),
               "skills": [s.get("name") for s in card.get("skills") or []] or None}
        out.append({k: v for k, v in row.items() if v})
    return out


def table(rows: Sequence[Mapping[str, Any]]) -> str:
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

"""A2A data shapes on the bus: agent cards and task states (RT-6.14).

The bus takes A2A 1.0's shapes, not its transport (as Cotal does): an agent's
identity is a subset of an A2A AgentCard, a task's state an A2A TaskState.
Both live in Mattermost post props, which only the MCP server writes and
reads; slim posts never carry them, so no model reads them unasked.

    props.agent_card  on the HELLO post (announce)
    props.a2a         {"v": 1, "kind": <word>?, "state": <TaskState>?, "to": "all"?}

Ids are Mattermost's: messageId = post id, contextId = thread root,
taskId = the root of a TASK thread. Posts without props (older versions,
people, hand-written) are read from their text with the same rules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

V = 1

# The bus's convention words (protocol.md) → (bus status, A2A TaskState).
# Status is what ASSIGNMENTS shows; None: the word moves no task.
WORDS: dict[str, tuple[str | None, str | None]] = {
    "TASK": ("open", "submitted"),
    "DELEGATE": ("open", "submitted"),
    "ACK": ("active", "working"),
    "PROGRESS": ("active", "working"),
    "BLOCKED": ("blocked", "input-required"),
    "WAITING": ("blocked", "input-required"),
    "RESULT": ("review", "input-required"),
    "DONE": ("done", "completed"),
    "NO": ("open", "rejected"),
    "CANCEL": ("canceled", "canceled"),
    "HELLO": (None, None),
    "IDLE": (None, None),
    "BYE": (None, None),
    "HANDOVER": (None, None),
    "ASSIGNMENTS": (None, None),
}
# Kinds the server sets itself, not read from a word.
ASK = "ask"
PERMISSION = "permission"
# props.a2a.to: the post is for every agent in the channel (RT-6.11, RT-6.2).
ALL = "all"

# The first word after an optional 👑, emoji or markdown; upper case only, so
# "No worries" is no NO. `PR #57` (or a PR link) is a RESULT.
_FIRST = re.compile(r"^[^\w@#]*([A-Z]{2,11})(?![A-Za-z])")
_PR_FIRST = re.compile(r"^[^\w@#]*PR\s*#\d+", re.I)


def word(message: str) -> str | None:
    """The convention word a message starts with, or None."""
    text = message or ""
    if _PR_FIRST.match(text):
        return "RESULT"
    m = _FIRST.match(text)
    return m[1] if m and m[1] in WORDS else None


def status(kind: str | None) -> str | None:
    return WORDS.get((kind or "").upper(), (None, None))[0]


def state(kind: str | None) -> str | None:
    if kind == ASK:
        return "input-required"
    if kind == PERMISSION:
        return "auth-required"
    return WORDS.get((kind or "").upper(), (None, None))[1]


def annotate(message: str, to: str = "") -> dict[str, Any] | None:
    """props.a2a for a post the server writes, or None for a plain message."""
    w = word(message)
    if w is None and not to:
        return None
    out: dict[str, Any] = {"v": V}
    if w is not None:
        out["kind"] = w.lower()
        if state(w):
            out["state"] = state(w)
    if to:
        out["to"] = to
    return out


def to_all(post: Mapping[str, Any]) -> bool:
    """The post says it is for every agent. Anyone can write props through the
    API, so this tells who it is for, never who wrote it (gate.py)."""
    props = post.get("props")
    a2a = props.get("a2a") if isinstance(props, Mapping) else None
    return isinstance(a2a, Mapping) and a2a.get("to") == ALL


def kind_of(post: Mapping[str, Any]) -> str | None:
    """A post's kind: from its props, else from its text (upper case)."""
    a2a = (post.get("props") or {}).get("a2a")
    if isinstance(a2a, Mapping) and a2a.get("kind"):
        k = str(a2a["kind"])
        return k if k in (ASK, PERMISSION) else k.upper()
    message = str(post.get("message", ""))
    if message.lstrip().removeprefix("👑").lstrip().startswith("🙋"):
        return ASK
    return word(message)


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "skill"


def skills(capabilities: str) -> list[dict[str, str]]:
    """AgentSkill subset ({id, name}) from announce's capabilities (names only)."""
    names = [c.strip() for c in re.split(r"[,;]", capabilities or "") if c.strip()]
    return [{"id": _slug(n), "name": n} for n in names if n not in ("–", "-")]


def card(
    *,
    name: str,
    task: str,
    version: str,
    capabilities: str = "",
    project: str = "",
    branch: str = "",
    model: str = "",
    role: str = "member",
    kind: str = "session",
    responsible: str | None = None,
    trusted: list[str] | None = None,
    push: bool = False,
    relay: bool = False,
) -> dict[str, Any]:
    """The agent's card: A2A field names where A2A has them, the rest in meta."""
    caps: dict[str, Any] = {"pushNotifications": push}
    if relay:
        caps["extensions"] = [{"uri": "urn:mm-agent-bus:permission-relay"}]
    meta = {"project": project, "branch": branch, "model": model, "role": role,
            "kind": kind, "for": responsible or "", "trusts": trusted or []}
    return {"v": V, "name": name, "description": task, "version": version,
            "skills": skills(capabilities), "capabilities": caps,
            "meta": {k: v for k, v in meta.items() if v}}


_HELLO_FIELDS = re.compile(r"^(task|model|capabilities|role):\s*(.*)$")
_HELLO_PAIR = re.compile(r"^(project|for):\s*(.*?)\s+·\s+(branch/PR|trusts):\s*(.*)$")


def card_of(post: Mapping[str, Any], name: str = "") -> dict[str, Any] | None:
    """A HELLO post's card: props.agent_card, else read from the HELLO text
    (the format announce writes, protocol.md); None for any other post."""
    stored = (post.get("props") or {}).get("agent_card")
    if isinstance(stored, Mapping) and stored.get("name"):
        return dict(stored)
    message = str(post.get("message", ""))
    if word(message) != "HELLO":
        return None
    fields: dict[str, str] = {}
    for line in message.splitlines()[1:]:
        # Two lines hold two fields each; values may contain " · " themselves.
        pair = _HELLO_PAIR.match(line.strip())
        if pair:
            fields[pair[1]], fields[pair[3]] = pair[2].strip(), pair[4].strip()
            continue
        m = _HELLO_FIELDS.match(line.strip())
        if m:
            fields[m[1]] = m[2].strip()
    first = message.splitlines()[0] if message else ""
    m = re.search(r"@([\w.-]+)", first)
    found = card(
        name=name or (m[1] if m else ""), task=fields.get("task", ""), version="",
        capabilities=fields.get("capabilities", ""), project=fields.get("project", ""),
        branch="" if fields.get("branch/PR") in (None, "–") else fields["branch/PR"],
        model=fields.get("model", ""), role=fields.get("role", "member"),
        responsible=(fields.get("for") or "").split(" (")[0].strip("– ") or None,
        trusted=[t.strip() for t in fields.get("trusts", "").split(",") if t.strip("– ")],
    )
    found.pop("version", None)
    return found

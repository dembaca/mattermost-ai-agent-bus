"""A2A data shapes on the bus: agent cards and task states (RT-6.14).

The bus takes A2A 1.0's shapes, not its transport (as Cotal does): an agent's
identity is a subset of an A2A AgentCard, a task's state an A2A TaskState.
Both live in Mattermost post props, which only the MCP server writes and
reads; slim posts never carry them, so no model reads them unasked.

    props.agent_card  on the HELLO post (announce)
    props.a2a         {"v": 1, "kind": <word>?, "state": <TaskState>?,
                       "intent": "action" | "reply" | "fyi"?, "to": "all"?,
                       "pr": <number>?, "branch": <name>?, "crown": true?}

Ids are Mattermost's: messageId = post id, contextId = thread root,
taskId = the root of a TASK thread. Posts without props (older versions,
people, hand-written) are read from their text with the same rules.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .model import Post

V = 1

# What a post asks of its readers (RT-6.14): to do something, to answer, or
# nothing. An fyi post wakes only whom it names and the thread's starter.
ACTION, REPLY, FYI = "action", "reply", "fyi"

# The bus's convention words (protocol.md) → (bus status, A2A TaskState, intent).
# Status is what ASSIGNMENTS shows; None: the word moves no task.
WORDS: dict[str, tuple[str | None, str | None, str]] = {
    "TASK": ("open", "submitted", ACTION),
    "DELEGATE": ("open", "submitted", ACTION),
    "ACK": ("active", "working", FYI),
    "PROGRESS": ("active", "working", FYI),
    "BLOCKED": ("blocked", "input-required", REPLY),
    "WAITING": ("blocked", "input-required", FYI),
    "RESULT": ("review", "input-required", FYI),
    "DONE": ("done", "completed", FYI),
    "NO": ("open", "rejected", REPLY),
    "CANCEL": ("canceled", "canceled", ACTION),
    "HELLO": (None, None, FYI),
    "IDLE": (None, None, FYI),
    "BYE": (None, None, FYI),
    "HANDOVER": (None, None, ACTION),
    "ASSIGNMENTS": (None, None, FYI),
}
_NONE = (None, None, None)
# Kinds the server sets itself, not read from a word; both ask for an answer.
ASK = "ask"
PERMISSION = "permission"
# The server's reminder of an open ask (RT-6.19): fyi for agents, no activity.
REMINDER = "reminder"
_OWN = (ASK, PERMISSION, REMINDER)
# props.a2a.to: the post is for every agent in the channel (RT-6.11, RT-6.2).
ALL = "all"

# The first word after an optional 👑, emoji or markdown; upper case only, so
# "No worries" is no NO. `PR #57` (or a PR link) is a RESULT.
# What a post names, read at write time into props.a2a and from the text of
# posts without (RT-6.14 step 3): its PR (PR #n, MR !n, a pull or merge
# request URL; the last one) and its branch.
_PR = re.compile(r"(?:\bPR\s*#|\bMR\s*!|/pull/|/merge_requests/)(\d+)", re.I)
_BRANCH = re.compile(r"\bbranch(?:/PR)?:?\s+([\w.]*[-/][\w./-]*\w)")
_FIRST = re.compile(r"^[^\w@#]*([A-Z]{2,11})(?![A-Za-z])")
_PR_FIRST = re.compile(r"^[^\w@#]*PR\s*#\d+", re.I)


def word(message: str) -> str | None:
    """The convention word a message starts with, or None."""
    text = message or ""
    if _PR_FIRST.match(text):
        return "RESULT"
    m = _FIRST.match(text)
    return m[1] if m and m[1] in WORDS else None


def lead(message: str, kind: str = "") -> str:
    """The message with `kind` as its first word (RT-6.14): props come from the
    text, so both say the same. A text that starts with it stays as it is;
    an unknown kind, or a text that starts with another word, is refused."""
    k = kind.strip().upper()
    if not k or word(message) == k:
        return message
    if k not in WORDS:
        raise ValueError(f"kind must be one of {', '.join(WORDS)}")
    m = _FIRST.match(message or "")
    if m and m[1] in WORDS:
        raise ValueError(f"kind is {k} but the message starts with {m[1]}: drop one")
    return f"{k} · {message.lstrip()}" if message.strip() else k


def status(kind: str | None) -> str | None:
    return WORDS.get((kind or "").upper(), _NONE)[0]


def state(kind: str | None) -> str | None:
    if kind == ASK:
        return "input-required"
    if kind == PERMISSION:
        return "auth-required"
    return WORDS.get((kind or "").upper(), _NONE)[1]


def intent(kind: str | None) -> str | None:
    if kind in (ASK, PERMISSION):
        return REPLY
    if kind == REMINDER:
        return FYI
    return WORDS.get((kind or "").upper(), _NONE)[2]


def pr(message: str) -> int | None:
    """The PR a text names, the last one (RT-6.14 step 3)."""
    found = _PR.findall(message or "")
    return int(found[-1]) if found else None


def branch(message: str) -> str | None:
    """The branch a text names, the last one (RT-6.14 step 3)."""
    found = _BRANCH.findall(message or "")
    return str(found[-1]) if found else None


def annotate(message: str = "", to: str = "", kind: str = "",
             crown: bool = False) -> dict[str, Any] | None:
    """props.a2a for a post the server writes, or None for a plain message.
    kind: one the server sets itself (ask, permission, reminder); else the first word.
    pr, branch: what the text names. crown: the author held the crown when it
    posted (RT-6.14 step 3)."""
    w = kind or word(message)
    named = {"pr": pr(message), "branch": branch(message), "crown": crown or None}
    if w is None and not to and not any(named.values()):
        return None
    out: dict[str, Any] = {"v": V}
    if w is not None:
        out["kind"] = w if w in _OWN else w.lower()
        for key, value in (("state", state(w)), ("intent", intent(w))):
            if value:
                out[key] = value
    if to:
        out["to"] = to
    out.update((k, v) for k, v in named.items() if v)
    return out


def props(message: str = "", to: str = "", kind: str = "",
          card: dict[str, Any] | None = None, crown: bool = False) -> dict[str, Any] | None:
    """The props of a post the server writes: the agent's card on its HELLO,
    props.a2a (annotate); None for a plain message."""
    out: dict[str, Any] = {"agent_card": card} if card else {}
    note = annotate(message, to, kind, crown)
    if note:
        out["a2a"] = note
    return out or None


def intent_of(post: Mapping[str, Any]) -> str | None:
    """A post's intent, from its props only: older posts and people's have
    none. Anyone can write props through the API, so callers honour it only
    for an agent's post (server.py), and never for the wake gate (gate.py)."""
    props = post.get("props")
    a2a = props.get("a2a") if isinstance(props, Mapping) else None
    value = a2a.get("intent") if isinstance(a2a, Mapping) else None
    return value if value in (ACTION, REPLY, FYI) else None


def to_all(post: Mapping[str, Any]) -> bool:
    """The post says it is for every agent. Anyone can write props through the
    API, so this tells who it is for, never who wrote it (gate.py)."""
    props = post.get("props")
    a2a = props.get("a2a") if isinstance(props, Mapping) else None
    return isinstance(a2a, Mapping) and a2a.get("to") == ALL


def _note(post: Mapping[str, Any]) -> Mapping[str, Any]:
    props = post.get("props")
    a2a = props.get("a2a") if isinstance(props, Mapping) else None
    return a2a if isinstance(a2a, Mapping) else {}


def pr_of(post: Mapping[str, Any]) -> int | None:
    """A post's PR: from its props, else from its text (old posts, people's)."""
    value = _note(post).get("pr")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return pr(str(post.get("message", "")))


def branch_of(post: Mapping[str, Any]) -> str | None:
    """A post's branch: from its props, else from its text."""
    value = _note(post).get("branch")
    return value if isinstance(value, str) and value else branch(str(post.get("message", "")))


def crowned(post: Mapping[str, Any]) -> bool:
    """The server wrote the post while its author held the crown. Anyone can
    write props through the API, so callers honour it only for an agent's post
    (tasks.py); a 👑 in the text counts for nothing."""
    return _note(post).get("crown") is True


def kind_of(post: Mapping[str, Any]) -> str | None:
    """A post's kind: from its props, else from its text (upper case)."""
    a2a = (post.get("props") or {}).get("a2a")
    if isinstance(a2a, Mapping) and a2a.get("kind"):
        k = str(a2a["kind"])
        return k if k in _OWN else k.upper()
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


def card_of(post: Post, name: str = "") -> dict[str, Any] | None:
    """A HELLO post's card: props.agent_card, else read from the HELLO text
    (the format announce writes, protocol.md); None for any other post."""
    stored = post.props.get("agent_card")
    if isinstance(stored, Mapping) and stored.get("name"):
        return dict(stored)
    message = post.message
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

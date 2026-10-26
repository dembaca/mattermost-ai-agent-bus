"""Claude Code permission prompts relayed to the responsible human (RT-6.13).

Research preview (code.claude.com/docs/en/channels-reference, "Relay permission
prompts"). A channel that declares `claude/channel/permission` gets each
tool-use approval prompt as `notifications/claude/channel/permission_request`
with a five-letter request id, while the terminal dialog stays open. A verdict
`notifications/claude/channel/permission` {request_id, behavior: allow|deny}
answers it; whichever answer comes first wins, a verdict for an id that is no
longer open is dropped by Claude Code without a word.

The prompt goes by DM to the responsible human, and Mattermost keeps every post
(retention, search, admin access, backups). So the preview carries what a
decision needs and no file content: a Bash command in full, for any other tool
paths and short values, file content and long text only as their size. Claude
Code masks well-known secrets before it sends the preview.
A verdict counts only from a trusted human (server-verified author, RT-6.8):
`yes <id>` / `no <id>`, or 👍/👎 on the prompt post.

Claude Code says nothing when the terminal answers first. An open dialog lets
no turn end, so the turn's end (the Stop hook's presence idle or listening)
closes every prompt still open, with a note in the DM.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import asks

CAPABILITY = "claude/channel/permission"
REQUEST = "notifications/claude/channel/permission_request"
VERDICT = "notifications/claude/channel/permission"
# Claude Code's ids: five letters a-z without l. /i for phone autocorrect.
REPLY = re.compile(r"^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$", re.I)
# A verdict counts as long as a yes on an ask (RT-6.11); and Claude Code says
# nothing when the terminal answered first: forget then.
MAX_AGE_SEC = float(asks.APPROVAL_SEC)
# A post holds 16383 characters; Claude Code caps each field at 3,500.
PREVIEW_CHARS = 12000
# Tools whose input carries file content: the DM names the path and sizes only.
CONTENT_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})
CONTENT_KEYS = frozenset({"content", "old_string", "new_string", "new_source", "edits"})
PATH_KEYS = frozenset({"file_path", "notebook_path", "path"})
# Any other tool's string longer than this, or spanning lines, shows as its size.
SHORT_CHARS = 200
# Claude Code relays a longer value as its start and end around this marker.
ELIDED = re.compile(r"\s*⋯ (\d+) code points elided ⋯\s*")


def relayed() -> bool:
    """Someone can answer: the same variables trusted_humans() reads (RT-6.8)."""
    return any(os.environ.get(v, "").strip() for v in (
        "MM_TRUSTED_HUMAN_USER_IDS", "MM_TRUSTED_HUMANS", "MM_OWNER_USER_ID", "MM_OWNER"))


def parse(message: str) -> tuple[str, str] | None:
    """`yes abcde` → ("allow", "abcde"); None for anything else."""
    m = REPLY.match(message or "")
    if not m:
        return None
    return ("allow" if m[1].lower().startswith("y") else "deny"), m[2].lower()


def _fenced(text: str) -> str:
    """Untrusted text as a code block no backtick run inside can close."""
    runs = [len(r) for r in re.findall(r"`+", text)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    return f"{fence}\n{text}\n{fence}"


def _chars(text: str) -> int:
    """The length of the value Claude Code relayed, elided part included."""
    return len(ELIDED.sub("", text)) + sum(int(n) for n in ELIDED.findall(text))


def _size(text: str) -> str:
    """A value as its size. Claude Code folds line breaks to spaces (older
    versions did not), so lines are counted only where they are left."""
    lines = len(text.splitlines())
    return f"‹{_chars(text)} chars, {lines} lines›" if lines > 1 else f"‹{_chars(text)} chars›"


def _shown(tool: str, key: str, value: Any) -> Any:
    """One argument as the DM shows it: file content and long text as their size."""
    if isinstance(value, dict):
        return {k: _shown(tool, k, v) for k, v in value.items()}
    if isinstance(value, list):
        return [_shown(tool, key, v) for v in value]
    if not isinstance(value, str) or key in PATH_KEYS or (tool == "Bash" and key == "command"):
        return value
    if (tool in CONTENT_TOOLS and key in CONTENT_KEYS) or _chars(value) > SHORT_CHARS or "\n" in value:
        return _size(value)
    return value


def preview(tool: str, text: str) -> str:
    """input_preview without file content (RT-6.13): Bash's command in full, no
    decision without it; the rest as _shown. Claude Code sends JSON-shaped text,
    cut per top-level field; what does not parse shows only for Bash, or short."""
    try:
        args = json.loads(text)
    except ValueError:
        args = None
    if isinstance(args, dict):
        return json.dumps(_shown(tool, "", args), ensure_ascii=False)
    if tool == "Bash" or (tool not in CONTENT_TOOLS and _chars(text) <= SHORT_CHARS):
        return text
    paths = re.findall(r'"(file_path|notebook_path|path)"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    return " ".join([*(f"{k}: {v}" for k, v in paths), f"‹input not shown: {_chars(text)} chars›"])


def prompt(params: Mapping[str, Any]) -> str:
    """The DM to the human: what wants to run, and how to answer. Both texts
    come from the model's tool call: shown as code, never as markdown."""
    rid = str(params.get("request_id", ""))
    tool = re.sub(r"[^\w.:-]", "", str(params.get("tool_name", ""))) or "?"
    args = preview(tool, str(params.get("input_preview", "")))
    if len(args) > PREVIEW_CHARS:
        args = args[:PREVIEW_CHARS] + " ⋯ cut"
    return "\n".join([
        f"🔐 **Permission for `{tool}`?** · id `{rid}`",
        _fenced(str(params.get("description", ""))),
        _fenced(args),
        f"Reply `yes {rid}` or `no {rid}`, or 👍 / 👎 on this post. "
        "The terminal can answer too; the first answer wins.",
    ])


@dataclass
class Pending:
    post_id: str
    tool: str
    at: float


class Relay:
    """Open prompts of this session, by request id and by prompt post."""

    def __init__(self) -> None:
        self.pending: dict[str, Pending] = {}

    def _expire(self) -> None:
        now = time.time()
        for rid in [r for r, p in self.pending.items() if now - p.at > MAX_AGE_SEC]:
            del self.pending[rid]

    def add(self, request_id: str, post_id: str, tool: str) -> None:
        self._expire()
        self.pending[request_id] = Pending(post_id, tool, time.time())

    def __contains__(self, request_id: str) -> bool:
        self._expire()
        return request_id in self.pending

    def for_post(self, post_id: str) -> str | None:
        self._expire()
        return next((r for r, p in self.pending.items() if p.post_id == post_id), None)

    def take(self, request_id: str) -> Pending | None:
        self._expire()
        return self.pending.pop(request_id, None)

    def take_all(self) -> dict[str, Pending]:
        """Every open prompt, now closed: the turn ended (RT-6.13 step 5)."""
        self._expire()
        taken, self.pending = self.pending, {}
        return taken

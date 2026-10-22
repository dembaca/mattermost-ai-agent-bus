"""Claude Code permission prompts relayed to the responsible human (RT-6.13).

Research preview (code.claude.com/docs/en/channels-reference, "Relay permission
prompts"). A channel that declares `claude/channel/permission` gets each
tool-use approval prompt as `notifications/claude/channel/permission_request`
with a five-letter request id, while the terminal dialog stays open. A verdict
`notifications/claude/channel/permission` {request_id, behavior: allow|deny}
answers it; whichever answer comes first wins, a verdict for an id that is no
longer open is dropped by Claude Code without a word.

The prompt goes by DM to the responsible human: its preview is the tool's
arguments (a command, a file's content) with only well-known secrets masked.
A verdict counts only from a trusted human (server-verified author, RT-6.8):
`yes <id>` / `no <id>`, or 👍/👎 on the prompt post.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

CAPABILITY = "claude/channel/permission"
REQUEST = "notifications/claude/channel/permission_request"
VERDICT = "notifications/claude/channel/permission"
# Claude Code's ids: five letters a-z without l. /i for phone autocorrect.
REPLY = re.compile(r"^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$", re.I)
# Claude Code says nothing when the terminal answered first; forget then.
MAX_AGE_SEC = 86400.0
# A post holds 16383 characters; Claude Code caps each field at 3,500.
PREVIEW_CHARS = 12000


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


def prompt(params: Mapping[str, Any]) -> str:
    """The DM to the human: what wants to run, and how to answer. Both texts
    come from the model's tool call: shown as code, never as markdown."""
    rid = str(params.get("request_id", ""))
    tool = re.sub(r"[^\w.:-]", "", str(params.get("tool_name", ""))) or "?"
    preview = str(params.get("input_preview", ""))
    if len(preview) > PREVIEW_CHARS:
        preview = preview[:PREVIEW_CHARS] + " ⋯ cut"
    return "\n".join([
        f"🔐 **Permission for `{tool}`?** · id `{rid}`",
        _fenced(str(params.get("description", ""))),
        _fenced(preview),
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

    def for_post(self, post_id: str) -> str | None:
        self._expire()
        return next((r for r, p in self.pending.items() if p.post_id == post_id), None)

    def take(self, request_id: str) -> Pending | None:
        self._expire()
        return self.pending.pop(request_id, None)

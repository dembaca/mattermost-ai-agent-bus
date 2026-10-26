"""Departed agents (RT-6.21 in docs/arc42/06-runtime-view.md).

A post in the work channel that @-mentions an agent which is gone reaches
nobody. One server answers it in the thread, without the model: the
coordinator's (a live 👑 here), else, with no crown anywhere, the server of
the oldest reachable agent, which the lease hands the crown to next (RT-6.6).
The answer says since when the agent is gone and links its BYE (its handoff)
or its last post; the event wakes the answering agent, which routes the work.

Gone: the agent's own latest post in its newest HELLO thread is a BYE, or its
status lease ran out (no live custom status), or it is no member of the work
channel any more while its HELLO is still there. 💤 idle and 👂 listening run
on a live lease: not gone. Each thread is told once per agent (gate-<uid>.env,
DEPARTED=<root_id>:<name>).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import a2a, gate, lease, session_file

if TYPE_CHECKING:
    from .client import MattermostClient

log = logging.getLogger(__name__)

# How far back the work channel is read for HELLO, BYE and last posts.
RECENT = 200
_MENTION = re.compile(r"(?<![\w.-])@([a-z0-9][a-z0-9._-]*)", re.I)


@dataclass(frozen=True)
class Departure:
    username: str
    since_ms: int  # 0: unknown
    bye: bool  # else the lease ran out
    post_id: str  # its BYE, else its last post; '' when none is in reach


def mentioned(message: str, me: str) -> list[str]:
    """The bus agents a message @-mentions (the registrar's prefix), not me."""
    prefix, out = session_file.agent_prefix().lower(), []
    for m in _MENTION.findall(message or ""):
        name = m.rstrip(".").lower()
        if name.startswith(prefix) and name != me.lower() and name not in out:
            out.append(name)
    return out


async def answers(client: MattermostClient, uid: str, name: str) -> bool:
    """Whether this server is the one that answers: it holds the 👑, or no
    agent wears one and it is the oldest reachable agent."""
    if lease.crown_held(uid):
        return True
    if await gate.coordinator_name(client):
        return False
    rows = await lease.roster(client)
    return bool(rows) and rows[0].username.lower() == name.lower()


def _hello_of(post: Mapping[str, Any], name: str) -> bool:
    first = str(post.get("message") or "").split("\n", 1)[0]
    return re.search(rf"(?<![\w.-])@{re.escape(name)}(?![\w-])", first, re.I) is not None


async def gone(client: MattermostClient, names: list[str]) -> list[Departure]:
    """Which of the named agents are gone, with since when and the post to link."""
    cid = await client.work_channel_id()
    rows = {str(r["username"]).lower(): r for r in await lease._rows(client, cid)}
    posts = [p for p in await client.channel_posts(cid, per_page=RECENT)
             if not p.get("type") and not p.get("delete_at")]  # newest first
    hellos = [p for p in posts if not p.get("root_id") and a2a.word(str(p.get("message") or "")) == "HELLO"]
    out = []
    for name in names:
        row = rows.get(name)
        uid = str(row["id"]) if row else ""
        hello = next((p for p in hellos if (p.get("user_id") == uid if uid else _hello_of(p, name))), None)
        uid = uid or str((hello or {}).get("user_id") or "")
        if not uid:
            continue  # no agent of this bus that we know of
        own = [p for p in posts if p.get("user_id") == uid]
        last = next((p for p in own if hello and hello["id"] in (p.get("id"), p.get("root_id"))), None)
        if last and a2a.word(str(last.get("message") or "")) == "BYE":
            out.append(Departure(name, int(last.get("create_at") or 0), True, str(last["id"])))
        elif row is None or not row.get("live"):
            since = (row or {}).get("until_ms") or (int(own[0].get("create_at") or 0) if own else 0)
            out.append(Departure(name, int(since), False, str(own[0]["id"]) if own else ""))
    return out


def _when(ms: int) -> str:
    at = time.gmtime(ms / 1000)
    same_day = time.strftime("%Y-%m-%d", at) == time.strftime("%Y-%m-%d", time.gmtime())
    return time.strftime("%H:%M UTC" if same_day else "%Y-%m-%d %H:%M UTC", at)


def reply_text(found: list[Departure], permalink_base: str) -> str:
    lines = []
    for d in found:
        since = f" since {_when(d.since_ms)}" if d.since_ms else ""
        why = "BYE" if d.bye else "its lease ran out"
        link = (f", see its {'handoff' if d.bye else 'last post'}: {permalink_base}{d.post_id}"
                if d.post_id else "")
        lines.append(f"{d.username} is gone{since} ({why}){link}.")
    return " ".join(lines) + " I route this instead."


async def check(client: MattermostClient, post: Mapping[str, Any], uid: str,
                name: str) -> tuple[list[Departure], list[Departure]]:
    """The departed agents a work-channel post mentions, and of those the ones
    this server has to tell its thread about (claimed in gate-<uid>.env).
    Both empty when another server answers."""
    names = mentioned(str(post.get("message") or ""), name)
    if not names or post.get("type") or not await answers(client, uid, name):
        return [], []
    if post.get("channel_id") != await client.work_channel_id():
        return [], []
    found = await gone(client, names)
    root = str(post.get("root_id") or post.get("id") or "")
    return found, [d for d in found if gate.first_reply(uid, f"{root}:{d.username}", "DEPARTED")]


async def tell(client: MattermostClient, post: Mapping[str, Any], uid: str,
               found: list[Departure]) -> None:
    """Answer in the post's thread; when that fails, the next mention may try again."""
    root = str(post.get("root_id") or post.get("id") or "")
    try:
        text = lease.with_crown(reply_text(found, client.permalink_base), uid)
        await client.post(text, channel_id=str(post.get("channel_id") or "") or None, root_id=root)
    except Exception as exc:  # noqa: BLE001 — the wake-up stands all the same
        log.info("no answer on departed agents in thread %s: %s", root, exc)
        for d in found:
            gate.forget_reply(uid, f"{root}:{d.username}", "DEPARTED")

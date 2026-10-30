"""Departed agents (RT-6.21 in docs/arc42/06-runtime-view.md).

A post in the work channel that @-mentions an agent which is gone reaches
nobody. One server answers it in the thread, without the model: the
coordinator's (a live 👑 here), else, with no crown anywhere, the server of
the oldest reachable agent, which the lease hands the crown to next (RT-6.6).
The answer says since when the agent is gone and links its BYE (its handoff)
or its last post; the event wakes the answering agent, which routes the work.

Gone: its status lease ran out (no live custom status), or it is no member of
the work channel any more while its HELLO is still there. A BYE alone is not
(it may mean "task finished"); for a gone agent whose latest post in its newest
HELLO thread is a BYE, that BYE gives the time and the handoff link. 💤 idle
and 👂 listening run on a live lease: not gone. Each thread is told once per agent (gate-<uid>.env,
DEPARTED=<root_id>:<name>).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import a2a, gate, lease, session_file
from .model import Post

if TYPE_CHECKING:
    from .port import ChatPort

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


async def answers(client: ChatPort, uid: str, name: str) -> bool:
    """Whether this server is the one that answers (RT-6.21 step 2): it holds
    its home's 👑, or no agent of the home wears one and it is the home's
    oldest reachable agent (lease.roster counts only the home's agents)."""
    if lease.crown_held(uid):
        return True
    if await gate.coordinator_name(client):
        return False
    rows = await lease.roster(client)
    return bool(rows) and rows[0].username.lower() == name.lower()


def _hello_of(post: Post, name: str, authors: dict[str, str]) -> bool:
    """Whether `post` is the HELLO of `name`: by its author, else (a HELLO
    before 0.65.4 whose author cannot be read) by `HELLO @<name>`."""
    if authors.get(post.user_id, "").lower() == name.lower():
        return True
    first = post.message.split("\n", 1)[0]
    return re.search(rf"(?<![\w.-])@{re.escape(name)}(?![\w-])", first, re.I) is not None


async def _authors(client: ChatPort, posts: list[Post]) -> dict[str, str]:
    """{user_id: username} of the posts' authors; {} when they cannot be read."""
    try:
        return await client.usernames({p.user_id for p in posts})
    except Exception as exc:  # noqa: BLE001 — the old HELLO text still names them
        log.debug("departed: authors: %s", exc)
        return {}


async def gone(client: ChatPort, names: list[str]) -> list[Departure]:
    """Which of the named agents are gone, with since when and the post to link."""
    cid = await client.work_channel_id()
    rows = {str(r["username"]).lower(): r for r in await lease._rows(client, cid)}
    posts = [p for p in await client.channel_posts(cid, per_page=RECENT)
             if not p.type and not p.delete_at]  # newest first
    hellos = [p for p in posts if not p.root_id and a2a.word(p.message) == "HELLO"]
    # A member no longer in the channel has no row: its HELLO's author names it.
    authors = await _authors(client, hellos) if any(n not in rows for n in names) else {}
    out = []
    for name in names:
        row = rows.get(name)
        uid = str(row["id"]) if row else ""
        hello = next((p for p in hellos if (p.user_id == uid if uid else _hello_of(p, name, authors))), None)
        uid = uid or (hello.user_id if hello else "")
        if not uid:
            continue  # no agent of this bus that we know of
        own = [p for p in posts if p.user_id == uid]
        last = next((p for p in own if hello and hello.id in (p.id, p.root_id)), None)
        if row is not None and row.get("live"):
            continue  # a live lease: here, BYE or not ("task finished")
        if last and a2a.word(last.message) == "BYE":
            out.append(Departure(name, last.create_at, True, last.id))
        else:
            since = (row or {}).get("until_ms") or (own[0].create_at if own else 0)
            out.append(Departure(name, int(since), False, own[0].id if own else ""))
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


async def check(client: ChatPort, post: Post, uid: str,
                name: str) -> tuple[list[Departure], list[Departure]]:
    """The departed agents a work-channel post mentions, and of those the ones
    this server has to tell its thread about (claimed in gate-<uid>.env).
    Both empty when another server answers."""
    names = mentioned(post.message, name)
    if not names or post.type or not await answers(client, uid, name):
        return [], []
    if post.channel_id != await client.work_channel_id():
        return [], []
    found = await gone(client, names)
    root = post.thread
    return found, [d for d in found if gate.first_reply(uid, f"{root}:{d.username}", "DEPARTED")]


async def tell(client: ChatPort, post: Post, uid: str,
               found: list[Departure]) -> None:
    """Answer in the post's thread; when that fails, the next mention may try again."""
    root = post.thread
    try:
        text = lease.with_crown(reply_text(found, client.permalink_base), uid)
        await client.post(text, channel_id=post.channel_id or None, root_id=root)
    except Exception as exc:  # noqa: BLE001 — the wake-up stands all the same
        log.info("no answer on departed agents in thread %s: %s", root, exc)
        for d in found:
            gate.forget_reply(uid, f"{root}:{d.username}", "DEPARTED")

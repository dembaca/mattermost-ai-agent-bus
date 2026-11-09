"""Departed agents and agents not here (RT-6.21 in docs/arc42/06-runtime-view.md).

A post in the work channel that @-mentions an agent which is gone, or a live
one that is no member of the channel, reaches nobody. One server answers it in
the thread, without the model: the coordinator's (a live 👑 here), else, with
no crown anywhere, the server of the oldest reachable peer, which the lease
hands the crown to next (RT-6.6 step 2). For a gone agent the answer says since when
and links its BYE (its handoff) or its last post; for one not here, that it
is not in this channel and where its home is. The event wakes the answering
agent, which routes or forwards the work.

Gone: its status lease ran out (no live custom status), or it is no member of
the work channel any more while its HELLO is still there. A BYE alone is not
(it may mean "task finished"); for a gone agent whose latest post in its newest
HELLO thread is a BYE, that BYE gives the time and the handoff link. 💤 idle
and 👂 listening run on a live lease: not gone. Not here: a bot account by
that name on a live lease that is no member of the channel, read only for the
names neither in the channel's roster nor gone. Each thread is told once per
agent (gate-<uid>.env, DEPARTED=<root_id>:<name>).
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

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


@dataclass(frozen=True)
class Elsewhere:
    """A live agent that is no member of the channel the post is in."""

    username: str
    home: str  # the home its status names; '' unknown


Absent = Departure | Elsewhere


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
    oldest reachable peer (lease.roster counts only the home's agents and marks a
    coordinator only among those that stand for the crown, ADR-007)."""
    if lease.crown_held(uid):
        return True
    if await gate.coordinator_name(client):
        return False
    rows = await lease.roster(client)
    return bool(rows) and rows[0].coordinator and rows[0].username.lower() == name.lower()


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


async def _members(client: ChatPort, channel_id: str) -> dict[str, dict[str, Any]]:
    """The channel's agents by lower-case name, with their lease (lease._rows)."""
    return {str(r["username"]).lower(): r for r in await lease._rows(client, channel_id)}


async def gone(client: ChatPort, names: list[str],
               rows: dict[str, dict[str, Any]] | None = None) -> list[Departure]:
    """Which of the named agents are gone, with since when and the post to link.
    rows: the work channel's agents (_members) when read already."""
    cid = await client.work_channel_id()
    if rows is None:
        rows = await _members(client, cid)
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


async def elsewhere(client: ChatPort, channel_id: str, names: list[str]) -> list[Elsewhere]:
    """Of the named agents, none in the channel's roster nor gone, the live
    ones that are no member of it (RT-6.21 step 3): a bot account by that
    name whose status is a live lease. One read for all names, one per live
    one for its membership (both cached by the adapter); a person or a name
    nobody has is left to Mattermost. [] when they cannot be read."""
    if not names:
        return []
    try:
        bots = await client.bots_named(set(names))
        out = []
        for name in names:
            user = bots.get(name)
            home = lease.live_home(user) if user is not None else None
            if user is None or home is None or await client.is_member(channel_id, user.id):
                continue
            out.append(Elsewhere(name, home))
        return out
    except Exception as exc:  # noqa: BLE001 — fail open: the gone ones still get their answer
        log.info("departed: agents not here not read: %s", exc)
        return []


def not_here(found: list[Absent]) -> str:
    """The event's `not_here`: `<agent> (~<home>)`, comma-separated."""
    return ", ".join(f"{d.username} (~{d.home})" if d.home else d.username
                     for d in found if isinstance(d, Elsewhere))


def _when(ms: int) -> str:
    at = time.gmtime(ms / 1000)
    same_day = time.strftime("%Y-%m-%d", at) == time.strftime("%Y-%m-%d", time.gmtime())
    return time.strftime("%H:%M UTC" if same_day else "%Y-%m-%d %H:%M UTC", at)


def reply_text(found: list[Absent], permalink_base: str, channel: str = "") -> str:
    """One sentence per agent (RT-6.21 step 4): the gone ones and "I route
    this instead.", then the ones not in `channel`."""
    lines, away = [], []
    where = f"~{channel}" if channel else "this channel"
    for d in found:
        if isinstance(d, Elsewhere):
            home = f" (home ~{d.home})" if d.home else ""
            away.append(f"{d.username} is not in {where}{home}: add it to the channel, or I forward it.")
            continue
        since = f" since {_when(d.since_ms)}" if d.since_ms else ""
        why = "BYE" if d.bye else "its lease ran out"
        link = (f", see its {'handoff' if d.bye else 'last post'}: {permalink_base}{d.post_id}"
                if d.post_id else "")
        lines.append(f"{d.username} is gone{since} ({why}){link}.")
    if lines:
        lines.append("I route this instead.")
    return " ".join(lines + away)


async def _present(client: ChatPort, channel_id: str, names: list[str]) -> list[str]:
    """Of the names the channel's page of agents lacks, the live ones that are
    members after all (a channel of more than 200 accounts)."""
    if not names:
        return []
    bots = await client.bots_named(set(names))
    return [n for n in names if (u := bots.get(n)) is not None and lease.live_home(u) is not None
            and await client.is_member(channel_id, u.id)]


async def unreachable(client: ChatPort, message: str, me: str, channel_id: str) -> list[str]:
    """The agents an outgoing task names (@, not me) that the roster of
    `channel_id` does not show as reachable, each `<name> (<why>)` (RT-6.14
    step 10). Reachable is a live lease, online, not idle: not that the agent
    listens right now (a lease outlives its session by up to MM_LEASE_SEC).
    One read of the channel's agents for all names; [] when none is named or
    when they cannot be read (fail open: the post is out anyway)."""
    names = mentioned(message, me)
    if not names:
        return []
    try:
        rows = await _members(client, channel_id)
        why_not = {n: lease.not_reachable(rows.get(n)) for n in names}
        # A channel's agents are read up to a page: one beyond it is no absent one.
        for n in await _present(client, channel_id, [n for n in names if why_not[n] == "absent"]):
            why_not[n] = ""
        bad = [n for n in names if why_not[n]]
        if not bad:
            return []
        work = channel_id == await client.work_channel_id()
        left = [n for n in bad if why_not[n] in ("absent", "lease")]
        departures = {d.username: d for d in await gone(client, left, rows)} if work and left else {}
        away = {e.username: e for e in await elsewhere(
            client, channel_id, [n for n in bad if n not in rows and n not in departures])}
    except Exception as exc:  # noqa: BLE001 — fail open: no warning
        log.info("unreachable agents not read: %s", exc)
        return []
    out = []
    for n in bad:
        d = departures.get(n)
        if d is not None:
            since = f" since {_when(d.since_ms)}" if d.since_ms else ""
            why = f"gone{since}, {'BYE' if d.bye else 'its lease ran out'}"
        elif n in away:
            home = f"~{away[n].home}" if away[n].home else "another channel"
            why = f"live in {home}, not in this channel"
        else:
            why = {"absent": "not in this channel, no agent of this bus by that name that I can see",
                   "lease": "its lease ran out", "idle": "idle, until its human resumes",
                   "offline": "offline"}[why_not[n]]
        out.append(f"{n} ({why})")
    return out


async def check(client: ChatPort, post: Post, uid: str,
                name: str) -> tuple[list[Absent], list[Absent]]:
    """The agents a work-channel post mentions that are gone or not here, and
    of those the ones this server has to tell its thread about (claimed in
    gate-<uid>.env). Both empty when another server answers."""
    names = mentioned(post.message, name)
    if not names or post.type or not await answers(client, uid, name):
        return [], []
    cid = await client.work_channel_id()
    if post.channel_id != cid:
        return [], []
    rows = await _members(client, cid)
    found: list[Absent] = list(await gone(client, names, rows))
    known = set(rows) | {d.username for d in found}
    found += await elsewhere(client, cid, [n for n in names if n not in known])
    root = post.thread
    return found, [d for d in found if gate.first_reply(uid, f"{root}:{d.username}", "DEPARTED")]


async def tell(client: ChatPort, post: Post, uid: str,
               found: list[Absent]) -> None:
    """Answer in the post's thread; when that fails, the next mention may try again."""
    root = post.thread
    try:
        text = lease.with_crown(reply_text(found, client.permalink_base, client.work_channel_name), uid)
        await client.post(text, channel_id=post.channel_id or None, root_id=root,
                          props={a2a.system_key(): True})
    except Exception as exc:  # noqa: BLE001 — the wake-up stands all the same
        log.info("no answer on departed or absent agents in thread %s: %s", root, exc)
        for d in found:
            gate.forget_reply(uid, f"{root}:{d.username}", "DEPARTED")

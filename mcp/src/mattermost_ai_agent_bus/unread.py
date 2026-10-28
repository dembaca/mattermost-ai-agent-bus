"""Is anyone waiting on this agent? The pending count for the turn hooks and
the listener (RT-6.2 step 1, RT-6.17 in docs/arc42/06-runtime-view.md), and
what the inbox's backfill reads.

What waits is what came after the bus's read watermark (watermark.py), at
most a day back and never before the bot existed. A channel is read only when
its newest post is newer than that; Mattermost's counters decide nothing, and
only stand in for a channel whose newest post's time Mattermost leaves out.
Of those posts, a DM or group counts someone else's; any other channel the
posts that mention this agent (or @channel, @all, @here) and the posts for all
agents (``to: all``, RT-6.11). Posts the wake gate held back (RT-6.16) count
nowhere. Per channel, the detail says how many of them are an agent's fyi
posts (RT-6.14), which need no answer. Reading marks nothing handled, except a
run of the bot's own, system and gated posts right after the mark: nothing
there is for the agent, and passing it saves the next read.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any, NamedTuple

from . import a2a, gate, watermark
from .client import account_created

if TYPE_CHECKING:
    from .port import ChatPort
    from .model import Post, UnreadChannel

log = logging.getLogger(__name__)

DETAIL_CHANNELS = 5
DAY_MS = 86_400_000
_MENTION = re.compile(r"@[a-z0-9_.-]+")
_EVERYONE = ("@channel", "@all", "@here")


class After(NamedTuple):
    """A channel's posts after its watermark, oldest first."""

    channel: UnreadChannel
    posts: list[Post]


class Pending(NamedTuple):
    """A channel with `waiting` posts waiting for this agent among `posts`."""

    channel: UnreadChannel
    waiting: int
    posts: list[Post]


def _at(post: Post) -> int:
    return post.create_at


def _passed(post: Post, uid: str, gated: set[str]) -> bool:
    """Nothing in it for the agent: its own, a system post, deleted, gated."""
    return bool(post.user_id == uid or post.type or post.delete_at or post.id in gated)


def _direct(channel: UnreadChannel) -> bool:
    return channel.channel_type in ("D", "G")


async def after(client: ChatPort) -> list[After]:
    """Every channel with posts after its watermark. One request for the
    channels, then one per channel whose newest post is past its mark."""
    me = await client.get_me()
    uid = str(me.get("id") or "")
    floor = max(account_created(me), int(time.time() * 1000) - DAY_MS)
    marks = watermark.read(uid)
    gated = gate.gated(uid)
    out: list[After] = []
    for ch in await client.member_channels():
        mark = marks.get(ch.channel_id)
        since = max(mark.at if mark else 0, floor)
        if ch.last_post_at:
            if ch.last_post_at <= since:
                continue
        elif not (ch.pending or ch.unread):
            continue
        try:
            got = await client.channel_posts(ch.channel_id, since_ms=since)
        except Exception as exc:  # noqa: BLE001 — read again next time
            log.debug("posts after the watermark in %s: %s", ch.channel_id, exc)
            continue
        posts = sorted((p for p in got if _at(p) > since), key=_at)
        skip = 0
        while skip < len(posts) and _passed(posts[skip], uid, gated):
            skip += 1
        if skip:
            last = posts[skip - 1]
            watermark.advance(uid, ch.channel_id, last.id, _at(last))
        if posts[skip:]:
            out.append(After(ch, posts[skip:]))
    return out


def _for_me(post: Post, channel: UnreadChannel, name: str) -> bool:
    """Addressed to this agent: in a DM or group, for all agents, or naming it
    (or @channel, @all, @here)."""
    if _direct(channel) or post.to == a2a.ALL:
        return True
    names = {m.removesuffix(".") for m in _MENTION.findall(post.message.lower())}
    return f"@{name.lower()}" in names or any(e in names for e in _EVERYONE)


async def channels(client: ChatPort) -> list[Pending]:
    """The channels with something for this agent, each with how much is
    pending there and the posts it was counted from."""
    me = await client.get_me()
    uid, name = str(me.get("id") or ""), str(me.get("username") or "")
    gated = gate.gated(uid)
    out: list[Pending] = []
    for ch, posts in await after(client):
        mine = [p for p in posts if not _passed(p, uid, gated) and _for_me(p, ch, name)]
        if mine:
            out.append(Pending(ch, len(mine), mine))
    return out


async def detail(client: ChatPort, rows: list[Pending]) -> list[dict[str, Any]]:
    """The first DETAIL_CHANNELS of channels(): {id, type, pending, fyi}, fyi
    how many of the posts for this agent are an agent's fyi posts (props anyone
    can write count only from an agent). One user lookup, no more."""
    fyi = [(row.channel.channel_id, p.user_id) for row in rows[:DETAIL_CHANNELS]
           for p in row.posts if p.intent == a2a.FYI]
    agents: set[str] = set()
    if fyi:
        try:
            agents = await client.agents({u for _, u in fyi})
        except Exception as exc:  # noqa: BLE001 — nobody counts as an agent then
            log.debug("fyi authors: %s", exc)
    return [{"id": row.channel.channel_id, "type": row.channel.channel_type,
             "pending": row.waiting,
             "fyi": sum(1 for c, u in fyi if c == row.channel.channel_id and u in agents)}
            for row in rows[:DETAIL_CHANNELS]]

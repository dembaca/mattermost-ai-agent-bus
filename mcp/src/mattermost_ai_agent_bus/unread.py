"""Is anyone waiting on this agent? The pending count for the turn hooks and
the listener (RT-6.2 step 1, RT-6.17 in docs/arc42/06-runtime-view.md).

A DM or group counts its unread messages; any other channel its mentions plus
the unread posts for all agents (``to: all``, RT-6.11), which hold no @ and so
raise no mention counter, leaving out those the wake gate held back (RT-6.16).
Per channel, the detail says how many of them are an agent's fyi posts
(RT-6.14), which need no answer. Reading the counters marks nothing read;
draining the inbox (wait_for_events) does.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from . import a2a, gate

if TYPE_CHECKING:
    from .client import MattermostClient
    from .model import UnreadChannel

log = logging.getLogger(__name__)

DETAIL_CHANNELS = 5
DAY_MS = 86_400_000
_MENTION = re.compile(r"@[a-z0-9_.-]+")
_EVERYONE = ("@channel", "@all", "@here")


def _since(channel: UnreadChannel) -> int:
    """Read from the last view, a day back at most (and never before the bot
    existed, client.unread_channels)."""
    return max(channel.since, int(time.time() * 1000) - DAY_MS)


def _new(post: Mapping[str, Any], uid: str, since: int) -> bool:
    """Someone else's user post, created after `since`."""
    return (post.get("user_id") != uid and not post.get("type")
            and int(post.get("create_at") or 0) > since)


def _direct(channel: UnreadChannel) -> bool:
    return channel.channel_type in ("D", "G")


async def channels(client: MattermostClient) -> list[tuple[UnreadChannel, int]]:
    """The channels with something for this agent, in the counters' order, each
    with how much is pending there. Posts for all agents cost one request per
    channel with unread posts, no more."""
    uid = str((await client.get_me()).get("id") or "")
    gated = gate.gated(uid)
    out: list[tuple[UnreadChannel, int]] = []
    for ch in await client.unread_channels(unread_posts=True):
        n = ch.pending
        if not _direct(ch) and ch.unread > 0:
            since = _since(ch)
            try:
                posts = await client.channel_posts(ch.channel_id, since_ms=since)
            except Exception as exc:  # noqa: BLE001 — the mentions still count
                log.debug("posts for all agents: %s", exc)
                posts = []
            n += sum(1 for p in posts
                     if _new(p, uid, since) and a2a.to_all(p) and p.get("id") not in gated)
        if n > 0:
            out.append((ch, n))
    return out


def _for_me(post: Mapping[str, Any], channel: UnreadChannel, name: str) -> bool:
    """Addressed to this agent: in a DM or group, for all agents, or naming it
    (or @channel, @all, @here)."""
    if _direct(channel) or a2a.to_all(post):
        return True
    names = {m.removesuffix(".") for m in _MENTION.findall(str(post.get("message") or "").lower())}
    return f"@{name.lower()}" in names or any(e in names for e in _EVERYONE)


async def detail(client: MattermostClient,
                 rows: list[tuple[UnreadChannel, int]]) -> list[dict[str, Any]]:
    """The first DETAIL_CHANNELS of channels(): {id, type, pending, fyi}, fyi
    how many of the posts for this agent are an agent's fyi posts (props anyone
    can write count only from an agent). One request per channel, one user
    lookup."""
    me = await client.get_me()
    uid, name = str(me.get("id") or ""), str(me.get("username") or "")
    fyi: list[tuple[str, str]] = []
    for ch, _ in rows[:DETAIL_CHANNELS]:
        since = _since(ch)
        try:
            posts = await client.channel_posts(ch.channel_id, since_ms=since)
        except Exception as exc:  # noqa: BLE001 — no fyi count for it
            log.debug("fyi posts: %s", exc)
            continue
        fyi += [(ch.channel_id, str(p.get("user_id"))) for p in posts
                if _new(p, uid, since) and a2a.intent_of(p) == a2a.FYI and _for_me(p, ch, name)]
    agents: set[str] = set()
    if fyi:
        try:
            agents = await client.agents({u for _, u in fyi})
        except Exception as exc:  # noqa: BLE001 — nobody counts as an agent then
            log.debug("fyi authors: %s", exc)
    return [{"id": ch.channel_id, "type": ch.channel_type, "pending": n,
             "fyi": sum(1 for c, u in fyi if c == ch.channel_id and u in agents)}
            for ch, n in rows[:DETAIL_CHANNELS]]

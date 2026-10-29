"""Lean tool results: what an agent needs from a post, nothing more.

Mattermost's post JSON carries metadata, props, hashtags, file ids and more;
an agent reads every token of it in every turn the result stays in context.
A slim post keeps id, thread, channel name, author name, time and text — about
a fifth of the size — and, for a post with attachments, a short `files` hint
(id, name, type, size) to fetch them by with get_file (RT-6.23). Tools take
raw=true for the full JSON.

What the bus brings unasked (a channel push, a wait_for_events item) has its
text cut at BODY_MAX characters with a pointer to get_thread (RT-6.12 step 3);
what the agent fetches by id or channel (get_thread, list_recent) stays whole.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from . import files

if TYPE_CHECKING:
    from .port import ChatPort
    from .model import Post


# Pushed and delivered posts are cut here (RT-6.12 step 3): about 7% of the
# measured pushes are longer, Mattermost allows 16,383 (#68).
BODY_MAX = 2000


def cut(text: str, post_id: str) -> str:
    """`text` at most about BODY_MAX characters, cut at a line or word end and
    ending with where the rest is: get_thread with the post's id, which works
    for a root and a reply alike. Unchanged when short or without an id."""
    if len(text) <= BODY_MAX or not post_id:
        return text
    head = text[:BODY_MAX]
    floor = BODY_MAX * 4 // 5  # a boundary further back drops too much
    end = head.rfind("\n", floor)
    if end < 0:
        end = max(head.rfind(" ", floor), head.rfind("\t", floor))
    if end > 0:
        head = head[:end]
    return f"{head.rstrip()}\n… (cut, {len(text)} chars in all; get_thread {post_id})"


def _when(ms: int | None) -> str:
    if not ms:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


async def _names(client: ChatPort, posts: Sequence[Post]) -> tuple[dict[str, str], dict[str, str]]:
    users: dict[str, str] = {}
    channels: dict[str, str] = {}
    try:
        users = await client.usernames({p.user_id for p in posts} - {""})
    except Exception:  # noqa: BLE001 — names are a convenience; ids still identify
        pass
    for cid in {p.channel_id for p in posts} - {""}:
        try:
            channels[cid] = (await client.get_channel(cid)).name or cid
        except Exception:  # noqa: BLE001
            channels[cid] = cid
    return users, channels


def _one(p: Post, users: dict[str, str], channels: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": p.id}
    if p.root_id:
        out["root_id"] = p.root_id
    out["channel"] = channels.get(p.channel_id, p.channel_id)
    out["author"] = users.get(p.user_id) or p.user_id
    out["at"] = _when(p.create_at)
    out["message"] = p.message
    if p.type:
        out["type"] = p.type
    if p.edit_at:
        out["edited"] = True
    if p.reply_count:
        out["replies"] = p.reply_count
    if p.files:
        out["files"] = [files.hint(f) for f in p.files]  # RT-6.23: get_file fetches one
    return out


async def posts(client: ChatPort, items: Sequence[Post],
                *, drop_system: bool = False) -> list[dict[str, Any]]:
    if drop_system:
        items = [p for p in items if not p.type]
    users, channels = await _names(client, items)
    return [_one(p, users, channels) for p in items]


async def post(client: ChatPort, item: Post) -> dict[str, Any]:
    return (await posts(client, [item]))[0]


def text_of(item: Mapping[str, Any]) -> str:
    """The message of a wait_for_events item's slim post; '' without one."""
    return str((item.get("post") or {}).get("message") or "")


def cut_item(item: dict[str, Any]) -> None:
    """A wait_for_events item's post text cut in place (see `cut`)."""
    post = item.get("post")
    if isinstance(post, dict) and isinstance(post.get("message"), str):
        post["message"] = cut(post["message"], str(post.get("id") or ""))


def root_of(item: Mapping[str, Any]) -> str:
    """The thread root of a wait_for_events item's slim post; '' for a root."""
    return str((item.get("post") or {}).get("root_id") or "")

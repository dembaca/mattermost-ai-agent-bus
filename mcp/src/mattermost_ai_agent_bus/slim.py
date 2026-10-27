"""Lean tool results: what an agent needs from a post, nothing more.

Mattermost's post JSON carries metadata, props, hashtags, file ids and more;
an agent reads every token of it in every turn the result stays in context.
A slim post keeps id, thread, channel name, author name, time and text — about
a fifth of the size. Tools take raw=true for the full JSON.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import MattermostClient


def _when(ms: int | None) -> str:
    if not ms:
        return ""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ms / 1000))


async def _names(client: MattermostClient, posts: list[dict[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    users: dict[str, str] = {}
    channels: dict[str, str] = {}
    try:
        users = await client.usernames({p.get("user_id", "") for p in posts} - {""})
    except Exception:  # noqa: BLE001 — names are a convenience; ids still identify
        pass
    for cid in {p.get("channel_id", "") for p in posts} - {""}:
        try:
            channels[cid] = (await client.get_channel(cid)).get("name") or cid
        except Exception:  # noqa: BLE001
            channels[cid] = cid
    return users, channels


def _one(p: dict[str, Any], users: dict[str, str], channels: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": p.get("id")}
    if p.get("root_id"):
        out["root_id"] = p["root_id"]
    cid = p.get("channel_id", "")
    out["channel"] = channels.get(cid, cid)
    uid = p.get("user_id", "")
    out["author"] = users.get(uid) or uid
    out["at"] = _when(p.get("create_at"))
    out["message"] = p.get("message", "")
    if p.get("type"):
        out["type"] = p["type"]
    if p.get("edit_at"):
        out["edited"] = True
    if p.get("reply_count"):
        out["replies"] = p["reply_count"]
    return out


async def posts(client: MattermostClient, items: list[dict[str, Any]],
                *, drop_system: bool = False) -> list[dict[str, Any]]:
    if drop_system:
        items = [p for p in items if not p.get("type")]
    users, channels = await _names(client, items)
    return [_one(p, users, channels) for p in items]


async def post(client: MattermostClient, item: dict[str, Any]) -> dict[str, Any]:
    return (await posts(client, [item]))[0]

"""Who may wake an agent (RT-6.16 in docs/arc42/06-runtime-view.md).

Every post's author is one of three classes, by the account Mattermost
authenticates, never by what the post says: trusted (a trusted human, RT-6.8),
agent (a bot account named agent-*) or other (people, bots that are no bus
agents, and every webhook post, whatever account it names).

With trusted humans configured, a post by `other` that would wake a worker is
gated: it reaches the agent only as data, and the worker's MCP server answers
in the thread itself, once per thread, without the model. The coordinator is
never gated. What the gate did lives next to asks-<uid>.env, in gate-<uid>.env:

    REPLIED=<root_id> <epoch>   a thread this bot answered "Not woken" in
    GATED=<post_id> <epoch>     a gated post: the listener does not wake for it

Entries older than MAX_AGE_SEC are dropped.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import a2a, channel, lease, session_file

if TYPE_CHECKING:
    from .client import MattermostClient

TRUSTED, AGENT, OTHER = "trusted", "agent", "other"
MAX_AGE_SEC = 7 * 86400
# The coordinator's name is read with the roster, a subprocess: at most this often.
COORD_TTL_SEC = 60.0

_coord: tuple[float, str] | None = None


def webhook(post: Mapping[str, Any]) -> bool:
    """Anyone with a hook's URL posts as the account that made the hook."""
    return str((post.get("props") or {}).get("from_webhook", "")) == "true"


def author_class(post: Mapping[str, Any], trusted: Mapping[str, str],
                 agents: set[str]) -> str:
    if webhook(post):
        return OTHER
    uid = post.get("user_id", "")
    if uid in trusted:
        return TRUSTED
    return AGENT if uid in agents else OTHER


async def author_classes(client: MattermostClient, posts: list[dict[str, Any]]) -> list[str]:
    """The class of each post's author; raises when the accounts cannot be read."""
    trusted = await client.trusted_humans()
    agents = await client.agents({p.get("user_id", "") for p in posts} - {""})
    return [author_class(p, trusted, agents) for p in posts]


def wake_reason(kind: str, post: Mapping[str, Any], in_my_thread: bool) -> str:
    """Why the post would wake a worker (RT-6.2 step 4): mention, dm, thread or
    broadcast (@channel in the text, or props to="all"); '' for a post that
    only goes into the digest."""
    if kind in ("mention", "dm"):
        return kind
    if in_my_thread:
        return "thread"
    return "broadcast" if a2a.to_all(post) or channel.broadcast(str(post.get("message", ""))) else ""


async def author_name(client: MattermostClient, post: Mapping[str, Any]) -> str:
    if webhook(post):
        return "a webhook"
    uid = post.get("user_id", "")
    return (await client.usernames({uid})).get(uid) or uid


async def coordinator_name(client: MattermostClient) -> str:
    """The agent wearing a live 👑, '' when none. Only a crown counts: the
    roster's oldest reachable agent coordinates nothing yet. An unreadable
    roster is not remembered."""
    global _coord
    now = time.monotonic()
    if _coord is None or now - _coord[0] > COORD_TTL_SEC:
        rows = await lease.roster(client)
        if not rows:
            return ""
        _coord = (now, next((str(r.get("username", "")) for r in rows if r.get("crown")), ""))
    return _coord[1]


async def names_coordinator(client: MattermostClient, post: Mapping[str, Any]) -> bool:
    """RT-6.16 step 3: the post already @-mentions the agent wearing the 👑: it was told, an
    answer saying that it will look would only repeat it."""
    name = await coordinator_name(client)
    return bool(name) and re.search(
        rf"(?<![\w.-])@{re.escape(name)}(?![\w-]|\.\w)", str(post.get("message", "")), re.I) is not None


async def reply_text(client: MattermostClient, author: str, *, dm: bool) -> str:
    """The answer to a gated post. Names stay plain: only ask_human notifies a
    person, and the coordinator wakes for every channel post anyway."""
    coord = await coordinator_name(client)
    head = f"Not woken: {author} is not on my trusted list"
    if coord and dm:
        # The coordinator cannot see a DM.
        return f"{head}. Ask in ~{client.work_channel_name}, where {coord} ({lease.CROWN}) coordinates."
    if coord:
        return f"{head}. {coord} ({lease.CROWN}) will look at it."
    trusted = await client.trusted_humans()
    names = [n for n in trusted.values() if n] or sorted(
        (await client.usernames(set(trusted))).values())
    return f"{head}, and no agent coordinates right now. Ask {', '.join(names)}."


def _path(uid: str) -> Path:
    return session_file.state_dir() / f"gate-{uid}.env"


def _read(uid: str) -> dict[str, dict[str, float]]:
    state: dict[str, dict[str, float]] = {"REPLIED": {}, "GATED": {}}
    try:
        lines = _path(uid).read_text().splitlines()
    except OSError:
        return state
    now = time.time()
    for line in lines:
        key, _, val = line.partition("=")
        parts = val.split()
        if key not in state or not parts:
            continue
        try:
            when = float(parts[1])
        except (IndexError, ValueError):
            when = now
        if now - when < MAX_AGE_SEC:
            state[key][parts[0]] = when
    return state


def _write(uid: str, state: dict[str, dict[str, float]]) -> None:
    lines = [f"{key}={pid} {int(at)}" for key, ids in state.items() for pid, at in ids.items()]
    path = _path(uid)
    if not lines:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n")
    os.replace(tmp, path)


def mark_gated(uid: str, post_id: str) -> None:
    state = _read(uid)
    state["GATED"][post_id] = time.time()
    _write(uid, state)


def gated(uid: str) -> set[str]:
    return set(_read(uid)["GATED"])


def first_reply(uid: str, root_id: str) -> bool:
    """Claim the thread for the one answer: False when it had one already."""
    state = _read(uid)
    if root_id in state["REPLIED"]:
        return False
    state["REPLIED"][root_id] = time.time()
    _write(uid, state)
    return True


def forget_reply(uid: str, root_id: str) -> None:
    """The answer was not posted: the next gated post there may try again."""
    state = _read(uid)
    if state["REPLIED"].pop(root_id, None) is not None:
        _write(uid, state)

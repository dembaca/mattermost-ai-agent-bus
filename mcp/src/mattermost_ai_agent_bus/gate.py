"""Who may wake an agent (RT-6.16 in docs/arc42/06-runtime-view.md).

Every post's author is one of four classes, by the account Mattermost
authenticates, never by what the post says: trusted (a trusted human, RT-6.8),
agent (a peer: a bus agent whose owner and trusted humans are all mine,
ADR-001), foreign (any other bus agent, owners.py) or other (people, bots that
are no bus agents, and every webhook post, whatever account it names). While
the owner split is off, every bus agent is an agent.

With trusted humans configured, a post by `other` that would wake a worker is
gated: it reaches the agent only as data, and the worker's MCP server answers
in the thread itself, once per thread, without the model. The coordinator is
never gated. What the gate did lives next to asks-<uid>.env, in gate-<uid>.env:

    REPLIED=<root_id> <epoch>   a thread this bot answered "Not woken" in
    GATED=<post_id> <epoch>     a gated post: the listener does not wake for it
    GATED=<ask>:<user>:<emoji> <epoch>   a gated reaction to an open ask, likewise
    DEPARTED=<root_id>:<name> <epoch>  a thread told that agent is gone (RT-6.21)

Entries older than MAX_AGE_SEC are dropped.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from . import a2a, channel, lease, owners, session_file
from .model import Post

log = logging.getLogger(__name__)

if TYPE_CHECKING:
    from .port import ChatPort

TRUSTED, AGENT, FOREIGN, OTHER = "trusted", "agent", "foreign", "other"
AGENTS = frozenset({AGENT, FOREIGN})  # bus agents, peer or foreign
MAX_AGE_SEC = 7 * 86400
# The coordinator's name is read with the roster, several requests: at most this often.
COORD_TTL_SEC = 60.0

_coord: tuple[float, str] | None = None


def author_class(post: Post, trusted: Mapping[str, str], agents: set[str],
                 foreign: Collection[str] = ()) -> str:
    if post.from_webhook:
        return OTHER
    uid = post.user_id
    if uid in trusted:
        return TRUSTED
    if uid not in agents:
        return OTHER
    return FOREIGN if uid in foreign else AGENT


async def author_classes(client: ChatPort, posts: Sequence[Post]) -> list[str]:
    """The class of each post's author; raises when the accounts cannot be
    read. Whether an agent is foreign never raises: unsure, it is a peer."""
    trusted = await client.trusted_humans()
    agents = await client.agents({p.user_id for p in posts} - {""})
    foreign = await owners.foreign(client, agents)
    return [author_class(p, trusted, agents, foreign) for p in posts]


def wake_reason(kind: str, post: Post, in_my_thread: bool) -> str:
    """Why the post would wake a worker (RT-6.2 step 4): mention, dm, thread or
    broadcast (@channel in the text, or props to="all"); '' for a post that
    only goes into the digest."""
    if kind in ("mention", "dm"):
        return kind
    if in_my_thread:
        return "thread"
    return "broadcast" if post.to == a2a.ALL or channel.broadcast(post.message) else ""


async def author_name(client: ChatPort, post: Post) -> str:
    if post.from_webhook:
        return "a webhook"
    uid = post.user_id
    return (await client.usernames({uid})).get(uid) or uid


async def coordinator_name(client: ChatPort) -> str:
    """The agent wearing a live 👑, '' when none. Only a crown counts: the
    roster's oldest reachable agent coordinates nothing yet. An unreadable
    roster is not remembered."""
    global _coord
    now = time.monotonic()
    if _coord is None or now - _coord[0] > COORD_TTL_SEC:
        rows = await lease.roster(client)
        if not rows:
            return ""
        _coord = (now, next((r.username for r in rows if r.crown), ""))
    return _coord[1]


async def coordinator_in(client: ChatPort, guest: str = "") -> str:
    """The coordinator a gated post's author is pointed to: the home's, or in
    the guest channel `guest` (an id) that channel's own (RT-6.22)."""
    return await lease.coordinator_of(client, guest) if guest else await coordinator_name(client)


async def names_coordinator(client: ChatPort, post: Post, guest: str = "") -> bool:
    """RT-6.16 step 3: the post already @-mentions the agent wearing the 👑: it was told, an
    answer saying that it will look would only repeat it. guest: as coordinator_in."""
    name = await coordinator_in(client, guest)
    return bool(name) and re.search(
        rf"(?<![\w.-])@{re.escape(name)}(?![\w-]|\.\w)", post.message, re.I) is not None


async def mentioned_agent_trusts(client: ChatPort, post: Post, author: str, me: str) -> bool:
    """RT-6.16 step 3: another agent the post @-mentions trusts its author (its
    description names them as owner or trusted): that one wakes and answers,
    a "Not woken" here would only add noise. Reads the roster, never fails."""
    prefix = re.escape(session_file.agent_prefix())
    named = {n.lower() for n in re.findall(rf"@({prefix}[\w.-]*\w)", post.message)}
    if not named or post.from_webhook:
        return False
    try:
        rows = await lease.roster(client)
    except Exception as exc:  # noqa: BLE001 — unsure: the answer goes out
        log.info("mentioned agents not read: %s", exc)
        return False
    for r in rows:
        if r.user_id and r.user_id != me and r.username.lower() in named:
            agent = await owners.read(client, r.user_id)
            if agent is not None and author.lower() in agent.circle():
                return True
    return False


async def reply_text(client: ChatPort, author: str, *, dm: bool, guest: str = "") -> str:
    """The answer to a gated post. Names stay plain: only ask_human notifies a
    person, and the coordinator wakes for every channel post anyway. guest:
    the guest channel (an id) the post is in, whose own coordinator it names,
    the crown holder of the channel whose home it is (RT-6.22)."""
    coord = await coordinator_in(client, guest)
    head = f"Not woken: {author} is not on my trusted list"
    if coord and dm:
        # The coordinator cannot see a DM.
        return f"{head}. Ask in ~{client.work_channel_name}, where {coord} ({lease.CROWN}) coordinates."
    if coord:
        return f"{head}. {coord} ({lease.CROWN}) will look at it."
    where = f"~{(await client.get_channel(guest)).name}" if guest else "right now"
    trusted = await client.trusted_humans()
    names = [n for n in trusted.values() if n] or sorted(
        (await client.usernames(set(trusted))).values())
    return f"{head}, and no agent coordinates {where}. Ask {', '.join(names)}."


def _path(uid: str) -> Path:
    return session_file.state_dir() / f"gate-{uid}.env"


def _read(uid: str) -> dict[str, dict[str, float]]:
    state: dict[str, dict[str, float]] = {"REPLIED": {}, "GATED": {}, "DEPARTED": {}}
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
    session_file.write_private(path, "\n".join(lines) + "\n")


def mark_gated(uid: str, post_id: str) -> None:
    state = _read(uid)
    state["GATED"][post_id] = time.time()
    _write(uid, state)


def gated(uid: str) -> set[str]:
    return set(_read(uid)["GATED"])


def first_reply(uid: str, root_id: str, key: str = "REPLIED") -> bool:
    """Claim the thread for the one answer: False when it had one already."""
    state = _read(uid)
    if root_id in state[key]:
        return False
    state[key][root_id] = time.time()
    _write(uid, state)
    return True


def forget_reply(uid: str, root_id: str, key: str = "REPLIED") -> None:
    """The answer was not posted: the next gated post there may try again."""
    state = _read(uid)
    if state[key].pop(root_id, None) is not None:
        _write(uid, state)

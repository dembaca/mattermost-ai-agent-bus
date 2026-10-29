"""Peer or foreign: whose agent may set this worker a task (ADR-001, ADR-003).

A bus agent B is a **peer** when its owner and every human it trusts are
trusted by this session: ``{owner(B)} ∪ trusts(B) ⊆ trusted(me)``. Every other
bus agent is **foreign**: its mention wakes the worker as a request, its
``TASK`` binds nothing, its broadcasts are gated (RT-6.16, RT-6.14).

The owner is B's native owner in the chat system where the bus can read it,
else ``for @<owner>`` from B's bot description (``agent_owner``, WP3); the
trusted list always comes from the description, which the registrar writes
(``for @<owner> · trusts @…``). Every MCP server reads both with its own bot
token through the chat port: the same in every harness. The split is active
only when this session has trusted humans and its own bot has an owner;
otherwise every agent is a peer, as before. An agent whose owner and
description cannot be read counts as a peer, with a log line: like the gate,
this fails open. One without an owner is foreign (``unbound`` too), and so is
a cut list (``+N``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from . import profile

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Owner:
    """What a bot's description says about whom it answers to."""

    owner: str  # lower case; '' when the description names none
    trusts: frozenset[str]
    cut: bool  # the registrar wrote only part of the trusted list (+N)

    def circle(self) -> set[str]:
        """Everyone the agent obeys: its owner and its trusted humans."""
        return ({self.owner} if self.owner else set()) | set(self.trusts)


def parse(description: str) -> Owner:
    return Owner(owner=profile.owner(description), trusts=frozenset(profile.listed(description)),
                 cut=profile.cut(description))


def is_peer(agent: Owner, mine: set[str]) -> bool:
    """ADR-001: I follow an agent only if I would follow everyone it obeys."""
    return bool(agent.owner) and not agent.cut and agent.circle() <= mine


def decide(native: str | None, description: str | None, user_id: str = "") -> str | None:
    """The agent's owner, lower case, from its native owner (`native`: the
    username the chat system keeps as the bot's owner; None when unreadable or
    not a human) and its description (ADR-003, WP3); '' for none, None when
    neither can be read.

    The registrar's own account is never a human owner: it owns every bot it
    did not bind, so an ``unbound`` description, and one the registrar wrote
    without ``for @``, name no owner whatever `native` says. A native owner
    that disagrees with ``for @`` makes no owner either (foreign), logged."""
    if native is None and description is None:
        return None
    claimed = profile.owner(description)
    if native is None or profile.unbound(description):
        return claimed
    native = native.lower()
    if claimed and claimed != native:
        log.warning("agent %s: native owner @%s but description for @%s: foreign",
                    user_id, native, claimed)
        return ""
    if not claimed and profile.by_registrar(description):
        return ""  # the registrar's account owns it
    return native


async def read(client: ChatPort, user_id: str) -> Owner | None:
    """The bot's owner (``agent_owner``) and trusted list (its description);
    None when neither can be read."""
    try:
        description = await client.bot_description(user_id)
        owner = await client.agent_owner(user_id)
    except Exception as exc:  # noqa: BLE001 — unreadable
        log.info("owner of %s not readable: %s", user_id, exc)
        return None
    if owner is None:
        return None
    return Owner(owner=owner, trusts=frozenset(profile.listed(description)),
                 cut=profile.cut(description))


async def trusted_names(client: ChatPort) -> set[str]:
    """The usernames of this session's trusted humans, lower case."""
    trusted = await client.trusted_humans()
    names = {n.lower() for n in trusted.values() if n}
    unnamed = {i for i, n in trusted.items() if not n}
    if unnamed:
        names |= {n.lower() for n in (await client.usernames(unnamed)).values() if n}
    return names


async def mine(client: ChatPort) -> tuple[set[str], str]:
    """My trusted humans' names and my bot's id while the owner split is
    active; (set(), '') while it is not: no trusted humans, or my own bot
    has no owner (unbound, say) or it cannot be read."""
    try:
        names = await trusted_names(client)
        if not names:
            return set(), ""
        me = (await client.get_me()).id
        own = await read(client, me)
    except Exception as exc:  # noqa: BLE001 — fail open: every agent a peer
        log.info("owner split off: %s", exc)
        return set(), ""
    if own is None or not own.owner:
        return set(), ""
    return names, me


async def foreign(client: ChatPort, agent_ids: Iterable[str]) -> dict[str, str]:
    """The foreign agents among `agent_ids` (bus agents), each with its owner
    ('' when its description names none); {} while the split is off. Never fails."""
    ids = set(agent_ids)
    if not ids:
        return {}
    names, me = await mine(client)
    if not names:
        return {}
    out: dict[str, str] = {}
    for uid in sorted(ids - {me}):
        agent = await read(client, uid)
        if agent is None:
            log.info("agent %s counts as a peer: its description is not readable", uid)
        elif not is_peer(agent, names):
            out[uid] = agent.owner
    return out

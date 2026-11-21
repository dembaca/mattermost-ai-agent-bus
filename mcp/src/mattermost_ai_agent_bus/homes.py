"""The agent's channels as the bus watches them, read through the port (arc42 §5.1).

The home, the lobby and the guest channels (ADR-002, RT-6.2 step 1, RT-6.22)
from a ChatPort's members. Each helper swallows what cannot be read and logs
it: a channel that does not resolve is left out, the others still count.
client.py re-exports them.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .config import env
from .ids import _ID

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)


async def watched_channel_ids(client: ChatPort) -> set[str]:
    """The channels the bus watches (RT-6.2 step 1): the work channel (the
    home), the project channel and the default channel (the lobby), by the
    ids the host passed (MM_PROJECT_CHANNEL_ID, MM_CHANNEL_ID) and by name,
    and the guest channels (RT-6.22: the bot's other team channels, where
    only a mention or a reply in its own thread wakes it). Never a DM or
    group channel. The one list for both sides: the inbox delivers their
    posts (websocket_inbox), the hook API counts and reads them (hookapi), so
    what the Stop hook counts is what wait_for_events can hand over. A
    channel that does not resolve is left out, the others still count."""
    ids = {v for v in (env("MM_PROJECT_CHANNEL_ID"), env("MM_CHANNEL_ID")) if _ID.fullmatch(v)}
    names = dict.fromkeys(n for n in (client.channel_name, client.project_channel_name) if n)
    for name in names:
        try:
            ids.add(await client.get_channel_id(name))
        except Exception as exc:  # noqa: BLE001 — a channel that is not there
            log.warning("could not resolve watched channel %r: %s", name, exc)
    try:
        ids.add(await client.work_channel_id())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not resolve the work channel: %s", exc)
    return ids | await guest_ids(client)


async def lobby_ids(client: ChatPort) -> set[str]:
    """The lobby (ADR-002, RT-6.22 step 3): MM_CHANNEL's id while it is not the
    home. None when it is the home (no project channel) or cannot be resolved:
    then its posts follow the home's rules, as before 0.63.0."""
    lobby = getattr(client, "lobby_channel_id", None)
    if lobby is None:  # a test's stand-in without a lobby
        return set()
    try:
        cid, home = await lobby(), await client.work_channel_id()
    except Exception as exc:  # noqa: BLE001
        log.warning("could not resolve the lobby: %s", exc)
        return set()
    return {cid} - {home, ""}


async def guest_ids(client: ChatPort) -> set[str]:
    """client.guest_channel_ids, none when the memberships cannot be read
    (RT-6.22): the home and the lobby still count."""
    guests = getattr(client, "guest_channel_ids", None)
    if guests is None:  # a test's stand-in without memberships
        return set()
    try:
        return set(await guests())
    except Exception as exc:  # noqa: BLE001
        log.warning("could not read the guest channels: %s", exc)
        return set()

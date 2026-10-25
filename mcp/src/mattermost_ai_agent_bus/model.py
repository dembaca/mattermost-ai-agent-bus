"""Bus types for what Mattermost keeps in its state carriers (arc42 §5.1).

Post props, custom status, unread counters and the profile description are
read and written in client.py, a2a.py and lease.py only; the rest of the
server gets these values instead. Posts themselves are still plain dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RosterEntry:
    """A reachable agent of the work channel (RT-6.6). status is a word:
    working, listening, idle, bus, or what the agent's status text says."""

    username: str
    status: str = ""
    coordinator: bool = False
    crown: bool = False


@dataclass(frozen=True)
class UnreadChannel:
    """A channel with something for this agent, to be read from `since` (ms).
    channel_type is Mattermost's, as the WebSocket names it too: D, G, O or P."""

    channel_id: str
    channel_type: str = "O"
    since: int = 0
    # The counters: mentions (in a DM or group: unread messages) and unread
    # posts. Not the channel's identity, so comparisons leave them out.
    pending: int = field(default=0, compare=False)
    unread: int = field(default=0, compare=False)

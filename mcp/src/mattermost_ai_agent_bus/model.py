"""Bus types for what Mattermost keeps in its state carriers (arc42 §5.1).

Post props, custom status, channel activity (unread counters, last post) and the profile description are
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
    """A channel of this bot, with when its newest post came (ms; 0 when
    Mattermost does not say) and Mattermost's counters. Which of its posts
    wait is the bus's own read watermark's to say (watermark.py, RT-6.2).
    channel_type is Mattermost's, as the WebSocket names it too: D, G, O or P."""

    channel_id: str
    channel_type: str = "O"
    # Not the channel's identity, so comparisons leave them out. The counters:
    # mentions (in a DM or group: unread messages) and unread posts.
    last_post_at: int = field(default=0, compare=False)
    pending: int = field(default=0, compare=False)
    unread: int = field(default=0, compare=False)

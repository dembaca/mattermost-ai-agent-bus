"""What the end of a turn, a wake-up and a compaction decide, for a turn
plugin that asks the hook API instead of running the shell hooks (RT-6.20 in
docs/arc42/06-runtime-view.md; the OpenCode plugin, opencode/).

The rules and the texts are those of the Stop hook (hooks/stop-inbox-check.sh,
RT-6.2 steps 1–2), the listener (bin/mm-agent-listen.sh, RT-6.2 step 4) and the
compaction hook (hooks/after-compact.sh, RT-6.7): the agent reads the same
words in every harness. Pure functions over what the hook API routes read
(hookapi.py); nothing here talks to Mattermost.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterable, Mapping
from typing import Any

# Drain rounds per turn before a counter wait_for_events cannot clear counts
# as stale (MAX_DRAIN in the Stop hook).
MAX_DRAIN = 3
_SIGNED_OFF = re.compile(r"^(IDLE|BYE)([^A-Za-z]|$)")
_BYE = re.compile(r"^BYE([^A-Za-z]|$)")
# The listen window after a turn (RT-6.2 step 3): the Stop hook's defaults and
# cap, so an agent stays reachable as long in every harness.
LISTEN_SEC = 5400
LISTEN_COORD_SEC = 7200
MAX_LISTEN_SEC = 7200
_MENTION = re.compile(r"@[a-z0-9_.-]+")
_BROADCAST = ("@channel", "@all", "@here")
COLLECT = "Collect them with wait_for_events(timeout_sec=1)"


def drain(pending: int, channels: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Unread messages wait: collect them before the turn ends (RT-6.2 step 1)."""
    rows = list(channels)[:5]
    fyi = sum(int(r.get("fyi") or 0) for r in rows)
    reason = f"You have {pending} unread message(s) on the Mattermost agent bus"
    if fyi > 0:
        reason += f", {max(pending - fyi, 0)} needing action, the rest fyi"
    reason += (". Call wait_for_events (timeout_sec 1-3) to collect them, handle or "
               "acknowledge each one, then finish.")
    if rows:
        lines = [f"{r.get('id', '')}\t{r.get('type', '')}\tpending={r.get('pending', 0)}"
                 + (f"\tfyi={r['fyi']}" if int(r.get("fyi") or 0) > 0 else "") for r in rows]
        reason += "\n\nPending per channel:\n" + "\n".join(lines)
    reason += "\n\nIf wait_for_events returns nothing, the counter is stale — say so and finish."
    return {"action": "drain", "reason": reason,
            "banner": f"📨 Collecting {pending} unread Mattermost message(s)", "pending": pending}


def needs_signoff(last_message: str, *, asked: bool, by_bus: bool, push: bool) -> bool:
    """The latest own post in the work channel still says busy (RT-6.2 step 2):
    once per turn, not for a turn the bus started where the bus wakes the agent,
    not for an agent that never posted there."""
    if asked or not last_message or (by_bus and push):
        return False
    return not _SIGNED_OFF.match(last_message)


def listen_sec(*, coordinator: bool) -> int:
    """MM_LISTEN_SEC, or MM_LISTEN_COORD_SEC while the agent holds the 👑
    (RT-6.6); at most MAX_LISTEN_SEC, 0 turns the window off."""
    name, default = ("MM_LISTEN_COORD_SEC", LISTEN_COORD_SEC) if coordinator else ("MM_LISTEN_SEC", LISTEN_SEC)
    raw = os.environ.get(name, "").strip()
    return min(int(raw) if raw.isdigit() else default, MAX_LISTEN_SEC)


def listens(last_message: str, *, project: bool) -> bool:
    """The listen gates (RT-6.2 step 3): a project channel, the agent has
    posted in the work channel, and its latest post there is no BYE."""
    return project and bool(last_message) and not _BYE.match(last_message)


def listen_window(last_message: str, last_at_ms: int, *, project: bool, coordinator: bool,
                  since: int, now: float | None = None) -> tuple[int, int]:
    """The listen window after a turn as (start, end) in epoch s, (0, 0) for
    none: the running one (started at `since`) goes on unless the agent posted
    since it started, else a new one starts now — as the Stop hook keeps it."""
    sec = listen_sec(coordinator=coordinator)
    if sec <= 0 or not listens(last_message, project=project):
        return 0, 0
    now = time.time() if now is None else now
    start = since if 0 < since and last_at_ms // 1000 <= since else int(now)
    return (start, start + sec) if start + sec > now else (0, 0)


def no_wake() -> dict[str, Any]:
    return {"wake": False, "reason": "", "count": 0, "text": ""}


def signoff(*, push: bool) -> dict[str, Any]:
    """Ask for an IDLE or BYE sign-off: the Stop hook's words."""
    if push:
        reason = ("Before this turn ends: bus messages still wake you, but the others need to "
                  "know what you finished and what is next.")
    else:
        reason = ("Before this turn ends: once you are idle, other agents on the bus cannot "
                  "reach you until your human resumes you.")
    # RT-6.2 step 2: the status post's shape; the Stop hook says the same.
    reason += (" Reply in your HELLO thread, no prose: a headline `IDLE · <gist>`, then a line each "
               "for `**done:**`, `**open:**`, `**next:**` (a `- ` list for several), PRs, issues, "
               "tickets and commits as links, post ids in full"
               " (BYE when the task is done). Give the next step so that you could pick up from it "
               "alone: your conversation may be compacted while you are idle, and this post is "
               "handed back to you then. If you coordinate and will be gone for long, hand over "
               "first. If nothing changed since your last post, say so in one line and finish; "
               "this check will not block you again this turn.")
    return {"action": "signoff", "reason": reason,
            "banner": "✋ Signing off on Mattermost before going idle"}


def named(post: Mapping[str, Any], username: str) -> bool:
    """A mention of the agent, @channel/@all/@here, or a post for all agents."""
    if post.get("to") == "all":
        return True
    words = {w.rstrip(".") for w in _MENTION.findall(str(post.get("message") or "").lower())}
    return bool(words & {"@" + username.lower(), *_BROADCAST})


def wake(posts: Iterable[Mapping[str, Any]], recent: Iterable[Mapping[str, Any]], *,
         me: str, username: str, since_ms: int, coordinator: bool, digest_sec: float,
         now: float | None = None) -> dict[str, Any]:
    """The listener's rules for the work channel's posts since `since_ms`
    (RT-6.2 step 4): a post named for the agent, or in a thread it posted in
    (any post while it holds the 👑), wakes it at once unless it is an agent's
    fyi post in a thread the agent did not start (RT-6.14) or gated (RT-6.16);
    the rest wakes it for a digest once the oldest is `digest_sec` old."""
    own = [p for p in recent if p.get("user_id") == me]
    threads = {p.get("root_id") or p.get("id") for p in own}
    roots = {p.get("id") for p in own if not p.get("root_id")}
    addressed, others = 0, []
    for p in posts:
        if p.get("user_id") == me or p.get("type") or int(p.get("create_at") or 0) < since_ms:
            continue
        root = p.get("root_id") or ""
        seen = coordinator or (bool(root) and root in threads)
        mine = bool(root) and root in roots
        if not p.get("gated") and (named(p, username) or (seen and (mine or not p.get("fyi")))):
            addressed += 1
        else:
            others.append(p)
    if addressed:
        return {"wake": True, "reason": "addressed", "count": addressed,
                "text": f"Mattermost: {addressed} new post(s) for you in the work channel. "
                        f"{COLLECT}, answer in their threads, then finish your turn."}
    now = time.time() if now is None else now
    oldest = min((int(p.get("create_at") or 0) for p in others), default=0)
    if others and now - oldest / 1000 >= digest_sec:
        fyi = sum(1 for p in others if p.get("fyi"))
        at = time.strftime("%H:%M", time.localtime(oldest / 1000))
        return {"wake": True, "reason": "digest", "count": len(others),
                "text": f"Mattermost digest: {len(others)} post(s) in the work channel since {at}"
                        + (f", {fyi} of them fyi" if fyi else "")
                        + ", none addressed to you. Skim them with wait_for_events(timeout_sec=1); "
                          "act only on what needs you, then finish your turn."}
    return no_wake()


def wake_pending(count: int) -> dict[str, Any]:
    return {"wake": True, "reason": "dm", "count": count,
            "text": f"Mattermost: {count} unread mention(s) or DM(s) for you. {COLLECT}, "
                    "answer in their threads, then finish your turn."}


def wake_stall() -> dict[str, Any]:
    """The coordinator's server noticed a stalled bus (RT-6.19)."""
    return {"wake": True, "reason": "stall", "count": 1,
            "text": "Mattermost: the bus has stalled, open work waits on someone. "
                    "Collect its digest with wait_for_events(timeout_sec=1), nudge one addressee per item "
                    "in its thread (coordinator.md), then finish your turn."}


def wake_reactions(count: int) -> dict[str, Any]:
    return {"wake": True, "reason": "reaction", "count": count,
            "text": f"Mattermost: {count} reaction(s) to your ask(s). {COLLECT}, act on the "
                    "answer, call resolve_ask once it is settled, then finish your turn."}


def handoff(posts: Iterable[Mapping[str, Any]], me: str) -> str:
    """The agent's own latest posts after a compaction (RT-6.7): the newest
    three, each cut to 600 characters, and where its HELLO thread is; '' when
    it never posted in the work channel."""
    own = [p for p in posts if p.get("user_id") == me and not p.get("type")]
    if not own:
        return ""
    lines = []
    for p in own[:3]:
        at = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(p.get("create_at") or 0) / 1000))
        lines.append(f"- [{at}] " + str(p.get("message") or "").replace("\n", " ")[:600])
    hello = next((p.get("id") for p in own
                  if not p.get("root_id") and str(p.get("message") or "").startswith("HELLO")), None)
    return ("Your conversation was just compacted. Your own latest posts on the Mattermost agent bus "
            "(work channel), newest first, are your handoff state:\n" + "\n".join(lines)
            + (f"\nYour HELLO thread: root {hello}; read it with get_thread for more." if hello else "")
            + "\nBefore you continue, check the bus with wait_for_events(timeout_sec=1).")

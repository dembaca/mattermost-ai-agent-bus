"""The coordinator notices a stall (RT-6.19 in docs/arc42/06-runtime-view.md).

A quiet bus wakes nobody, so a coordinator that only runs when woken never
sees work stall. The MCP server of the agent wearing the 👑 looks for it in its
heartbeat (lease.heartbeat, watch()): no model, no tokens while it is quiet.

A **stall** is something open — a task in open / active / blocked / review
(tasks.build, as coordinator("tasks") gives them, not one whose assignee said
WAITING: that wait is on purpose), an ask_human ask without its answer, or a
post in the work channel that @-mentions a reachable agent and has no reply
by that agent in its thread after it (not an agent's fyi post, not older than
a day) — and for MM_STALL_SEC (default 600 s) no post there by an agent or a
trusted human (the ask reminders of remind.py are none) and no agent with the
status `working`; the crown holder's own posts and status are none either (its
nudges would start a new quiet period), they only push the next wake to
MM_STALL_SEC after them. Without any activity in the window the quiet period
starts with the oldest open item. The crown holder's own asks within their
24 h are listed but are no reason to wake it: remind.py nudges their human, so
a stall whose only items they are wakes nobody. Then the server wakes its agent with an event
`{kind: "stall"}` whose text lists the items: what, who is next, the last
activity. The event goes into the inbox queue, so it reaches the agent like
any event: pushed as a channel tag (RT-6.12), from wait_for_events, and the
hooks' pending count (/v1/pending) includes it for the Stop hook's drain and
the listener.

Wakes in one quiet period back off: MM_STALL_SEC after the last activity,
then twice that after the wake, doubling, each gap capped at
MM_REMIND_MAX_SEC (8 h); any activity starts over. The server looks at every
heartbeat while it holds the crown (one read of the work channel, as
coordinator("tasks") makes), sooner when a wake is due. stall-<uid>.env in
the state dir keeps QUIET (epoch ms of the activity it counts from), WAKES,
NEXT (epoch of the next possible wake) and FROM (taking the crown, or a
restart with it, allows no wake for MM_STALL_SEC: the previous holder's
backoff is not on the bus), so a restart does not wake early. DMs
between other agents are theirs: the coordinator sees the work channel only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import a2a, asks, gate, lease, owners, remind, session_file, tasks
from .model import Post

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

KIND = "stall"
OPEN = ("open", "active", "blocked", "review")
ADDRESSED_KEEP_SEC = 86400
MAX_ITEMS = 8
LINE = 200
TITLE = 60
MAX_TEXT = 2000

# Set by server.py: queue an event for the agent (False: no inbox yet, try
# again at the next tick), and whether one still waits to be collected.
def _none_waits() -> bool:
    return False


sink: Callable[[str], bool] | None = None
waiting: Callable[[], bool] = _none_waits
_held = False  # whether this server held the crown at its last watch


def stall_sec() -> int:
    """Quiet time before a stall (MM_STALL_SEC, default 600 s, at least 60)."""
    return remind._sec("MM_STALL_SEC", 600)


def backoff(wakes: int) -> float:
    """The gap after the wakes so far in one quiet period: 10, 20, 40 … min, capped."""
    return min(stall_sec() * 2.0 ** min(wakes, 32), remind.remind_max_sec())


# --- what is open ------------------------------------------------------------------

def _at(p: Post) -> int:
    return p.create_at


def _hhmm(ms: int) -> str:
    return time.strftime("%H:%M", time.localtime(ms / 1000)) if ms else "–"


def quiet_since(posts: Sequence[Post], classes: list[str], uid: str = "") -> tuple[int, int]:
    """Epoch ms of the newest post by an agent or trusted human other than
    `uid` (the crown holder), and of the newest post by `uid`; reminders and
    system posts do not count. 0 without one."""
    counted = [p for p, c in zip(posts, classes, strict=True)
               if c != gate.OTHER and not p.type and not p.delete_at
               and p.kind != a2a.REMINDER]
    return (max((_at(p) for p in counted if p.user_id != uid), default=0),
            max((_at(p) for p in counted if uid and p.user_id == uid), default=0))


def _last(thread: Sequence[Post], names: Mapping[str, str]) -> tuple[int, str]:
    if not thread:
        return 0, ""
    p = max(thread, key=_at)
    who = names.get(p.user_id, "")
    return _at(p), f"{who} {p.kind or 'post'} {_hhmm(_at(p))}".strip()


def items(posts: Sequence[Post], names: Mapping[str, str], classes: list[str],
          rows: Sequence[Mapping[str, Any]], reachable: set[str], now_ms: int,
          trusted: Collection[str] = (), me: str = "",
          foreign: Collection[str] = ()) -> list[dict[str, Any]]:
    """The open items, longest quiet first: {what, id, title, next, last, at}.
    An ask of `me` (the crown holder) still within its 24 h is marked own: its
    reminders nudge the human, so it is listed but wakes nobody."""
    live = [p for p in posts if not p.type and not p.delete_at]
    by_thread: dict[str, list[Post]] = {}
    for p in live:
        by_thread.setdefault(p.thread, []).append(p)

    def who(p: Post) -> str:
        return names.get(p.user_id, "")

    out: list[dict[str, Any]] = []
    for r in rows:
        status = r.get("status", "open")
        if status not in OPEN:
            continue
        thread = by_thread.get(r["task"], [])
        agent = r.get("agent")
        said = [p.kind for p in sorted(thread, key=_at) if agent and who(p) == agent]
        said = [w for w in said if w and a2a.status(w)]
        if said and said[-1] == "WAITING":
            continue  # the assignee waits on purpose
        nxt = {"open": agent or "nobody: assign it", "active": agent,
               "blocked": r.get("by"), "review": r.get("by")}[status] or "–"
        at, last = _last(thread, names)
        out.append({"what": f"task {status}", "id": r["task"], "title": r.get("title", ""),
                    "next": nxt, "last": last, "at": at})

    by_id = {p.id: p for p in live}
    for a in tasks.open_asks(live, names, trusted, foreign):
        post = by_id.get(a["ask"])
        asked = _at(post) if post else 0
        at, last = _last(by_thread.get((post.root_id if post else "") or a["ask"], []), names)
        if asked and now_ms - asked >= asks.APPROVAL_SEC * 1000:
            what, nxt = "ask expired", f"{a.get('by', '–')}: resolve it, open a new ask"
        else:
            what, nxt = "ask", f"{a.get('human', '–')} (human; reminded automatically)"
        item = {"what": what, "id": a["ask"], "title": a.get("need", ""),
                "next": nxt, "last": last, "at": at}
        if what == "ask" and me and a.get("by") == me:
            item["own"] = True
        out.append(item)

    cls = {p.id: c for p, c in zip(posts, classes, strict=True)}
    for p in sorted(live, key=_at):
        pid = p.id
        kind = p.kind
        if (cls.get(pid, gate.OTHER) == gate.OTHER or now_ms - _at(p) > ADDRESSED_KEEP_SEC * 1000
                or kind in (a2a.ASK, a2a.PERMISSION, a2a.REMINDER)
                or (kind in ("TASK", "DELEGATE") and not p.root_id)
                or (cls[pid] in gate.AGENTS and p.intent == a2a.FYI)):
            continue
        thread = by_thread.get(p.root_id or pid, [])
        for agent in dict.fromkeys(tasks._mentions(p.message)):
            if agent == who(p) or agent not in reachable:
                continue
            if any(who(q) == agent and _at(q) > _at(p) for q in thread):
                continue
            out.append({"what": "post", "id": pid, "title": tasks.title(p.message),
                        "next": agent, "last": f"{who(p)} {_hhmm(_at(p))}", "at": _at(p)})
            break
    return sorted(out, key=lambda i: i["at"])


def digest(found: Sequence[Mapping[str, Any]], quiet_ms: int, now: float) -> str:
    """The stall event's text: one line per item, capped."""
    since = f" since {_hhmm(quiet_ms)}" if quiet_ms else ""
    quiet = round((now - quiet_ms / 1000) / 60) if quiet_ms else 0
    # The crown holder's own posts are no activity (its nudges would restart
    # the quiet period), so the text says whose posts it counts.
    head = (f"Stall: no post by another agent or a trusted human{since} ({quiet} min; "
            f"your own posts do not count), no agent working, {len(found)} open:"
            ) if quiet_ms else f"Stall: {len(found)} open, nobody working:"
    lines = [head]
    for i in found[:MAX_ITEMS]:
        t = str(i.get("title") or "")
        title = f' "{t[:TITLE - 1] + "…" if len(t) > TITLE else t}"' if t else ""
        lines.append(f"- {i['what']} {i['id']}{title} · next: {i['next']} · last: {i['last'] or '–'}"[:LINE])
    if len(found) > MAX_ITEMS:
        lines.append(f"- … and {len(found) - MAX_ITEMS} more (coordinator(\"tasks\"))")
    lines.append("Per item nudge exactly one addressee in its thread (coordinator.md); "
                 "if the wait is fine, do nothing.")
    text = "\n".join(lines)
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


# --- the state ---------------------------------------------------------------------

def _path(uid: str) -> Path:
    return session_file.state_dir() / f"stall-{uid}.env"


def _read(uid: str) -> dict[str, float]:
    out = {"QUIET": 0.0, "WAKES": 0.0, "NEXT": 0.0, "FROM": 0.0}
    try:
        for line in _path(uid).read_text().splitlines():
            k, _, v = line.partition("=")
            if k in out and v.replace(".", "", 1).isdigit():
                out[k] = float(v)
    except OSError:
        pass
    return out


def _save(uid: str, quiet_ms: int, wakes: int, nxt: float, frm: float) -> None:
    try:
        session_file.write_private(
            _path(uid), f"QUIET={quiet_ms}\nWAKES={wakes}\nNEXT={nxt:.3f}\nFROM={frm:.3f}\n")
    except OSError as exc:
        log.debug("stall state not saved: %s", exc)


def crowned(uid: str, now: float | None = None) -> None:
    """The server just took the crown (or restarted with it): the previous
    holder's backoff is not on the bus, so the first wake comes not before
    MM_STALL_SEC from now (FROM)."""
    st = _read(uid)
    _save(uid, int(st["QUIET"]), int(st["WAKES"]), st["NEXT"], (now or time.time()) + stall_sec())


# --- the check -----------------------------------------------------------------------

async def check(client: ChatPort, uid: str, now: float | None = None) -> float | None:
    """One look while holding the crown: wake the agent when the bus stalls.
    Seconds until the next wake could be due; None to look at the next tick.
    Times in the state are whole: QUIET in ms as Mattermost gives them."""
    now = now or time.time()
    now_ms = int(now * 1000)
    st = _read(uid)
    posts = await client.posts_after(tasks.since_ms(now))
    reactors = {r.user_id for p in posts for r in p.reactions}
    names = await client.usernames(({p.user_id for p in posts} | reactors | {uid}) - {""})
    classes = await gate.author_classes(client, posts)
    roster = await lease.roster(client)
    # The crown holder's own posts (its nudges) and own `working` (its wake
    # turn) start no quiet period: only someone else's activity does.
    quiet, own = quiet_since(posts, classes, uid)
    if any(r.status == "working" and r.username != names.get(uid) for r in roster):
        quiet = now_ms
    trusted = await client.trusted_humans()
    untrusted: set[str] = set()
    if trusted:
        untrusted = {p.id for p, c in zip(posts, classes, strict=True)
                     if c in (gate.OTHER, gate.FOREIGN) and not p.root_id}
    rows = tasks.build(posts, names, now_ms=now_ms, untrusted=untrusted,
                       coordinators={r.username for r in roster if r.coordinator})
    # A foreign agent's ✅ keeps an ask open (RT-6.11, ADR-001).
    foreign = await owners.foreign(client, tasks.ask_closers(posts)) if trusted else {}
    found = items(posts, names, classes, rows, {r.username for r in roster}, now_ms, trusted,
                  me=names.get(uid, ""), foreign=set(foreign))
    if not quiet:  # nothing that counts in the window: the saved start, else the oldest open item
        quiet = int(st["QUIET"]) or min((i["at"] for i in found if i["at"]), default=now_ms)
    wakes, nxt = int(st["WAKES"]), st["NEXT"]
    if quiet > st["QUIET"]:
        wakes, nxt = 0, quiet / 1000 + stall_sec()  # activity: a new quiet period
    else:
        quiet = int(st["QUIET"])
    # Own posts and a fresh crown only push the next wake out; the backoff goes on.
    nxt = max(nxt, own / 1000 + stall_sec() if own else 0.0, st["FROM"])
    if all(i.get("own") for i in found):
        # Nothing open, or only the crown holder's own asks: their reminders
        # already nudge the human, a wake would find nothing to do.
        _save(uid, quiet, wakes, nxt, st["FROM"])
        return None
    if nxt > now:
        _save(uid, quiet, wakes, nxt, st["FROM"])
        return nxt - now
    if not waiting() and (sink is None or not sink(digest(found, quiet, now))):
        return None  # nobody to wake yet: look again at the next tick
    wakes += 1
    _save(uid, quiet, wakes, now + backoff(wakes), st["FROM"])
    return backoff(wakes)


async def watch(client: ChatPort) -> float | None:
    """The heartbeat's watch (lease.heartbeat): the own asks' reminders, and
    with the crown the stall check. Seconds until one of them is due next;
    never raises."""
    try:
        uid = await lease._user_id(client)
    except Exception as exc:  # noqa: BLE001
        log.debug("watch: %s", exc)
        return None
    global _held
    held = lease.crown_held(uid)
    if held and not _held:
        crowned(uid)
    _held = held
    waits: list[float] = []
    for name, step in (("reminders", remind.tick), ("stall", check)):
        if step is check and not held:
            continue
        try:
            got = await step(client, uid)
        except Exception as exc:  # noqa: BLE001 — try again at the next tick
            log.debug("%s: %s", name, exc)
            continue
        if got is not None:
            waits.append(got)
    return min(waits, default=None)

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
starts with the oldest open item. An ask within its 24 h whose asking agent is
still on the roster (the crown holder's own too) is listed but is no reason to
wake it: that agent's remind.py nudges the human, the coordinator would find
nothing to do, so a stall whose only items they are wakes nobody (RT-6.19
step 2). A task whose assignee has such an ask open waits on that human as
well: listed with the ask, no reason to wake either; an answered ask shields
nothing. An ask expired, or whose agent left (no reminders any more), still
wakes it, and so does any other item: a question to the 👑 (RT-6.25) too. Then the server wakes its agent with an event
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

from . import a2a, asks, gate, lease, mandate, owners, remind, session_file, tasks
from .model import Mark, Post

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
    An ask still within its 24 h by `me` (the crown holder) or by an agent in
    `reachable` is marked reminded: that agent's server reminds the human
    (remind.py), so it is listed but wakes nobody (RT-6.19 step 2). So is a
    task whose assignee has such an ask open: it waits on that human."""
    live = [p for p in posts if not p.type and not p.delete_at]
    by_thread: dict[str, list[Post]] = {}
    for p in live:
        by_thread.setdefault(p.thread, []).append(p)

    def who(p: Post) -> str:
        return names.get(p.user_id, "")

    out: list[dict[str, Any]] = []
    on_human: dict[str, dict[str, Any]] = {}  # agent → its reminded ask
    by_id = {p.id: p for p in live}
    for a in tasks.open_asks(live, names, trusted, foreign):
        post = by_id.get(a["ask"])
        asked = _at(post) if post else 0
        at, last = _last(by_thread.get((post.root_id if post else "") or a["ask"], []), names)
        by = a.get("by", "")
        reminded = bool(by) and (by == me or by in reachable)
        if asked and now_ms - asked >= asks.APPROVAL_SEC * 1000:
            what, nxt = "ask expired", f"{by or '–'}: resolve it, open a new ask"
        elif a.get("question"):
            # RT-6.25 step 7: a question to the 👑 wakes it; its human is
            # asked by the asking agent's server once the wait is over.
            what, nxt = "question", f"{a.get('human', '–')} ({a2a.symbol(Mark.CROWN)}: answer it, or ask the human)"
            reminded = False
        elif reminded:
            what, nxt = "ask", f"{a.get('human', '–')} (human; reminded automatically)"
        else:
            what, nxt = "ask", f"{a.get('human', '–')} (human; {by or 'its agent'} left: not reminded)"
        item = {"what": what, "id": a["ask"], "title": a.get("need", ""),
                "next": nxt, "last": last, "at": at}
        if what == "ask" and reminded:
            item["reminded"] = True
            on_human.setdefault(by, a)
        out.append(item)

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
        item = {"what": f"task {status}", "id": r["task"], "title": r.get("title", ""),
                "next": nxt, "last": last, "at": at}
        if agent and (waits := on_human.get(agent)):
            # Its assignee waits on a human's answer, reminded by its server.
            item["next"] = f"{waits.get('human', '–')} (ask {waits['ask']} of {agent}; reminded automatically)"
            item["reminded"] = True
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


def digest(found: Sequence[Mapping[str, Any]], quiet_ms: int, now: float,
           guests: Sequence[str] = ()) -> str:
    """The stall event's text: one line per item, capped. `guests`: the mandated
    guest channels among them; their items are marked `~channel`."""
    since = f" since {_hhmm(quiet_ms)}" if quiet_ms else ""
    quiet = round((now - quiet_ms / 1000) / 60) if quiet_ms else 0
    # The crown holder's own posts are no activity (its nudges would restart
    # the quiet period), so the text says whose posts it counts.
    head = (f"Stall: no post by another agent or a trusted human{since} ({quiet} min; "
            f"your own posts do not count), no agent working, {len(found)} open:"
            ) if quiet_ms else f"Stall: {len(found)} open, nobody working:"
    if guests:  # the quiet times differ per channel: none in the head
        head = (f"Stall: {len(found)} open, items marked ~channel are in the guest channels of "
                f"your mandate ({', '.join('~' + g for g in guests)}), the others in your home:")
    lines = [head]
    for i in found[:MAX_ITEMS]:
        t = str(i.get("title") or "")
        title = f' "{t[:TITLE - 1] + "…" if len(t) > TITLE else t}"' if t else ""
        where = f"~{i['channel']}: " if i.get("channel") else ""
        lines.append(f"- {where}{i['what']} {i['id']}{title} · next: {i['next']} · last: {i['last'] or '–'}"[:LINE])
    if len(found) > MAX_ITEMS:
        lines.append(f"- … and {len(found) - MAX_ITEMS} more (coordinator(\"tasks\"))")
    lines.append("Per item nudge exactly one addressee in its thread (coordinator.md), "
                 "in the channel the item is from; if the wait is fine, do nothing."
                 if guests else
                 "Per item nudge exactly one addressee in its thread (coordinator.md); "
                 "if the wait is fine, do nothing.")
    text = "\n".join(lines)
    return text if len(text) <= MAX_TEXT else text[: MAX_TEXT - 1] + "…"


# --- the state ---------------------------------------------------------------------

_KEYS = ("QUIET", "WAKES", "NEXT", "FROM")


def _path(uid: str) -> Path:
    return session_file.state_dir() / f"stall-{uid}.env"


def _raw(uid: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in _path(uid).read_text().splitlines():
            k, _, v = line.partition("=")
            if k.partition("@")[0] in _KEYS and v.replace(".", "", 1).isdigit():
                out[k] = v
    except OSError:
        pass
    return out


def _read(uid: str, chan: str = "") -> dict[str, float]:
    """The saved state of the home, or of the mandated guest channel `chan`
    (its keys carry `@<channel id>`: one file per bot, as before)."""
    raw, sfx = _raw(uid), f"@{chan}" if chan else ""
    return {k: float(raw.get(k + sfx, 0.0)) for k in _KEYS}


def _save(uid: str, quiet_ms: int, wakes: int, nxt: float, frm: float, chan: str = "") -> None:
    raw, sfx = _raw(uid), f"@{chan}" if chan else ""
    raw.update({"QUIET" + sfx: str(quiet_ms), "WAKES" + sfx: str(wakes), "NEXT" + sfx: f"{nxt:.3f}"})
    if not chan:  # a guest inherits the home's FROM (a fresh crown, RT-6.19 step 4)
        raw["FROM"] = f"{frm:.3f}"
    _write(uid, raw)


def _write(uid: str, raw: dict[str, str]) -> None:
    try:
        session_file.write_private(_path(uid), "".join(f"{k}={v}\n" for k, v in sorted(raw.items())))
    except OSError as exc:
        log.debug("stall state not saved: %s", exc)


def _forget(uid: str, keep: Collection[str]) -> None:
    """Drop the saved state of guest channels no longer mandated (RT-6.19 step 6)."""
    raw = _raw(uid)
    kept = {k: v for k, v in raw.items() if "@" not in k or k.partition("@")[2] in keep}
    if kept != raw:
        _write(uid, kept)


def crowned(uid: str, now: float | None = None) -> None:
    """The server just took the crown (or restarted with it): the previous
    holder's backoff is not on the bus, so the first wake comes not before
    MM_STALL_SEC from now (FROM)."""
    st = _read(uid)
    _save(uid, int(st["QUIET"]), int(st["WAKES"]), st["NEXT"], (now or time.time()) + stall_sec())


# --- the check -----------------------------------------------------------------------

class _Look:
    """What one channel shows at a check: its open items and its place in
    the quiet period (RT-6.19 steps 2 to 4)."""

    def __init__(self, uid: str, chan: str, name: str, found: list[dict[str, Any]],
                 quiet: int, wakes: int, nxt: float, frm: float) -> None:
        self.uid, self.chan, self.name, self.found = uid, chan, name, found
        self.quiet, self.wakes, self.nxt, self.frm = quiet, wakes, nxt, frm

    def save(self) -> None:
        _save(self.uid, self.quiet, self.wakes, self.nxt, self.frm, self.chan)


async def _look(client: ChatPort, uid: str, now: float, chan: str = "", name: str = "",
                frm: float = 0.0) -> _Look | None:
    """One channel: the home (`chan` empty) or a guest where this coordinator's
    mandate stands (step 6). None for a guest whose home has a live crown of
    its own: that coordinator looks first, this one only steps in when it is gone."""
    now_ms = int(now * 1000)
    roster = await lease.roster_in(client, chan, name) if chan else await lease.roster(client)
    if chan and lease.crown_in(roster):
        return None
    st = _read(uid, chan)
    posts = await (client.posts_after(tasks.since_ms(now), channel_id=chan) if chan
                   else client.posts_after(tasks.since_ms(now)))
    reactors = {r.user_id for p in posts for r in p.reactions}
    names = await client.usernames(({p.user_id for p in posts} | reactors | {uid}) - {""})
    classes = await gate.author_classes(client, posts)
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
    if chan:
        for i in found:
            i["channel"] = name
    if not quiet:  # nothing that counts in the window: the saved start, else the oldest open item
        quiet = int(st["QUIET"]) or min((i["at"] for i in found if i["at"]), default=now_ms)
    wakes, nxt = int(st["WAKES"]), st["NEXT"]
    if quiet > st["QUIET"]:
        wakes, nxt = 0, quiet / 1000 + stall_sec()  # activity: a new quiet period
    else:
        quiet = int(st["QUIET"])
    # Own posts and a fresh crown only push the next wake out; the backoff goes on.
    nxt = max(nxt, own / 1000 + stall_sec() if own else 0.0, frm)
    return _Look(uid, chan, name, found, quiet, wakes, nxt, frm)


async def check(client: ChatPort, uid: str, now: float | None = None) -> float | None:
    """One look while holding the crown: wake the agent when the bus stalls,
    in the home or in a guest channel where its mandate stands (RT-6.26 step 9).
    Seconds until the next wake could be due; None to look at the next tick.
    Times in the state are whole: QUIET in ms as Mattermost gives them. One
    event for all channels that are due; each keeps its own quiet period."""
    now = now or time.time()
    frm = _read(uid)["FROM"]
    looks = [lk for lk in [await _look(client, uid, now, frm=frm)] if lk is not None]
    try:
        guests, unsure = await mandate.held(client, now)
        # Only a guest whose mandate is known to be gone loses its state: one
        # not judged this time (a failed read) keeps it (RT-6.19 step 6).
        _forget(uid, {g["id"] for g in guests} | unsure)
    except Exception as exc:  # noqa: BLE001 — the home alone, nothing forgotten
        log.info("stall: mandated guests not read: %s", exc)
        guests = []
    for g in guests:
        try:
            if (lk := await _look(client, uid, now, g["id"], g["name"], frm)) is not None:
                looks.append(lk)
        except Exception as exc:  # noqa: BLE001 — that guest is left out, its state kept
            log.info("stall: ~%s not read: %s", g["name"], exc)
    waits: list[float] = []
    due: list[_Look] = []
    for lk in looks:
        if all(i.get("reminded") for i in lk.found):
            # RT-6.19 step 2: nothing open, or only asks their agents' servers
            # remind and tasks whose assignee waits on such an ask: the human
            # is nudged already, a wake would find nothing to do. The quiet period and backoff are kept, so a later item that
            # needs an agent wakes as if these had not been there.
            lk.save()
        elif lk.nxt > now:
            lk.save()
            waits.append(lk.nxt - now)
        else:
            due.append(lk)
    if not due:
        return min(waits, default=None)
    text = digest([i for lk in due for i in lk.found], min(lk.quiet for lk in due), now,
                  [lk.name for lk in due if lk.chan])
    if not waiting() and (sink is None or not sink(text)):
        return None  # nobody to wake yet: look again at the next tick
    for lk in due:
        lk.wakes += 1
        gap = backoff(lk.wakes)
        _save(uid, lk.quiet, lk.wakes, now + gap, lk.frm, lk.chan)
        waits.append(gap)
    return min(waits)


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
    # RT-6.26 step 1: the home's pinned posts read again for a mandate.
    for name, step in (("reminders", remind.tick), ("mandate", mandate.tick), ("stall", check)):
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

"""Mandate for a role (RT-6.26 in docs/arc42/06-runtime-view.md; ADR-006 phase 1).

A trusted human pins a post in a home whose first line is
`mandate: 👑 of ~<home> [until <YYYY-MM-DD>]` (any case, in backticks too):
in this home the holder of the crown of ~<home> gives tasks and priorities
for them. The pin only finds the post; its author's account id decides,
against this session's own trusted humans (ADR-001). Never a webhook's,
a bot's or an agent's post, never a reply (step 2).

With the registrar's keys pinned (MM_REGISTRAR_KEYS, ADR-006 phase 2, step 8)
only the registrar's signed record counts, which a trusted human sets with
`/agent-mandate` (`off` removes it): a pinned post mandates nothing, and the
coordinator's server answers a trusted human's one with how it is set (observe).
Without pinned keys, as follows.

Each server reads its home's pinned posts at its first look and then every
REFRESH_SEC on the heartbeat (stall.watch), one read each, kept in memory;
a failed read keeps the last good one, which still ends at its expiry. The
newest qualifying post wins, by its time: its last edit, else its creation.
It ends DEFAULT_DAYS after that time; `until` (the start of that day, UTC)
never later than MAX_DAYS after it. Deleted, edited away or unpinned, it is
gone at the next read; pinned again before its expiry, it is back (step 3).

The role resolves when an event arrives (judge, step 4): the live 👑 of
~<home> (lease.coordinator_of; where that home cannot be read, the
ambassador's status in this home's roster), and only a peer whose owner and
trusted humans are all this session's (owners.is_peer, the split active).
Its TASK root in the home, or its reply in a TASK's thread there, posted
within the mandate's time, is marked with the mandate post's id. It approves
nothing: its author stays an agent, never trusted (RT-6.8, RT-6.11).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from . import a2a, attest, gate, lease, owners
from .model import Mark, NoMandateEndpoint, Post

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

REFRESH_SEC = 120
# A registrar's record that could not be read again counts for this many
# refresh periods after the last read that went through (step 8); a pinned
# post's last read holds to its expiry (step 1).
STALE_PERIODS = 5
DEFAULT_DAYS, MAX_DAYS = 7, 30
DAY_MS = 86_400_000
TASK = "TASK"
JUDGED_MAX = 5_000  # task roots judged once (step 5); the oldest go first

_LINE = re.compile(rf"`?mandate:\s*{a2a.mandate_crown()}\s+of\s+~([a-z0-9][a-z0-9_-]{{0,63}})"
                   r"(?:\s+until\s+(\S+?))?`?[.!]?", re.I)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


SIGNED = "registrar"
POST = "post"


@dataclass(frozen=True)
class Mandate:
    post: str      # the mandate post's id; `registrar-<set_at_ms>` for a signed record
    home: str      # the named home: the role is the crown of ~home
    home_id: str   # its channel id at the last read, '' when not found
    by: str        # the trusted human's username
    by_id: str
    at_ms: int     # its last edit, else its creation; a record's set_at_ms
    until_ms: int  # when it ends
    source: str = POST  # POST (a pinned post) or SIGNED (the registrar's record)
    note: str = ""

    @property
    def role(self) -> str:
        if self.source == SIGNED:
            return f"coordinator of ~{self.home}"
        return f"{a2a.symbol(Mark.CROWN)} of ~{self.home}"


@dataclass(frozen=True)
class _Read:
    mandate: Mandate | None
    why: str  # why none holds, '' with one
    at: float = 0.0  # when the registrar's record was last read (step 8), 0 for a post


_reads: dict[str, _Read] = {}     # home id -> the last good read
_fetched: dict[str, float] = {}   # home id -> when this process last tried
_judged: dict[str, str] = {}      # TASK root id -> mandate post id, '' none
_noted: set[tuple[str, ...]] = set()


def _note(key: tuple[str, ...], msg: str, *args: object) -> None:
    if key not in _noted:
        _noted.add(key)
        log.info(msg, *args)


def parse(message: str) -> tuple[str, str] | None:
    """The named home (lower case) and the raw `until` ('' none) of a post
    whose first line is a mandate; None for anything else."""
    first = (message or "").strip().split("\n", 1)[0].strip()
    m = _LINE.fullmatch(first)
    return (m[1].lower(), m[2] or "") if m else None


def until_ms(at_ms: int, until: str) -> int | None:
    """When a mandate of that time ends (step 3): DEFAULT_DAYS later, or at
    the start of `until` (UTC), never past MAX_DAYS; None for no date."""
    if not until:
        return at_ms + DEFAULT_DAYS * DAY_MS
    if not _DATE.fullmatch(until):
        return None
    try:
        day = datetime.strptime(until, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return min(int(day.timestamp() * 1000), at_ms + MAX_DAYS * DAY_MS)


def _iso(ms: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%MZ", time.gmtime(ms / 1000))


def _at(post: Post) -> int:
    return post.edit_at or post.create_at


async def _newest(client: ChatPort, home: str, pinned: Sequence[Post]) -> _Read:
    """Step 2: the newest pinned post that is a mandate by a trusted human of
    this session (by account id), top-level in the home, no webhook's, no
    bot's, no agent's."""
    trusted = await client.trusted_humans()
    if not trusted:
        return _Read(None, "no trusted humans: no mandate counts")
    found = [(p, got) for p in pinned if (got := parse(p.message)) is not None]
    if not found:
        return _Read(None, "none pinned in this home")
    agents = await client.agents({p.user_id for p, _ in found} - {""})
    named = await client.usernames({p.user_id for p, _ in found if p.user_id in trusted})
    ok: list[tuple[Post, tuple[str, str], str]] = []
    for p, got in found:
        who = trusted.get(p.user_id) or named.get(p.user_id, "")
        if (p.channel_id != home or p.root_id or p.type or p.from_webhook or p.delete_at
                or p.user_id not in trusted or p.user_id in agents or not who
                or await client.bots_named({who})):
            _note(("ignored", p.id), "mandate post %s ignored: not a trusted human's "
                  "top-level post in the home", p.id)
            continue
        ok.append((p, got, who))
    if not ok:
        return _Read(None, "no pinned mandate by a trusted human of this session")
    p, (name, until), who = max(ok, key=lambda o: _at(o[0]))
    end = until_ms(_at(p), until)
    if end is None:
        _note(("until", p.id), "mandate post %s: until %r is no date: none holds", p.id, until)
        return _Read(None, f"mandate post {p.id}: until is no date")
    try:
        home_id = await client.get_channel_id(name)
    except Exception as exc:  # noqa: BLE001 — a renamed or missing home: no holder
        _note(("home", name), "mandate: ~%s not found, nobody holds its crown: %s", name, exc)
        home_id = ""
    return _Read(Mandate(post=p.id, home=name, home_id=home_id, by=who, by_id=p.user_id,
                         at_ms=_at(p), until_ms=end), "")


def signed() -> bool:
    """Whether the registrar's keys are pinned (MM_REGISTRAR_KEYS): then only
    its signed record mandates (step 8), else a trusted human's pinned post."""
    return bool(attest.pinned())


async def _signed(client: ChatPort, home: str, old: _Read | None, now: float) -> _Read | None:
    """Step 8: the registrar's record of the home, verified against the pinned
    keys. A record set by a trusted human of this session (by id: authority from
    trust, ADR-001) is the mandate; none set (404, also after `off`) means none;
    one set by someone else, none. None keeps the last good read: a failed fetch,
    a record that does not verify, one older than the kept."""
    try:
        token = await client.mandate_record(home)
    except NoMandateEndpoint:
        _note(("endpoint", home), "mandate: the registrar has no mandate endpoint (a 404 "
              "without `no mandate set`: older than 0.6.0?): the last record holds for a while")
        return None
    except Exception as exc:  # noqa: BLE001 — the last good read holds, for a while
        log.info("mandate: the registrar's record not read: %s", exc)
        return None
    if token is None:
        return _Read(None, "none set with /agent-mandate (the registrar's keys are pinned: "
                           "a pinned post mandates nothing)", at=now)
    record, why = attest.verify_mandate(token, attest.pinned(), channel_id=home)
    if record is None:
        _note(("refused", home, why), "mandate: the registrar's record for %s refused: %s", home, why)
        return None
    trusted = await client.trusted_humans()
    if not trusted:
        return _Read(None, "no trusted humans: no mandate counts", at=now)
    if record.set_by_id not in trusted:
        _note(("untrusted", home, record.set_by_id, str(record.set_at_ms)),
              "mandate by %s ignored: not a trusted human of this session", record.set_by)
        return _Read(None, f"set by {record.set_by}, not a trusted human of this session",
                     at=now)
    if (old is not None and old.mandate is not None and old.mandate.source == SIGNED
            and old.mandate.at_ms > record.set_at_ms):
        return None  # older than the kept record: a replay (it does not renew the kept one)
    end = until_ms(record.set_at_ms, record.until)
    if end is None:  # the record's shape allows a date that is none, `2026-13-01`
        _note(("until", home, record.until), "mandate record: until %r is no date", record.until)
        return _Read(None, f"mandate record: until {record.until!r} is no date", at=now)
    try:
        name = (await client.get_channel(record.home)).name
    except Exception as exc:  # noqa: BLE001 — not readable: the id names it
        _note(("home", record.home), "mandate: the home %s not read: %s",
              record.home, exc)
        name = record.home
    return _Read(Mandate(post=f"registrar-{record.set_at_ms}", home=name,
                         home_id=record.home, by=record.set_by, by_id=record.set_by_id,
                         at_ms=record.set_at_ms, until_ms=end, source=SIGNED,
                         note=record.note), "", at=now)


async def refresh(client: ChatPort, now: float | None = None, channel: str = "") -> bool:
    """Read the home's mandate once (`channel`: a guest channel's, step 9): the
    registrar's record with keys pinned, else its pinned posts, and keep it (or
    why none holds). Whether the read went through; never raises: a failed one
    keeps the last good result, which still ends at its expiry."""
    now = time.time() if now is None else now
    try:
        home = channel or await client.work_channel_id()
    except Exception as exc:  # noqa: BLE001 — no home: no mandate
        log.info("mandate: no home: %s", exc)
        return False
    _fetched[home] = now
    old = _reads.get(home)
    try:
        got = (await _signed(client, home, old, now) if signed()
               else await _newest(client, home, await client.pinned_posts(home)))
    except Exception as exc:  # noqa: BLE001 — the last good read holds, to its expiry
        log.info("mandate: not read: %s", exc)
        return False
    if got is None:
        return False
    _reads[home] = got
    m = got.mandate
    if m is not None and (old is None or old.mandate != m):
        log.info("mandate %s by %s (post %s) until %s", m.role, m.by, m.post, _iso(m.until_ms))
    elif m is None and old is not None and old.mandate is not None:
        log.info("mandate %s ended: %s", old.mandate.role, got.why)
    return True


async def current(client: ChatPort, now: float | None = None,
                  channel: str = "") -> tuple[Mandate | None, str]:
    """The home's mandate now (`channel`: a guest channel's, step 9), read at
    the first look; else (None, why). An expired one counts as none, logged once."""
    now = time.time() if now is None else now
    try:
        home = channel or await client.work_channel_id()
    except Exception as exc:  # noqa: BLE001 — no home: no mandate
        return None, f"no home: {exc}"
    if home not in _fetched:
        await refresh(client, now, channel)
    got = _reads.get(home)
    if got is None:
        return None, "the home's pinned posts not read yet"
    m = got.mandate
    if m is None:
        return None, got.why
    if now * 1000 >= m.until_ms:
        _note(("expired", m.post, str(m.until_ms)), "mandate %s by %s (post %s) expired at %s",
              m.role, m.by, m.post, _iso(m.until_ms))
        return None, f"mandate post {m.post} expired at {_iso(m.until_ms)}"
    if m.source == SIGNED and now - got.at > STALE_PERIODS * REFRESH_SEC:
        _note(("stale", m.post, str(got.at)), "mandate %s by %s: the registrar's record was not "
              "read for %d min: none holds until it is", m.role, m.by, int((now - got.at) // 60))
        return None, (f"the registrar's record was not read for "
                      f"{int((now - got.at) // 60)} min: none holds until it is")
    return m, ""


async def held(client: ChatPort, now: float | None = None) -> list[dict[str, str]]:
    """Step 9, the holder's side: the guest channels (`id`, `name`) where a
    mandate names this session's own home, so its coordinator watches them
    (stall.check). Each guest's record is read as that home's server reads its
    own (refresh, REFRESH_SEC apart, kept in memory) and counts under the same
    rules: verified, set by a trusted human of this session, not expired. A
    guest that cannot be read, or whose mandate names another home, is left
    out. Never raises."""
    now = time.time() if now is None else now
    try:
        home_id = await client.work_channel_id()
        mine = client.work_channel_name.lower()
        guests = getattr(client, "guest_channel_ids", None)
        ids = sorted(set(await guests())) if guests is not None else []
    except Exception as exc:  # noqa: BLE001 — no guests known: none held
        log.info("mandate: guest channels not read: %s", exc)
        return []
    out: list[dict[str, str]] = []
    for cid in ids:
        if cid == home_id:
            continue
        try:
            fetched = _fetched.get(cid)
            if fetched is None or now - fetched >= REFRESH_SEC:
                await refresh(client, now, cid)
            m, _ = await current(client, now, cid)
            if m is None or not (m.home_id == home_id or (not m.home_id and m.home.lower() == mine)):
                continue
            out.append({"id": cid, "name": (await client.get_channel(cid)).name or cid})
        except Exception as exc:  # noqa: BLE001 — that guest is left out
            log.info("mandate: guest %s not judged: %s", cid, exc)
    return out


async def holder(client: ChatPort, m: Mandate) -> tuple[str, str]:
    """Step 4: the username of the live 👑 of ~<home> and '' or why there is
    none: lease.coordinator_of over that home; where it cannot be read, the
    ambassador of ~<home> in this home's roster (its own status)."""
    if not m.home_id:
        return "", f"~{m.home} not found"
    try:
        name = await lease.coordinator_of(client, m.home_id)
    except Exception as exc:  # noqa: BLE001 — that home unreadable: the status counts
        log.info("mandate: ~%s not read, its holder from the roster here: %s", m.home, exc)
        name = next((r.username for r in await lease.roster(client) if r.crown_of == m.home), "")
    return name, "" if name else f"the crown of ~{m.home} is not held"


async def _peer(client: ChatPort, user_id: str) -> bool:
    """The holder is a peer (ADR-001): owners.peer."""
    return await owners.peer(client, user_id)


async def _in_task(client: ChatPort, post: Post, home: str) -> bool:
    """A TASK root, or a reply in the thread of a TASK root in the home that
    is a task (by a trusted human or a peer, RT-6.14 step 3)."""
    if not post.root_id:
        return post.kind == TASK
    root = next((q for q in (await client.thread(post.root_id)).posts if q.id == post.root_id),
                None)
    if (root is None or root.root_id or root.kind != TASK or root.channel_id != home
            or root.delete_at):
        return False
    return (await gate.author_classes(client, [root]))[0] in (gate.TRUSTED, gate.AGENT)


async def _judge(client: ChatPort, posts: Sequence[Post], now: float) -> dict[str, tuple[str, str]]:
    m, _ = await current(client, now)
    if m is None:
        return {}
    home = await client.work_channel_id()
    name, _ = await holder(client, m)
    if not name:
        return {}
    names = await client.usernames({p.user_id for p in posts} - {""})
    mine = [p for p in posts if names.get(p.user_id, "").lower() == name.lower()]
    if not mine:
        return {}
    if not await _peer(client, mine[0].user_id):
        _note(("foreign", m.post, name), "mandate %s: its holder %s is no peer of this "
              "session: never mandated", m.role, name)
        return {}
    out: dict[str, tuple[str, str]] = {}
    for p in mine:
        if (p.channel_id == home and not p.type and not p.from_webhook and not p.delete_at
                and m.at_ms <= p.create_at < m.until_ms and await _in_task(client, p, home)):
            out[p.id] = (m.post, m.by)
    return out


async def judge(client: ChatPort, posts: Sequence[Post],
                now: float | None = None) -> dict[str, tuple[str, str]]:
    """Step 4, when the events arrive: of these posts by peer agents, the
    ones the home's mandate covers now, each with (mandate post id, the
    human's name). A TASK root is judged once and kept so (step 5). Never
    raises: unsure, no marker."""
    if not posts:
        return {}
    now = time.time() if now is None else now
    try:
        out = await _judge(client, posts, now)
    except Exception as exc:  # noqa: BLE001 — unsure: no marker
        log.info("mandate: events not judged: %s", exc)
        return {}
    for p in posts:
        if not p.root_id and p.kind == TASK:
            _judged.setdefault(p.id, out.get(p.id, ("", ""))[0])
    while len(_judged) > JUDGED_MAX:
        _judged.pop(next(iter(_judged)))
    return out


async def task_marks(client: ChatPort, posts: Sequence[Post], untrusted: Collection[str] = (),
                     now: float | None = None) -> dict[str, str]:
    """Step 5: the home's TASK roots marked `mandated`, with the mandate
    post's id. Each is judged once, at first sight (its event, else this
    read) and kept so: a mandate that ends later unmarks nothing, and one
    set later marks nothing older than itself."""
    roots = [p for p in posts if not p.root_id and p.kind == TASK and p.id not in untrusted]
    new = [p for p in roots if p.id not in _judged]
    if new:
        await judge(client, new, now)
    return {p.id: _judged[p.id] for p in roots if _judged.get(p.id)}


async def info(client: ChatPort, now: float | None = None) -> dict[str, Any]:
    """bus_info's mandate (step 6): role, by, until, holder, post; holder
    null with why when nobody may use it; only why when none holds."""
    m, why = await current(client, now)
    if m is None:
        return {"why": why}
    out: dict[str, Any] = {"role": m.role, "by": m.by, "until": _iso(m.until_ms),
                           "holder": None, "post": m.post, "source": m.source,
                           **({"note": m.note} if m.note else {})}
    try:
        name, why = await holder(client, m)
        if name:
            bot = (await client.bots_named({name})).get(name.lower())
            if bot is not None and await _peer(client, bot.id):
                out["holder"] = name
            else:
                why = f"{name} is foreign, or not shown to be a peer: never mandated"
    except Exception as exc:  # noqa: BLE001 — diagnostic only
        why = f"holder not read: {exc}"
    return out | ({"why": why} if why else {})


async def holds(client: ChatPort, uid: str, now: float | None = None) -> list[str]:
    """bus_info's `mandate_holds` (step 9): the guest channels, as `~name`,
    this coordinator watches under a mandate; none without the crown."""
    try:
        return [f"~{c['name']}" for c in await held(client, now)] if lease.crown_held(uid) else []
    except Exception as exc:  # noqa: BLE001 — diagnostic only
        log.info("mandate: holds not read: %s", exc)
        return []


_HINT = ("With the registrar's keys pinned, a mandate is set with `/agent-mandate {command}` "
         "(`off` removes it), signed by the registrar, so this post mandates nothing.")


async def observe(client: ChatPort, post: Post, uid: str) -> bool:
    """A post the inbox sees, live or in its backfill: True when it is a
    mandate post by a trusted human in the home while the registrar's keys are
    pinned (step 8). It mandates nothing and goes to no agent's model; the
    coordinator's server answers it once in its thread with how a mandate is set. False
    for any other post, without pinned keys (phase 1: a post is the mandate)
    and on any error: the post goes on as any post."""
    try:
        got = parse(post.message) if signed() else None
        if got is None:
            return False
        home = await client.work_channel_id()
        if (post.channel_id != home or post.root_id or post.type or post.from_webhook
                or post.delete_at or not post.user_id
                or post.user_id not in await client.trusted_humans()
                or post.user_id in await client.agents({post.user_id})):
            return False
        if not lease.crown_held(uid):
            _note(("swallowed", post.id), "mandate post %s by a trusted human goes to no agent "
                  "(the registrar's keys are pinned); only the coordinator's server answers it", post.id)
        else:
            name, until = got
            posts = (await client.thread(post.id)).posts
            if not any(p.kind == a2a.MANDATE for p in posts):
                command = f"set ~{name}" + (f" until {until}" if until else "")
                await client.post(lease.with_crown(_HINT.format(command=command), uid),
                                  root_id=post.id, props=a2a.props(kind=a2a.MANDATE))
        return True
    except Exception as exc:  # noqa: BLE001 — fail open
        log.info("mandate post %s not read: %s", post.id or None, exc)
        return False


async def tick(client: ChatPort, uid: str = "", now: float | None = None) -> float | None:
    """On the reminders' heartbeat (stall.watch): read the home's pinned
    posts again once REFRESH_SEC have passed, as askroute reads the signed
    mode; it brings no beat forward (None). Never raises."""
    now = time.time() if now is None else now
    try:
        home = await client.work_channel_id()
        last = _fetched.get(home)
        if last is None or now - last >= REFRESH_SEC:
            await refresh(client, now)
    except Exception as exc:  # noqa: BLE001 — the next heartbeat tries again
        log.info("mandate: not refreshed: %s", exc)
    return None

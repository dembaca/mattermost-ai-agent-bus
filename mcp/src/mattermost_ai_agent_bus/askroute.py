"""Ask routing modes (RT-6.25 in docs/arc42/06-runtime-view.md; ADR-005 phases 1, 2).

Two classes of ask. A *question* is content: approach A or B, what first, who
does X. An *approval* is an action with effect. ask_human takes the class the
agent declares (`kind`, default approval) and makes it an approval whenever
its need, decision or answers name an action with effect (FLOOR, the one list
of them): the floor decides, never the agent, and unclear stays approval.

Two modes per home. `direct`: every ask goes to its human (RT-6.11).
`coordinator`: a question of an agent that does not hold its home's crown goes
to the home's 👑; approvals, the 👑's own asks and every ask in `direct` go to
the human. Routing is not authority: the 👑's answer is from_agent, never
from_trusted_human, so it approves nothing (ADR-001).

The default is MM_ASK_ROUTE. A trusted human switches a home with a top-level
post there whose first line is `ask-mode coordinator` or `ask-mode direct`
(command). Each server that sees it keeps it in askmode-<uid>.env, 0600:

    MODE=<direct|coordinator>
    SOURCE=post            (a file without it is post)
    POST=<the switching post's id>
    AT=<its create_at, epoch ms>
    HOME=<the home's channel id>
    SET_BY=<the human's name>
    CONFIRMED=1            (once the 👑's server confirmed it)

Without that file for its home a server reads the home's newest READ_POSTS
posts once for the newest switch; nothing there, the default. The 👑's server
acknowledges a switch once in its thread; a holder that took the crown after
the switch does so on the heartbeat.

With the registrar's keys pinned (MM_REGISTRAR_KEYS, ADR-005 phase 2, step 8)
only the registrar's signed record sets the mode: a human types `/agent-mode
direct|coordinator` in the home, and every server reads
`<registrar>/register/v1/modes/<home>` at its first mode() and then every
REFRESH_SEC on the heartbeat, verifies it (attest.verify_mode) and keeps it
when it is newer (SOURCE=registrar, AT=set_at_ms) and set by one of its
trusted humans. A switching post then changes nothing.

A question to the 👑 that neither it nor a trusted human answered within
MM_ASK_ROUTE_WAIT_MIN minutes (default 15) is asked of the human by the asking
agent's server, once (tick, from the reminders' heartbeat and under their lock,
remind.tick). The post itself is the server's (fallback): it builds an ask card.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from . import a2a, asks, attest, lease, session_file
from .model import Post

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

QUESTION, APPROVAL = "question", "approval"
KINDS = (QUESTION, APPROVAL)
DIRECT, COORDINATOR = "direct", "coordinator"
MODES = (DIRECT, COORDINATOR)
ENV = "MM_ASK_ROUTE"
WAIT_ENV = "MM_ASK_ROUTE_WAIT_MIN"
WAIT_MIN = 15
# The server's own event when it asked the human instead of the 👑 (step 6).
KIND = "ask_route"
# How far back a server looks for the newest switch without a stored one.
READ_POSTS = 200
ENV_SOURCE = "env"

# --- the floor: actions with effect (step 1) -------------------------------------------
# One list, tested in test_ask_route.py. A hit makes an ask an approval whatever
# the agent declared; a word that only looks like one leans to approval too.
# External writes: a forge or tracker changed, or a message sent outside the bus.
# "the open PRs" or "write tests for the PR" name none.
_FORGE_VERBS = (r"(?:(?:re)?open(?:s|ed|ing)?\s+(?:a|an|the|new|up|it)\b|creat\w*"
                r"|fil(?:e|ed|ing)\b|rais\w*|submit\w*|clos(?:e|es|ed|ing)\b|comment\w*"
                r"|post(?:s|ed|ing)?\b|edit\w*|updat\w*|label\w*)")
_FORGE = (r"(?:issues?|PRs?|pull[ -]requests?|merge[ -]requests?|MRs?|tickets?|jira|github"
          r"|gitlab)")
_SEND_VERBS = (r"(?:send\w*|sent|post(?:s|ed|ing)?|writ\w*|repl(?:y|ies|ied)|notif\w*"
               r"|e-?mail\w*|messag\w*)")
_OUTSIDE = (r"(?:slack|teams|e-?mails?|mails?|customers?|upstream|external\w*"
            r"|mailing[ -]lists?)")
FLOOR: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("merge", re.compile(r"\bmerg(?:e|es|ed|ing)\b", re.I)),
    ("deploy", re.compile(r"\b(?:deploy\w*|roll(?:s|ed|ing)?[ -]?out|ausroll\w*)\b", re.I)),
    ("release", re.compile(r"\b(?:releas(?:e|es|ed|ing)|tag(?:s|ged|ging)?|publish\w*"
                           r"|veröffentlich\w*)\b", re.I)),
    ("install", re.compile(r"\b(?:(?:un|re)?install\w*|installier\w*)\b", re.I)),
    ("delete", re.compile(r"\b(?:delet\w*|remov\w*|destroy\w*|wip(?:e|es|ed|ing)\b|purg\w*"
                          r"|lösch\w*|entfern\w*)|\brm\s+-", re.I)),
    ("push", re.compile(r"\bpush\w*", re.I)),
    ("external write", re.compile(rf"\b{_FORGE_VERBS}[^.?!\n]{{0,40}}?\b{_FORGE}\b", re.I)),
    ("external write", re.compile(rf"\b{_SEND_VERBS}\b[^.?!\n]{{0,40}}?\b{_OUTSIDE}\b", re.I)),
    ("spending", re.compile(r"\b(?:spen[dt]\w*|pa(?:y|ys|ying|id|yment\w*)\b|purchas\w*"
                            r"|buy\w*|bought|subscri\w*|invoic\w*|bezahl\w*|kauf\w*)"
                            r"|[$€£]\s?\d|\d\s?(?:[€$£]|(?:EUR|USD|CHF)\b)", re.I)),
)


def floor(*texts: str) -> str | None:
    """The first action with effect the texts name (FLOOR), None for none."""
    text = " ".join(t for t in texts if t)
    return next((name for name, rx in FLOOR if rx.search(text)), None)


def classify(declared: str, *texts: str) -> tuple[str, str | None]:
    """The ask's class and, when the server overrode a declared question, the
    action that did (step 1). A kind other than the two is an approval."""
    kind = (declared or APPROVAL).strip().lower()
    if kind not in KINDS:
        return APPROVAL, None
    if kind == QUESTION and (named := floor(*texts)):
        return APPROVAL, named
    return kind, None


# --- the mode (step 2) -----------------------------------------------------------------

_warned: set[str] = set()


def env_mode() -> str:
    """MM_ASK_ROUTE: direct or coordinator; unset or anything else is direct,
    the latter logged once."""
    raw = os.environ.get(ENV, "").strip().lower()
    if raw in MODES:
        return raw
    if raw and raw not in _warned:
        _warned.add(raw)
        log.warning("%s=%r is no ask mode (direct, coordinator): direct", ENV, raw)
    return DIRECT


def wait_sec() -> int:
    """How long a question waits for the 👑 before the human is asked
    (MM_ASK_ROUTE_WAIT_MIN, default 15 min, at least 1)."""
    raw = os.environ.get(WAIT_ENV, "").strip()
    return (max(1, int(raw)) if raw.isdigit() else WAIT_MIN) * 60


_COMMAND = re.compile(r"`?ask-mode\s+(direct|coordinator)`?[.!]?", re.I)


def command(message: str) -> str | None:
    """The mode a post's first line sets: `ask-mode coordinator` or
    `ask-mode direct`, any case, in backticks too; None for anything else."""
    first = (message or "").strip().split("\n", 1)[0].strip()
    m = _COMMAND.fullmatch(first)
    return m[1].lower() if m else None


_UID = re.compile(r"[a-z0-9]{26}")
POST_SOURCE, REGISTRAR = "post", "registrar"
# How often a server reads the registrar's record of its home again (step 8),
# on the reminders' heartbeat; the first mode() of a process reads it at once.
REFRESH_SEC = 120


def _path(uid: str) -> Path | None:
    return session_file.state_dir() / f"askmode-{uid}.env" if _UID.fullmatch(uid) else None


def signed() -> bool:
    """Whether the registrar's keys are pinned (MM_REGISTRAR_KEYS): then only
    its signed record sets a home's mode (step 8), else a trusted human's post
    (step 2)."""
    return bool(attest.pinned())


def stored(uid: str, home: str, *, source: str | None = None) -> dict[str, str] | None:
    """What is kept for this home: MODE, SOURCE (post or registrar; a file
    without it is post), AT, HOME, POST or SET_BY, CONFIRMED once the 👑's
    server confirmed it; None without one, or with `source` one from another."""
    path = _path(uid)
    try:
        lines = path.read_text().splitlines() if path else []
    except OSError:
        return None
    got = {k: v for k, sep, v in (line.partition("=") for line in lines) if sep}
    if got.get("HOME") != home or got.get("MODE") not in MODES or not got.get("AT", "").isdigit():
        return None
    got.setdefault("SOURCE", POST_SOURCE)
    return got if source is None or got["SOURCE"] == source else None


def _current(uid: str, home: str) -> dict[str, str] | None:
    """The kept mode that counts: the registrar's record with keys pinned,
    else a switching post."""
    return stored(uid, home, source=REGISTRAR if signed() else POST_SOURCE)


def _write(uid: str, fields: dict[str, str]) -> bool:
    path = _path(uid)
    if path is None:
        return False
    try:
        session_file.write_private(path, "".join(f"{k}={v}\n" for k, v in fields.items() if v))
    except OSError as exc:
        log.info("ask mode not kept: %s", exc)
        return False
    return True


def _store(uid: str, home: str, post: Post, mode: str, who: str) -> bool:
    """Keep the switch when it is newer than the one kept; whether it was."""
    old = stored(uid, home, source=POST_SOURCE)
    if old is not None and int(old["AT"]) >= post.create_at:
        return False
    if not _write(uid, {"MODE": mode, "SOURCE": POST_SOURCE, "POST": post.id,
                        "AT": str(post.create_at), "HOME": home, "SET_BY": who}):
        return False
    log.info("ask mode %s from post %s", mode, post.id)
    return True


async def _switches(client: ChatPort, post: Post, home: str) -> tuple[str, str] | None:
    """The mode the post switches its home to and the human's name: a
    top-level post in the home, by a trusted human (by the account, as for
    from_trusted_human, RT-6.8), never a webhook's or an agent's, with the
    command; None otherwise."""
    mode = command(post.message)
    if (mode is None or post.channel_id != home or post.root_id or post.type
            or post.from_webhook or post.delete_at or not post.user_id):
        return None
    trusted = await client.trusted_humans()
    if post.user_id not in trusted:
        return None
    if post.user_id in await client.agents({post.user_id}):
        return None
    return mode, trusted[post.user_id]


_ACK = {
    COORDINATOR: ("ask-mode coordinator, set by {who}: the agents here ask the 👑 their "
                  "questions first; approvals still go to a human, never to an agent. A "
                  "question unanswered for {wait} min goes to its human."),
    DIRECT: "ask-mode direct, set by {who}: every ask goes to its human again.",
}
# Step 8: the confirmation of a registrar's record names its source.
_SIGNED = "{who} with /agent-mode (signed by the registrar)"
_HINT = ("With the registrar's keys pinned, the mode is set with `/agent-mode {mode}`, "
         "signed by the registrar, so this post changes nothing.")


async def _reply_once(client: ChatPort, post_id: str, text: str, uid: str,
                      thread_posts: tuple[Post, ...] | None = None) -> None:
    """The 👑's one reply in a switch's thread; none when the thread has one
    already (another holder, a restart)."""
    posts = (await client.thread(post_id)).posts if thread_posts is None else thread_posts
    if any(p.kind == a2a.ASK_MODE for p in posts):
        return
    await client.post(lease.with_crown(text, uid), root_id=post_id,
                      props=a2a.props(kind=a2a.ASK_MODE))


async def _acknowledge(client: ChatPort, post_id: str, mode: str, uid: str, who: str = "") -> None:
    """The 👑's one reply in the switch's thread (step 2)."""
    posts = (await client.thread(post_id)).posts
    if not who:  # kept before 0.68.0, without SET_BY
        root = next((p for p in posts if p.id == post_id), None)
        if root is not None and root.user_id:
            who = (await client.usernames({root.user_id})).get(root.user_id, "")
    text = _ACK[mode].format(who=who or "a trusted human", wait=wait_sec() // 60)
    await _reply_once(client, post_id, text, uid, posts)


async def _confirm(client: ChatPort, uid: str, home: str) -> None:
    """The 👑's server confirms the kept mode once (steps 2, 8): a switching
    post in its thread, a registrar's record top-level in the home. None when
    one is there already (another holder, a restart); either way it is marked
    CONFIRMED. Also for a switch kept while no server held the crown."""
    got = _current(uid, home)
    if got is None or got.get("CONFIRMED"):
        return
    mode = got["MODE"]
    if got["SOURCE"] == REGISTRAR:
        since = int(got["AT"]) - attest.SKEW_SEC * 1000
        head = f"ask-mode {mode},"
        posts = await client.channel_posts(home, per_page=READ_POSTS)
        if not any(p.kind == a2a.ASK_MODE and not p.root_id and p.create_at >= since
                   and head in p.message for p in posts):
            who = _SIGNED.format(who=got.get("SET_BY") or "a trusted human")
            text = _ACK[mode].format(who=who, wait=wait_sec() // 60)
            await client.post(lease.with_crown(text, uid), channel_id=home,
                              props=a2a.props(kind=a2a.ASK_MODE))
    elif got.get("POST"):
        await _acknowledge(client, got["POST"], mode, uid, got.get("SET_BY", ""))
    _write(uid, {**got, "CONFIRMED": "1"})


async def observe(client: ChatPort, post: Post, uid: str) -> bool:
    """A post the inbox sees, live or in its backfill: True when it is a
    switch by a trusted human in the home, which goes to no agent's model.
    Kept when newer than the kept one; the 👑's server acknowledges it then.
    With the registrar's keys pinned it switches nothing: the 👑's server
    answers it once with how the mode is set (step 8). Fails open: False on
    any error, and the post goes on as any post."""
    if command(post.message) is None:
        return False
    try:
        home = await client.work_channel_id()
        found = await _switches(client, post, home)
        if found is None:
            return False
        mode, who = found
        if signed():
            if lease.crown_held(uid):
                await _reply_once(client, post.id, _HINT.format(mode=mode), uid)
            return True
        if _store(uid, home, post, mode, who) and lease.crown_held(uid):
            await _confirm(client, uid, home)
        return True
    except Exception as exc:  # noqa: BLE001 — fail open
        log.info("ask mode post %s not read: %s", post.id or None, exc)
        return False


# --- the registrar's signed record (step 8, ADR-005 phase 2) ---------------------------

_fetched: dict[str, float] = {}  # home -> when this process last asked the registrar
_noted: set[tuple[str, ...]] = set()  # refused or ignored records, logged once


def _note(key: tuple[str, ...], msg: str, *args: object) -> None:
    if key not in _noted:
        _noted.add(key)
        log.info(msg, *args)


async def refresh(client: ChatPort, uid: str, home: str, now: float | None = None) -> bool:
    """Step 8: read the registrar's record of the home, verify it against the
    pinned keys and keep it when it is newer than the kept one and set by a
    trusted human of this session (by id: authority from trust, ADR-001); the
    👑's server confirms a record it newly kept. Whether it kept one. Never
    raises: what is kept holds, and the next cadence tries again."""
    _fetched[home] = time.time() if now is None else now
    try:
        token = await client.ask_mode_record(home)
    except Exception as exc:  # noqa: BLE001 — the kept one holds
        log.info("ask mode: the registrar's record not read: %s", exc)
        return False
    if token is None:
        return False  # none set there: the kept one holds (`direct` is the reset)
    try:
        record, why = attest.verify_mode(token, attest.pinned(), channel_id=home)
        if record is None:
            _note((home, why), "ask mode: the registrar's record for %s refused: %s", home, why)
            return False
        if record.set_by_id not in await client.trusted_humans():
            _note((home, record.set_by_id, str(record.set_at_ms)),
                  "ask mode %s by %s ignored: not a trusted human of this session", record.mode,
                  record.set_by)
            return False
        old = stored(uid, home, source=REGISTRAR)
        if old is not None and int(old["AT"]) >= record.set_at_ms:
            return False
        if not _write(uid, {"MODE": record.mode, "SOURCE": REGISTRAR, "AT": str(record.set_at_ms),
                            "HOME": home, "SET_BY": record.set_by, "SET_BY_ID": record.set_by_id}):
            return False
        log.info("ask mode %s from the registrar, set by %s", record.mode, record.set_by)
        if lease.crown_held(uid):
            await _confirm(client, uid, home)
        return True
    except Exception as exc:  # noqa: BLE001 — the kept one holds
        log.info("ask mode: the registrar's record not kept: %s", exc)
        return False


async def _heartbeat(client: ChatPort, uid: str, now: float) -> None:
    """On the reminders' heartbeat: read the registrar's record again every
    REFRESH_SEC (keys pinned), and the 👑's server confirms a kept mode no one
    confirmed yet (one kept while no server held the crown). Never raises."""
    try:
        home = await client.work_channel_id()
        last = _fetched.get(home)
        if signed() and (last is None or now - last >= REFRESH_SEC):
            await refresh(client, uid, home, now)
        if lease.crown_held(uid):
            await _confirm(client, uid, home)
    except Exception as exc:  # noqa: BLE001 — the next heartbeat tries again
        log.info("ask mode: not refreshed: %s", exc)


_looked: set[str] = set()  # homes this process read for a switch


async def _mode(client: ChatPort, uid: str, *, reread: bool = False) -> tuple[str, str, str]:
    try:
        home = await client.work_channel_id()
    except Exception as exc:  # noqa: BLE001 — the default
        log.info("ask mode: no home: %s", exc)
        return env_mode(), ENV_SOURCE, ""
    if signed():  # step 8: the registrar's record only, read once here, then on the heartbeat
        if home not in _fetched:
            await refresh(client, uid, home)
        got = stored(uid, home, source=REGISTRAR)
        return ((got["MODE"], REGISTRAR, got.get("SET_BY", "")) if got
                else (env_mode(), ENV_SOURCE, ""))
    if reread or (stored(uid, home, source=POST_SOURCE) is None and home not in _looked):
        try:
            posts = await client.channel_posts(home, per_page=READ_POSTS)
            _looked.add(home)
            for p in sorted(posts, key=lambda q: q.create_at, reverse=True):
                if (found := await _switches(client, p, home)) is not None:
                    _store(uid, home, p, *found)
                    break
        except Exception as exc:  # noqa: BLE001 — what is kept, else the default
            log.info("ask mode: home not read: %s", exc)
    got = stored(uid, home, source=POST_SOURCE)
    return (got["MODE"], got.get("POST") or ENV_SOURCE, "") if got else (env_mode(), ENV_SOURCE, "")


async def mode(client: ChatPort, uid: str, *, reread: bool = False) -> tuple[str, str]:
    """The home's ask mode and its source: "registrar" (keys pinned, step 8),
    the switching post's id, or "env". Keys pinned: the registrar's record
    only, read at the first call and then on the heartbeat. Else, without a
    kept switch, the home is read once (READ_POSTS, newest first); reread
    reads it again (an ask while the inbox saw nothing live). An error leaves
    what is kept, else the default."""
    found, source, _ = await _mode(client, uid, reread=reread)
    return found, source


async def info(client: ChatPort, uid: str) -> dict[str, object]:
    """bus_info's ask_mode: mode, source, set_by for the registrar's record,
    and whether the mode is signed."""
    found, source, who = await _mode(client, uid)
    return {"mode": found, "source": source, **({"set_by": who} if who else {}),
            "signed": source == REGISTRAR}


async def coordinator(client: ChatPort, username: str) -> str:
    """The home's 👑 to ask a question (step 3), as coordinator("who") finds
    it: a live 👑 among the agents of the home. '' for none, for the asking
    agent itself, or when the roster cannot be read (step 5: the human)."""
    try:
        holder = await lease.coordinator_of(client, await client.work_channel_id())
    except Exception as exc:  # noqa: BLE001 — unsure: the human
        log.info("ask route: no roster: %s", exc)
        return ""
    return "" if not holder or holder.lower() == username.lower() else holder


# --- no answer in time (step 6) --------------------------------------------------------

# Set by server.py: ask the human instead (the ask card is the server's), close
# the question, tell the agent; returns the new ask's id.
fallback: Callable[[ChatPort, str, str, str], Awaitable[str]] | None = None


async def _answered(client: ChatPort, uid: str, pid: str, coord: str) -> bool:
    """A reply in the question's thread after it by the 👑 or a trusted human,
    or a yes/no reaction by either; or the question gone."""
    posts = (await client.thread(pid)).posts
    ask = next((p for p in posts if p.id == pid), None)
    if ask is None or ask.delete_at:
        return True
    reactions = await client.reactions(pid)
    trusted = await client.trusted_humans()
    names = await client.usernames(({p.user_id for p in posts} | {r.user_id for r in reactions})
                                   - {"", uid})

    def by(user: str) -> bool:
        return user in trusted or (bool(coord) and names.get(user, "").lower() == coord.lower())

    if any(asks.answer_of(r.emoji_name) and by(r.user_id) for r in reactions):
        return True
    return any(p.id != pid and p.create_at > ask.create_at and not p.delete_at and by(p.user_id)
               for p in posts)


async def tick(client: ChatPort, uid: str, now: float | None = None) -> float | None:
    """The questions to the 👑 whose wait is over: settled ones are marked,
    the others asked of the human (fallback). Seconds until the next is due,
    None for none. Never raises: a failed one is tried at the next tick.
    First the mode's heartbeat: the registrar's record read again, a kept
    mode confirmed by the 👑's server (steps 2, 8)."""
    now = now or time.time()
    await _heartbeat(client, uid, now)
    wait, nxt = wait_sec(), None
    for pid, at, coord, human in asks.questions_open(uid):
        due = at + wait
        if due > now:
            nxt = due - now if nxt is None else min(nxt, due - now)
            continue
        try:
            if await _answered(client, uid, pid, coord):
                asks.question_settled(uid, pid)
                continue
            if fallback is None or asks.open_to(uid, human):
                continue  # one ask at a time: after the open one is settled
            await fallback(client, pid, human, coord)
        except Exception as exc:  # noqa: BLE001 — the next tick tries again
            log.info("question %s not passed on: %s", pid, exc)
    return nxt

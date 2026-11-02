"""Ask routing modes (RT-6.25 in docs/arc42/06-runtime-view.md; ADR-005 phase 1).

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
    POST=<the switching post's id>
    AT=<its create_at, epoch ms>
    HOME=<the home's channel id>

Without that file for its home a server reads the home's newest READ_POSTS
posts once for the newest switch; nothing there, the default. The 👑's server
acknowledges a switch once in its thread.

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

from . import a2a, asks, lease, session_file
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


def _path(uid: str) -> Path | None:
    return session_file.state_dir() / f"askmode-{uid}.env" if _UID.fullmatch(uid) else None


def stored(uid: str, home: str) -> dict[str, str] | None:
    """The switch kept for this home: MODE, POST, AT, HOME; None without one."""
    path = _path(uid)
    try:
        lines = path.read_text().splitlines() if path else []
    except OSError:
        return None
    got = {k: v for k, sep, v in (line.partition("=") for line in lines) if sep}
    if got.get("HOME") != home or got.get("MODE") not in MODES or not got.get("AT", "").isdigit():
        return None
    return got


def _store(uid: str, home: str, post: Post, mode: str) -> bool:
    """Keep the switch when it is newer than the one kept; whether it was."""
    path = _path(uid)
    old = stored(uid, home)
    if path is None or (old is not None and int(old["AT"]) >= post.create_at):
        return False
    try:
        session_file.write_private(
            path, f"MODE={mode}\nPOST={post.id}\nAT={post.create_at}\nHOME={home}\n")
    except OSError as exc:
        log.info("ask mode not kept: %s", exc)
        return False
    log.info("ask mode %s from post %s", mode, post.id)
    return True


async def _switches(client: ChatPort, post: Post, home: str) -> str | None:
    """The mode the post switches its home to: a top-level post in the home,
    by a trusted human (by the account, as for from_trusted_human, RT-6.8),
    never a webhook's or an agent's, with the command; None otherwise."""
    mode = command(post.message)
    if (mode is None or post.channel_id != home or post.root_id or post.type
            or post.from_webhook or post.delete_at or not post.user_id):
        return None
    if post.user_id not in await client.trusted_humans():
        return None
    if post.user_id in await client.agents({post.user_id}):
        return None
    return mode


_ACK = {
    COORDINATOR: ("ask-mode coordinator, set by {who}: the agents here ask the 👑 their "
                  "questions first; approvals still go to a human, never to an agent. A "
                  "question unanswered for {wait} min goes to its human."),
    DIRECT: "ask-mode direct, set by {who}: every ask goes to its human again.",
}


async def _acknowledge(client: ChatPort, post: Post, mode: str, uid: str) -> None:
    """The 👑's one reply in the switch's thread (step 2); none when the thread
    has one already (another holder, a restart)."""
    thread = await client.thread(post.id)
    if any(p.kind == a2a.ASK_MODE for p in thread.posts):
        return
    who = (await client.usernames({post.user_id})).get(post.user_id) or "a trusted human"
    text = _ACK[mode].format(who=who, wait=wait_sec() // 60)
    await client.post(lease.with_crown(text, uid), root_id=post.id,
                      props=a2a.props(kind=a2a.ASK_MODE))


async def observe(client: ChatPort, post: Post, uid: str) -> bool:
    """A post the inbox sees, live or in its backfill: True when it is a
    switch by a trusted human in the home, which goes to no agent's model.
    Kept when newer than the kept one; the 👑's server acknowledges it then.
    Fails open: False on any error, and the post goes on as any post."""
    if command(post.message) is None:
        return False
    try:
        home = await client.work_channel_id()
        mode = await _switches(client, post, home)
        if mode is None:
            return False
        if _store(uid, home, post, mode) and lease.crown_held(uid):
            await _acknowledge(client, post, mode, uid)
        return True
    except Exception as exc:  # noqa: BLE001 — fail open
        log.info("ask mode post %s not read: %s", post.id or None, exc)
        return False


_looked: set[str] = set()  # homes this process read for a switch


async def mode(client: ChatPort, uid: str, *, reread: bool = False) -> tuple[str, str]:
    """The home's ask mode and its source: the switching post's id, or "env".
    Without a kept switch the home is read once (READ_POSTS, newest first);
    reread reads it again (an ask while the inbox saw nothing live). An error
    leaves what is kept, else the default."""
    try:
        home = await client.work_channel_id()
    except Exception as exc:  # noqa: BLE001 — the default
        log.info("ask mode: no home: %s", exc)
        return env_mode(), ENV_SOURCE
    if reread or (stored(uid, home) is None and home not in _looked):
        try:
            posts = await client.channel_posts(home, per_page=READ_POSTS)
            _looked.add(home)
            for p in sorted(posts, key=lambda q: q.create_at, reverse=True):
                if (found := await _switches(client, p, home)) is not None:
                    _store(uid, home, p, found)
                    break
        except Exception as exc:  # noqa: BLE001 — what is kept, else the default
            log.info("ask mode: home not read: %s", exc)
    got = stored(uid, home)
    return (got["MODE"], got.get("POST") or ENV_SOURCE) if got else (env_mode(), ENV_SOURCE)


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
    None for none. Never raises: a failed one is tried at the next tick."""
    now = now or time.time()
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

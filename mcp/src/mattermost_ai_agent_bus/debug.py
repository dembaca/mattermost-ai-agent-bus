"""MCP debug, the bus side (RT-6.31 in docs/arc42/06-runtime-view.md; ADR-010 phase 2).

A trusted human switches debug categories on for a home with the registrar's
`/agent-debug` (phase 1, registrar 0.8.0), which signs one record per home.
With the registrar's keys pinned, each server reads that record at its first
look and then every REFRESH_SEC, and holds what it read only until the next
read: the registrar's exact 404 `no debug set`, a failed read, a record that
does not verify or one set by someone this session does not trust turn debug
off at once. Debug grants nothing, so nothing is won by keeping an old one.
Without pinned keys nothing is read and debug stays off.

While a category is on, the server reports what it decided, never a key, a
signature or a token, never an argument's value: about each event it
delivered of the home or a DM (`remember`, `block`: the agent's own view), and
about the agent's post in reply (`attachment`, the grey card below it). It
changes no decision. Never in a guest channel or the lobby; in a DM only what
the other side could see anyway (`SHARED`, step 6).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import deque
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from . import a2a, askroute, asks, attest, channel, mandate, owners, slim, tasks
from .model import NoDebugEndpoint, Post

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

REFRESH_SEC = 120  # as the ask mode's read (RT-6.25 step 8)
CATEGORIES = a2a.DEBUG_CATEGORIES
# A DM with a foreign agent or a person who is not trusted gets only these: they
# describe the post itself; the others show this session's trust decisions.
SHARED = frozenset({a2a.DEBUG_CALLS, a2a.DEBUG_PROPS})
ATTACHMENT_MAX = 4_000  # the whole card; the props blocks are cut first
BLOCK_PROPS_MAX = 1_000  # props in the agent's own view: every event costs context
LINE_MAX = 500
TRACES = 200  # delivered events remembered, the newest last
CALLS_MAX = 5
_CUT = "… (cut)"
# The card's table: Mattermost renders a markdown table in an attachment's text
# only with a header row, so the header says what the columns are.
TABLE_HEAD = "| what | value (times in UTC) |\n|:--|:--|"

NO_KEYS = ("the registrar's keys are not pinned (MM_REGISTRAR_KEYS): nothing is read "
           "from the registrar, debug stays off")
NO_ENDPOINT = ("the registrar has no debug endpoint (a 404 without `no debug set`: "
               "older than 0.8.0?)")


@dataclass(frozen=True)
class State:
    """What the last read of the home's record gave: the categories on (none:
    off, with why), until when, who set them, the record's kid and exp."""

    categories: tuple[str, ...] = ()
    until_ms: int = 0
    set_by: str = ""
    kid: str = ""
    exp: int = 0
    why: str = ""
    at: float = 0.0  # when it was read


@dataclass
class Trace:
    """One delivered event as debug saw it: where, its lines per category
    (without props and calls, which come when shown), its props as shown."""

    channel_id: str
    thread: str
    post_id: str
    lines: dict[str, str]
    sig_rows: tuple[tuple[str, str], ...]
    props_in: Mapping[str, Any]
    via: str = ""
    at: float = field(default_factory=time.time)


_states: dict[str, State] = {}  # home -> the last read
_traces: deque[Trace] = deque(maxlen=TRACES)
_calls: list[str] = []  # calls refused or rewritten since the agent's last view
_noted: set[tuple[str, ...]] = set()


def reset() -> None:
    """Forget everything kept in memory (tests)."""
    _states.clear()
    _traces.clear()
    _calls.clear()
    _noted.clear()


def _note(key: tuple[str, ...], msg: str, *args: object) -> None:
    if key not in _noted:
        _noted.add(key)
        log.info(msg, *args)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short(sec: float, now: float | None = None) -> str:
    """A time for people, always UTC: `19:11:30Z` when it is today, else
    `2026-11-12 21:15Z`."""
    now = time.time() if now is None else now
    at = datetime.fromtimestamp(sec, timezone.utc)
    if at.date() == datetime.fromtimestamp(now, timezone.utc).date():
        return at.strftime("%H:%M:%SZ")
    return at.strftime("%Y-%m-%d %H:%MZ")


# --- the record (step 2) ----------------------------------------------------------

async def _read(client: ChatPort, home: str, now: float) -> State:
    keys = attest.pinned()  # once per read
    if not keys:
        return State(why=NO_KEYS, at=now)
    try:
        token = await client.debug_record(home)
    except NoDebugEndpoint:
        _note(("endpoint", home), "debug: %s", NO_ENDPOINT)
        return State(why=NO_ENDPOINT, at=now)
    except Exception as exc:  # noqa: BLE001 — off until a read goes through
        log.info("debug: the registrar's record not read: %s", exc)
        return State(why=f"the registrar's record not read ({type(exc).__name__}): off "
                         "until a read goes through", at=now)
    if token is None:
        return State(why="none set with /agent-debug", at=now)
    record, why = attest.verify_debug(token, keys, channel_id=home, now=now)
    if record is None:
        _note(("refused", home, why), "debug: the registrar's record for %s refused: %s", home, why)
        return State(why=("the setting ended" if why == "ended"
                          else f"the registrar's record refused: {why}"), at=now)
    if record.set_by_id not in await client.trusted_humans():
        _note(("untrusted", home, record.set_by_id, str(record.set_at_ms)),
              "debug by %s ignored: not a trusted human of this session", record.set_by)
        return State(why=f"set by {record.set_by}, not a trusted human of this session", at=now)
    return State(categories=record.categories, until_ms=record.until_ms, set_by=record.set_by,
                 kid=record.kid, exp=record.exp, at=now)


async def refresh(client: ChatPort, now: float | None = None) -> State:
    """Read the home's record once and keep what it gave, replacing the last
    read whatever it was. Never raises."""
    now = time.time() if now is None else now
    try:
        home = await client.work_channel_id()
        got = await _read(client, home, now)
    except Exception as exc:  # noqa: BLE001 — off
        log.info("debug: not read: %s", exc)
        return State(why=f"not read: {type(exc).__name__}", at=now)
    old = _states.get(home)
    _states[home] = got
    if got.categories != (old.categories if old else ()):
        log.info("debug: %s", ", ".join(got.categories) + f" by {got.set_by} until "
                 f"{_iso(got.until_ms)}" if got.categories else f"off: {got.why}")
    return got


async def current(client: ChatPort, now: float | None = None) -> State:
    """The home's debug setting now: read again once REFRESH_SEC have passed
    since the last read (so nothing holds past the next read), off once its
    end or the record's exp passed (with attest.SKEW_SEC)."""
    now = time.time() if now is None else now
    try:
        home = await client.work_channel_id()
    except Exception as exc:  # noqa: BLE001 — no home: off
        return State(why=f"no home: {exc}", at=now)
    got = _states.get(home)
    if got is None or now - got.at >= REFRESH_SEC:
        got = await refresh(client, now)
    return _live(got, now)


def _live(got: State, now: float) -> State:
    """The state as it holds at `now`: off once its end or the record's exp
    passed (with attest.SKEW_SEC), or once the next read is due."""
    if got.categories and got.until_ms + attest.SKEW_SEC * 1000 <= now * 1000:
        return State(why=f"the setting ended at {_iso(got.until_ms)}", at=got.at)
    if got.categories and got.exp + attest.SKEW_SEC <= now:
        return State(why="the registrar's record expired", at=got.at)
    return got


async def tick(client: ChatPort, uid: str = "", now: float | None = None) -> float | None:
    """On the reminders' heartbeat (stall.watch): read the record again once
    REFRESH_SEC have passed. Brings no beat forward (None). Never raises."""
    try:
        await current(client, now)
    except Exception as exc:  # noqa: BLE001 — the next heartbeat tries again
        log.info("debug: not refreshed: %s", exc)
    return None


async def info(client: ChatPort) -> dict[str, Any]:
    """bus_info's `debug` (step 8): the categories, until when and who set them;
    or no categories and why debug is off."""
    got = await current(client)
    if not got.categories:
        return {"categories": [], "why": got.why}
    return {"categories": list(got.categories), "until": _iso(got.until_ms), "set_by": got.set_by}


# --- the lines of an event (step 3) -------------------------------------------------

def _wake(item: Mapping[str, Any], *, mine: bool | None, crown: bool) -> str:
    if item.get("gated"):
        return f"gated, wakes nobody here: {item['gated']}"
    kind, text = item.get("kind"), slim.text_of(item)
    fyi = item.get("intent") == a2a.FYI
    if kind == "mention":
        why = "a mention of this agent"
    elif kind == "dm":
        why = "a DM"
    elif kind == "reaction":
        why = "a reaction on its ask"
    elif item.get("departed"):
        why = f"names departed {item['departed']}: this agent routes it"
    elif item.get("not_here"):
        why = f"names {item['not_here']}, not in this channel: this agent forwards it"
    elif item.get("to") == a2a.ALL:
        why = "to all agents"
    elif channel.broadcast(text):
        why = "a broadcast (@channel, @here or @all)"
    elif mine:
        why = "a reply in its own thread" + (": fyi, wakes only the thread's starter" if fyi else "")
    elif crown and not tasks.mentions(text):
        why = "a plain post naming no agent: wakes only the coordinator, which routes it"
    elif tasks.mentions(text):
        why = "names another agent: for that one, here in the digest"
    else:
        why = "a post in the channel for no one: the digest"
    return why + (", fyi (needs no answer)" if fyi and not mine else "")


async def _identity(client: ChatPort, item: Mapping[str, Any], author: str) -> str:
    if item.get("trusted_human"):
        out = f"trusted human {item['trusted_human']} (by account id)"
    elif item.get("from_agent"):
        out = (f"foreign agent, owner {item.get('owner') or 'none'}" if item.get("foreign")
               else "peer agent")
        got = await owners.read(client, author) if author else None
        if got is not None:
            out += (f", owner {got.owner or 'none'} from its "
                    + ("attestation" if got.attested else "description"))
    else:
        out = "other: neither a trusted human nor a bus agent"
    if item.get("mandated"):
        out += f", mandated by {item.get('mandated_by')} ({item['mandated']})"
    if item.get("root_from_trusted_human"):
        out += f", its thread's root by trusted human {item['root_from_trusted_human']}"
    return out


async def _sig(client: ChatPort, state: State, item: Mapping[str, Any], author: str,
               uid: str) -> tuple[tuple[str, str], ...]:
    """The records the server relied on, one (label, value) per fact."""
    rows = [("debug record", f"valid, kid {state.kid}, exp {_short(state.exp)}, from the registrar")]
    if item.get("from_agent") and author:
        claim = await owners.attested(client, author)
        rows.append(("attestation", f"valid, kid {claim.kid}, exp {_short(claim.exp)}, from its account"
                     if claim is not None else "none or not valid: its description decides"))
    if uid:
        mode = await askroute.info(client, uid)
        rows.append(("ask mode", f"{mode['mode']} from {mode['source']}"
                     + (" (signed)" if mode.get("signed") else " (unsigned)")))
    m, why = await mandate.current(client)
    rows.append(("mandate", f"from {m.source}, until {_short(m.until_ms / 1000)}" if m is not None
                 else f"none: {why[:120]}"))
    return tuple(rows)


def _asks(item: Mapping[str, Any], uid: str, root: str, now: float) -> str:
    open_ = {pid: at for pid, at, _, _ in asks.waiting(uid)} if uid else {}
    queued = len(asks.queued(uid)) if uid else 0
    tail = f"{len(open_)} open, {queued} queued"
    ask = str(item.get("ask") or "")
    if ask:
        answer = item.get("answer") or (f"choice {item['choice']}" if item.get("choice") else "")
        if not item.get("from_trusted_human"):
            why = "does not count: not by a trusted human"
        elif item.get("expired"):
            why = "does not count: more than 24 h after the ask"
        elif not answer:
            why = "does not count: no answer emoji"
        else:
            why = "counts: a trusted human, this ask, within 24 h"
        age = f", {int((now - open_[ask]) // 60)} min old" if ask in open_ else ""
        return f"reaction {answer or item.get('emoji', '')} on ask {ask}{age}: {why}; {tail}"
    if root and root in open_:
        why = ("answers it" if item.get("from_trusted_human") and not item.get("expired")
               else "too late (24 h)" if item.get("expired") else "advice at most: not a trusted human")
        return f"a reply in the thread of open ask {root}: {why}; {tail}"
    return tail


async def remember(client: ChatPort, item: Mapping[str, Any], post: Post, *, state: State,
                   uid: str, mine: bool | None, crown: bool,
                   props_in: Mapping[str, Any]) -> Trace:
    """The event `item` (built from `post`) as debug sees it, kept as delivered
    in its channel (the attachment of a post answering it reads it)."""
    now = time.time()
    lines = {"wake": _wake(item, mine=mine, crown=crown),
             "identity": await _identity(client, item, post.user_id),
             "asks": _asks(item, uid, post.root_id, now)}
    sig = await _sig(client, state, item, post.user_id, uid)
    lines["sig"] = "; ".join(f"{k}: {v}" for k, v in sig)
    trace = Trace(channel_id=post.channel_id, thread=post.thread, post_id=post.id,
                  lines=lines, sig_rows=sig, props_in=dict(props_in), at=now)
    _traces.append(trace)
    return trace


def delivered_via(post_id: str, via: str) -> None:
    """How the newest trace of `post_id` reached the agent (pushed, the digest,
    wait_for_events), for the wake line."""
    for trace in reversed(_traces):
        if trace.post_id == post_id:
            trace.via = via
            return


def about(channel_id: str, root_id: str) -> tuple[Trace | None, bool]:
    """The event a post in `channel_id` (in thread `root_id`) answers, and
    whether that is a weak guess: the thread's last delivered event, else the
    channel's last delivered (a guess). Events of other channels never count."""
    here = [t for t in reversed(_traces) if t.channel_id == channel_id]
    if root_id:
        same = next((t for t in here if root_id in (t.thread, t.post_id)), None)
        if same is not None:
            return same, False
    return (here[0] if here else None), True


# --- calls (step 4) -------------------------------------------------------------------

def call_line(tool: str, notes: Collection[str] = (), /, **args: object) -> str:
    """A tool call as debug shows it: the arguments given by name and length
    (1 for a flag or number), never their values, and what the server changed."""
    shown = ", ".join(f"{k} {len(v) if isinstance(v, (str, list, dict)) else 1}"
                      for k, v in args.items() if v)
    return f"{tool}({shown})" + (": " + "; ".join(notes) if notes else ": as called")


def _on(now: float | None = None) -> bool:
    """Whether a category holds now, as current() would say without a read:
    its end, the record's exp and the next read due all count."""
    now = time.time() if now is None else now
    return any(_live(s, now).categories and now - s.at < REFRESH_SEC for s in _states.values())


def refused(tool: str) -> None:
    """A call the server refused, for the agent's next view while debug is on
    (no reason: it may quote a value)."""
    if _on():
        _calls.append(f"{tool} refused")
        del _calls[:-CALLS_MAX]


def rewritten(line: str) -> None:
    """A call the server changed (call_line with notes), for the agent's next
    view while debug is on."""
    if _on():
        _calls.append(line)
        del _calls[:-CALLS_MAX]


def _take_calls() -> str:
    out = "; ".join(_calls) or "none refused or changed since the last event"
    _calls.clear()
    return out


# --- display (steps 5 and 6) ----------------------------------------------------------

def _ago(sec: float) -> str:
    """How long ago, roughly: the weaker a guess, the older its event."""
    if sec < 60:
        return "just now"
    return f"{int(sec // 60)} min ago" if sec < 3600 else f"{int(sec // 3600)} h ago"


def _line(name: str, text: str) -> str:
    line = f"{name}: {' '.join(text.split())}"
    return line if len(line) <= LINE_MAX else line[:LINE_MAX - 1] + "…"


# A value is text, never markup: a bot's description may name an owner as a link or image.
_ESCAPE = re.compile(r"[\\`*_\[\]<>!|]")


def _row(label: str, text: str) -> str:
    """One table row: the label in bold, the value on one line, as literal text
    (backslash first, then markdown characters and `|`, each escaped once)."""
    value = _ESCAPE.sub(r"\\\g<0>", " ".join(text.split()))
    value = value if len(value) <= LINE_MAX else value[:LINE_MAX - 1] + "…"
    return f"| **{label}** | {value} |"


def _json(value: Mapping[str, Any] | None, indent: int | None) -> str:
    return json.dumps(dict(value or {}), sort_keys=True, indent=indent, ensure_ascii=False,
                      separators=None if indent else (",", ":"), default=str)


def _blocks(blocks: list[tuple[str, str]], room: int) -> str:
    """The props blocks as `json` code, cut to fit `room` characters together:
    a block shorter than its share keeps all of it, the others share the rest.
    '' when not even their frames fit."""
    frame = [f"{label}:\n```json\n" for label, _ in blocks]
    tail = "\n```"
    budget = room - sum(len(f) + len(tail) for f in frame) - (len(blocks) - 1)
    if budget < len(_CUT) * len(blocks):
        return ""
    texts = [text for _, text in blocks]
    caps = [0] * len(texts)
    left = budget
    for n, i in enumerate(sorted(range(len(texts)), key=lambda j: len(texts[j]))):
        caps[i] = min(len(texts[i]), left // (len(texts) - n))
        left -= caps[i]
    texts = [t if len(t) <= cap else t[:cap - len(_CUT)] + _CUT for t, cap in zip(texts, caps, strict=True)]
    return "\n".join(f + t + tail for f, t in zip(frame, texts, strict=True))


def attachment(state: State, trace: Trace | None, guess: bool, *, call: str,
               props_out: Mapping[str, Any] | None,
               allowed: Collection[str] | None = None) -> str:
    """The `debug` attachment's text for the agent's post (step 5): a table, one row
    per fact of each active category (of `allowed`), about `trace`, the event the post answers;
    props as two `json` blocks; at most ATTACHMENT_MAX characters, props cut
    first. '' with no category to show."""
    cats = [c for c in state.categories if allowed is None or c in allowed]
    if not cats:
        return ""
    if trace is None:
        head = "event: none delivered here while debug is on"
    elif guess:
        head = (f"event: post {trace.post_id}, the last delivered here (a guess: none in this "
                f"thread, {_ago(time.time() - trace.at)})")
    else:
        head = f"event: post {trace.post_id}, the last delivered in this thread"
    rows = [_row("event", head.removeprefix("event: "))]
    for c in cats:
        if c == a2a.DEBUG_CALLS:
            rows.append(_row(c, call))
        elif c == a2a.DEBUG_PROPS:
            continue
        elif trace is None:
            rows.append(_row(c, "no event"))
        elif c == "sig":
            rows.extend(_row(k, v) for k, v in trace.sig_rows)
        else:
            text = trace.lines.get(c, "-")
            if c == "wake" and trace.via:
                text += f"; {trace.via}"
            rows.append(_row(c, text))
    text = "\n".join([TABLE_HEAD, *rows])
    if a2a.DEBUG_PROPS in cats:
        room = ATTACHMENT_MAX - len(text) - 1
        shown = _blocks([("props in", _json(trace.props_in if trace else None, 2)),
                         ("props out", _json(props_out, 2))], room)
        text += "\n" + (shown or _line(a2a.DEBUG_PROPS, "cut: the attachment holds 4,000 characters"))
    return text[:ATTACHMENT_MAX]


def block(trace: Trace, state: State) -> str:
    """The agent's own view of an event of its home (step 6): the same lines,
    props in only, cut at BLOCK_PROPS_MAX; calls: what the server refused or
    changed since the last view."""
    lines = []
    for c in state.categories:
        if c == a2a.DEBUG_CALLS:
            lines.append(_line(c, _take_calls()))
        elif c == a2a.DEBUG_PROPS:
            text = _json(trace.props_in, None)
            if len(text) > BLOCK_PROPS_MAX:
                text = text[:BLOCK_PROPS_MAX - len(_CUT)] + _CUT
            lines.append(f"props in: {text}")
        else:
            lines.append(_line(c, trace.lines.get(c, "-")))
    return "\n".join(lines)

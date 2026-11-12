"""Context and limits of this agent, as its status line reports them (ADR-008,
RT-6.10 step 3, RT-6.6 step 1, RT-6.14).

bin/mm-agent-statusline.sh posts what Claude Code's status line JSON says
(`POST /v1/usage`, hookapi.py); the server keeps the last report of its one
session here, in memory. lease.py shows it in the bot's status text; the
roster reads it back from the status text of the agents it lists (the props of
a bot cannot carry it: Mattermost refuses its own patch with a 403). Numbers only, in range: anything else is ignored. A
display and a hint to the coordinator, never a permission (ADR-001).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

log = logging.getLogger(__name__)

WINDOWS = ("five_hour", "seven_day")
# A report's resets_at must be an epoch within these bounds of now (seconds).
RESET_PAST_SEC = 86400
RESET_AHEAD_SEC = 8 * 86400
CTX_BANDS = (50, 70, 85, 95)
LIMIT_BANDS = (70, 90, 100)
SHOW_RESET_FROM = 90  # a window's reset time and the 7-day segment show from here
BAND_GAP_SEC = 60.0  # a band write never comes sooner than this after the last
FIVE_HOURS_SEC = 5 * 3600
MAX_COST_USD = 1_000_000.0  # a sanity bound, not a limit
CTX_LINE_AT = 85  # ADR-008 step 4: one line in the HELLO thread per context window
# Below this the next line is armed: a compaction goes far below, a jitter around
# CTX_LINE_AT does not.
CTX_REARM_BELOW = CTX_LINE_AT - 15

_last: dict[str, Any] | None = None
_limits_logged = False
_logged = False
_bands_written: tuple[int, int, int] | None = None
_band_at = 0.0
_ctx_armed = True  # the context line may come (below CTX_LINE_AT since the last)


def reset() -> None:
    """Forget everything (tests)."""
    global _last, _limits_logged, _logged, _bands_written, _band_at, _ctx_armed
    _last, _limits_logged, _logged = None, False, False
    _bands_written, _band_at, _ctx_armed = None, 0.0, True


def _percent(value: Any) -> float | None:
    """A percentage 0 to 100; above 100 it is 100 (a window at its limit may
    say 100.4), never dropped; below 0, NaN and no number are none."""
    if type(value) not in (int, float) or not value >= 0:  # also not NaN
        return None
    return float(round(min(value, 100), 1))


def _cost(value: Any) -> float | None:
    """The session's cumulative cost in dollars: a number from 0 up to
    MAX_COST_USD, rounded to cents; anything else is none."""
    if type(value) not in (int, float) or not 0 <= value <= MAX_COST_USD:  # also not NaN
        return None
    return float(round(value, 2))


def _epoch(value: Any, now: float) -> int | None:
    if type(value) is float and value.is_integer():
        value = int(value)
    if type(value) is not int or not now - RESET_PAST_SEC <= value <= now + RESET_AHEAD_SEC:
        return None
    return value


def parse(body: Any, now: float) -> dict[str, Any]:
    """The valid part of a report: `ctx`, `ctx_size`, `cost_usd` (the session's
    cumulative cost, shown where no 5-hour window is reported), and `five_hour`
    / `seven_day` as {used, resets_at} (each field only when valid, a window
    only with a field). The rest is dropped without a word."""
    out: dict[str, Any] = {}
    if not isinstance(body, dict):
        return out
    ctx = _percent(body.get("ctx"))
    if ctx is not None:
        out["ctx"] = ctx
    cost = _cost(body.get("cost_usd"))
    if cost is not None:
        out["cost_usd"] = cost
    size = body.get("ctx_size")
    if type(size) is int and 0 < size < 2**40:
        out["ctx_size"] = size
    for name in WINDOWS:
        raw = body.get(name)
        if not isinstance(raw, dict):
            continue
        end = raw.get("resets_at")
        if isinstance(end, (int, float)) and not isinstance(end, bool) and end <= now:
            continue  # the window is over, however long ago: its `used` is old
        window: dict[str, Any] = {}
        used = _percent(raw.get("used"))
        if used is not None:
            window["used"] = used
        reset = _epoch(raw.get("resets_at"), now)
        if reset is not None:
            window["resets_at"] = reset
        if window:
            out[name] = window
    return out


def _bands(report: dict[str, Any] | None) -> tuple[int, int, int]:
    def band(value: Any, edges: tuple[int, ...]) -> int:
        return sum(1 for e in edges if isinstance(value, float) and value >= e)

    r = report or {}
    return (band(r.get("ctx"), CTX_BANDS),
            band((r.get("five_hour") or {}).get("used"), LIMIT_BANDS),
            band((r.get("seven_day") or {}).get("used"), LIMIT_BANDS))


def record(body: Any, now: float | None = None) -> bool:
    """Keep a report as this session's last one (the valid fields only, with
    `at`). True when a number changed, False for one that says the same (only
    `at` moves: cheap). Logs once whether the limits came (ADR-008 open
    question: do they come at all, and when)."""
    global _last, _limits_logged, _logged
    now = time.time() if now is None else now
    got = parse(body, now)
    if not got:
        return False
    both = all("used" in got.get(w, {}) for w in WINDOWS)
    # Once per session; the first report may precede the first API answer
    # (no rate_limits yet), so a later arrival of the limits is told once too.
    if not _logged or (both and not _limits_logged):
        log.info("usage: rate_limits five_hour+seven_day present" if both
                 else "usage: no rate_limits (only ctx)" if "ctx" in got else "usage: no ctx")
        _logged, _limits_logged = True, _limits_logged or both
    changed = _last is None or {k: v for k, v in _last.items() if k != "at"} != got
    _last = {**got, "at": int(now)}
    return changed


def last() -> dict[str, Any] | None:
    return dict(_last) if _last else None


def band_due(now: float | None = None) -> bool:
    """Whether a number crossed a band since the status last showed it (ctx
    50/70/85/95 %, a window 70/90/100 %) and no band write came within
    BAND_GAP_SEC. A cheap check, so a report that changes nothing costs
    nothing."""
    now = time.time() if now is None else now
    return (_last is not None and _bands(_last) != (_bands_written or (0, 0, 0))
            and now - _band_at >= BAND_GAP_SEC)


def take_band(now: float | None = None) -> bool:
    """band_due, and the crossing is taken: True once per crossing."""
    global _band_at
    now = time.time() if now is None else now
    if not band_due(now):
        return False
    _band_at = now
    return True


def ctx_line_due() -> bool:
    """Whether the last report's `ctx` reached CTX_LINE_AT and no line came for this
    context window yet (ADR-008 step 4, RT-6.6 step 9). A report below
    CTX_REARM_BELOW (a compaction) arms the next line; one without `ctx` changes
    nothing. Cheap."""
    global _ctx_armed
    ctx = (_last or {}).get("ctx")
    if ctx is None:
        return False
    if ctx < CTX_REARM_BELOW:
        _ctx_armed = True
    return ctx >= CTX_LINE_AT and _ctx_armed


def take_ctx_line() -> float | None:
    """ctx_line_due, and the line is taken: the percentage once per context
    window, None otherwise. ctx_line_failed gives it back."""
    global _ctx_armed
    if not ctx_line_due():
        return None
    _ctx_armed = False
    return float((_last or {})["ctx"])


def ctx_line_failed() -> None:
    """The line could not be posted: the next report tries again."""
    global _ctx_armed
    _ctx_armed = True


def shown() -> None:
    """The status text carries the current numbers (lease.py): their bands
    are no new crossing."""
    global _bands_written
    _bands_written = _bands(_last)


def _live(report: dict[str, Any], now: float) -> dict[str, Any]:
    """The report without a window that has reset since (its `used` is old)."""
    return {k: v for k, v in report.items()
            if k not in WINDOWS or not v.get("resets_at") or v["resets_at"] > now}


def segments(now: float | None = None) -> list[str]:
    """The status text's usage segments, in the order they are dropped from
    the end: `ctx 62%`, `5h 40%` (`5h 93% ↻21:40Z`: reset time from 90 %, UTC),
    `7d 92%` (only from 90 %); where no 5-hour window is reported the cost
    stands in its place, `$4.20`. [] without a report."""
    now = time.time() if now is None else now
    if _last is None:
        return []
    r = _live(_last, now)
    out = []
    if "ctx" in r:
        out.append(f"ctx {round(r['ctx'])}%")
    if "cost_usd" in r and (r.get("five_hour") or {}).get("used") is None:
        out.append(f"${r['cost_usd']:.2f}")
    for name, tag, floor in (("five_hour", "5h", 0), ("seven_day", "7d", SHOW_RESET_FROM)):
        used = (r.get(name) or {}).get("used")
        if used is None or used < floor:
            continue
        text = f"{tag} {round(used)}%"
        if used >= SHOW_RESET_FROM and r[name].get("resets_at"):
            text += f" ↻{time.strftime('%H:%MZ', time.gmtime(r[name]['resets_at']))}"
        out.append(text)
    return out


# The segments of a status text (segments() above writes them; RT-6.6 step 1):
# `ctx 62%`, `5h 40%` or `5h 93% ↻21:40Z`, `7d 92%` or `7d 92% ↻21:40Z`, `$4.20`.
_SEGMENT = re.compile(
    r"^(?:ctx (?P<ctx>\d{1,3})%"
    r"|5h (?P<h5>\d{1,3})%(?: ↻(?P<hh>\d\d):(?P<mm>\d\d)Z)?"
    r"|7d (?P<d7>\d{1,3})%(?: ↻\d\d:\d\dZ)?"
    r"|\$(?P<cost>\d{1,7}\.\d\d))$")


def from_status(text: str, now: float | None = None) -> dict[str, Any]:
    """What the roster shows of an agent from its custom status text, which every
    roster read holds anyway (ADR-008 step 3, as amended 2026-11-05): `ctx`, `cost_usd`,
    `limit_5h` and `limit_7d` as {used[, resets_at]}. A segment the text lacks is
    unknown, not 0 (the cut of RT-6.6 step 1 drops `7d`, `5h`, `ctx` in that order, and
    the label first). `resets_at` of the 5-hour window is the next time of day its
    `↻HH:MMZ` names (shown only from 90 %). {} for a text without a number. Validated
    like a report: the agent writes its own status."""
    now = time.time() if now is None else now
    out: dict[str, Any] = {}
    for piece in text.split(" · "):
        m = _SEGMENT.match(piece.strip())
        if m is None:
            continue
        if m["ctx"]:
            out["ctx"] = _percent(int(m["ctx"]))
        elif m["h5"]:
            window: dict[str, Any] = {"used": _percent(int(m["h5"]))}
            if m["hh"] and int(m["hh"]) < 24 and int(m["mm"]) < 60:
                day = now - now % 86400
                at = day + int(m["hh"]) * 3600 + int(m["mm"]) * 60
                at = at if at > now else at + 86400
                # A 5-hour window never ends more than 5 hours ahead (a minute of slack):
                # a later time of day means the text is old and the window is over.
                if at - now <= FIVE_HOURS_SEC + 60:
                    window["resets_at"] = int(at)
            out["limit_5h"] = window
        elif m["d7"]:
            out["limit_7d"] = {"used": _percent(int(m["d7"]))}
        elif m["cost"]:
            cost = _cost(float(m["cost"]))
            if cost is not None:
                out["cost_usd"] = cost
    return {k: v for k, v in out.items() if v is not None and v != {"used": None}}


def own(now: float | None = None) -> dict[str, Any]:
    """The roster's numbers for this agent itself, from the report in memory (the
    same shape as from_status): `ctx`, `cost_usd`, `limit_5h`, `limit_7d`; a window
    that has reset is left out."""
    now = time.time() if now is None else now
    if _last is None:
        return {}
    r = _live(_last, now)
    out: dict[str, Any] = {k: r[k] for k in ("ctx", "cost_usd") if k in r}
    for name, key in (("five_hour", "limit_5h"), ("seven_day", "limit_7d")):
        if r.get(name):
            window = dict(r[name])
            if "resets_at" in window:
                # The status text names it to the minute: the same value as an agent
                # reading that text gets, so `limit_shared` (exact equality) counts this one too.
                window["resets_at"] = window["resets_at"] // 60 * 60
            out[key] = window
    return out


def info(now: float | None = None) -> dict[str, Any]:
    """bus_info's `usage` (ADR-008 step 3): the last report this server holds, and
    the segments of the status text it shows (`shown`, what the other agents' rosters read)."""
    now = time.time() if now is None else now
    return {"report": ({k: v for k, v in _last.items() if k != "at"} if _last else None),
            "shown": " · ".join(segments(now))}

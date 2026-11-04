"""Context and limits of this agent, as its status line reports them (ADR-008,
RT-6.10 step 3, RT-6.6 step 1, RT-6.14).

bin/mm-agent-statusline.sh posts what Claude Code's status line JSON says
(`POST /v1/usage`, hookapi.py); the server keeps the last report of its one
session here, in memory. lease.py shows it in the bot's status text and writes
it into the bot's props (`agent_usage`); the roster reads it back from there
for the agents it lists. Numbers only, in range: anything else is ignored. A
display and a hint to the coordinator, never a permission (ADR-001).
"""

from __future__ import annotations

import json
import logging
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
PROPS_GAP_SEC = 60.0

_last: dict[str, Any] | None = None
_limits_logged = False
_logged = False
_bands_written: tuple[int, int, int] | None = None
_band_at = 0.0
_props_at = 0.0
_props_sent = ""


def reset() -> None:
    """Forget everything (tests)."""
    global _last, _limits_logged, _logged, _bands_written, _band_at, _props_at, _props_sent
    _last, _limits_logged, _logged = None, False, False
    _bands_written, _band_at, _props_at, _props_sent = None, 0.0, 0.0, ""


def _percent(value: Any) -> float | None:
    """A percentage 0 to 100; above 100 it is 100 (a window at its limit may
    say 100.4), never dropped; below 0, NaN and no number are none."""
    if type(value) not in (int, float) or not value >= 0:  # also not NaN
        return None
    return float(round(min(value, 100), 1))


def _epoch(value: Any, now: float) -> int | None:
    if type(value) is float and value.is_integer():
        value = int(value)
    if type(value) is not int or not now - RESET_PAST_SEC <= value <= now + RESET_AHEAD_SEC:
        return None
    return value


def parse(body: Any, now: float) -> dict[str, Any]:
    """The valid part of a report: `ctx`, `ctx_size`, and `five_hour` /
    `seven_day` as {used, resets_at} (each field only when valid, a window
    only with a field). The rest is dropped without a word."""
    out: dict[str, Any] = {}
    if not isinstance(body, dict):
        return out
    ctx = _percent(body.get("ctx"))
    if ctx is not None:
        out["ctx"] = ctx
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
    `7d 92%` (only from 90 %). [] without a report."""
    now = time.time() if now is None else now
    if _last is None:
        return []
    r = _live(_last, now)
    out = []
    if "ctx" in r:
        out.append(f"ctx {round(r['ctx'])}%")
    for name, tag, floor in (("five_hour", "5h", 0), ("seven_day", "7d", SHOW_RESET_FROM)):
        used = (r.get(name) or {}).get("used")
        if used is None or used < floor:
            continue
        text = f"{tag} {round(used)}%"
        if used >= SHOW_RESET_FROM and r[name].get("resets_at"):
            text += f" ↻{time.strftime('%H:%MZ', time.gmtime(r[name]['resets_at']))}"
        out.append(text)
    return out


def props_value(now: float | None = None) -> str:
    """The `agent_usage` the bot's props carry: compact JSON, the last report
    (without a reset window), '' without one."""
    now = time.time() if now is None else now
    if _last is None:
        return ""
    return json.dumps(_live(_last, now), separators=(",", ":"), sort_keys=True)


def props_due(now: float | None = None) -> str:
    """The value to write into the props now, '' when none is due: the
    numbers changed since the last write and PROPS_GAP_SEC passed."""
    now = time.time() if now is None else now
    value = props_value(now)
    if not value or now - _props_at < PROPS_GAP_SEC:
        return ""
    return "" if _same(value, _props_sent) else value


def _same(a: str, b: str) -> bool:
    def bare(s: str) -> Any:
        try:
            d = json.loads(s)
        except ValueError:
            return s
        return {k: v for k, v in d.items() if k != "at"} if isinstance(d, dict) else s

    return bool(b) and bare(a) == bare(b)


def props_tried(now: float | None = None) -> None:
    """A props write is attempted now: it counts against the gap either way."""
    global _props_at
    _props_at = time.time() if now is None else now


def props_written(value: str) -> None:
    """`value` is in the props; a failed write leaves the old one, tried again
    by the next beat after the gap."""
    global _props_sent
    _props_sent = value


def from_props(raw: str | None, now: float | None = None) -> dict[str, Any]:
    """What the roster shows of an agent's `agent_usage`: `ctx`, `limit_5h`,
    `limit_7d` ({used, resets_at}); {} for none, an unreadable value or a
    window that has reset. Validated again: the bot writes it itself."""
    now = time.time() if now is None else now
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return {}
    got = parse(data, now) if isinstance(data, dict) else {}
    out: dict[str, Any] = {}
    if "ctx" in got:
        out["ctx"] = got["ctx"]
    for name, key in (("five_hour", "limit_5h"), ("seven_day", "limit_7d")):
        window = (got.get(name) or {}) if isinstance(data, dict) else {}
        if window.get("resets_at", now + 1) <= now:
            continue
        if "used" in window or "resets_at" in window:
            out[key] = window
    return out

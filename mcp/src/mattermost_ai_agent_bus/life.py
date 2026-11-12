"""The agent's sign of life (ADR-007 amendment step 1, phase D; RT-6.6 step 1).

The server's heartbeat proves that the server runs, not that its agent does:
a session in a background terminal kept its crown for two days while it took
up nothing (ADR-007 amendment, case 1). So the server counts the agent's own
signs, which it sees anyway, and judges it `silent` without them:

- **Signs.** A turn begins or ends (the prompt and Stop hooks' presence), a
  call of one of the server's tools, a post or reply through the hook API
  (`mm-agent-post.sh`), and the hooks' and the listener's reads of the hook
  API (RT-6.17). Not the heartbeat, not the status line's usage report and not
  `/v1/me`, which the status line asks in a corral session.
- **The take-up.** A push (a channel tag or a digest, not the channel check;
  RT-6.12) or a wake the listener is about to give (a pending count above 0,
  a wake of `/v1/wake`) waits untaken until a turn begins or the agent calls
  a tool. One that comes during a turn is the turn's: Claude Code queues it
  for the next turn, which the Stop hook's end leads to.
- **Silent** while no turn runs: a push or wake untaken for MM_ACK_WAIT_SEC
  (default 45 s), or, outside a session started as a channel and idle there,
  no sign for MM_LEASE_SEC (the window, 900 s). A turn that runs counts as
  alive until it ends: the server's ping between events is not built (phase
  D without it, decided 2026-11-10).

State lives in memory: a new server starts alive, its start being the
client's handshake. lease.py writes the status from it and drops a crown.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable

_sign = time.time()  # the last sign of the agent
_turn = False  # a turn runs (the prompt hook's working, until the Stop hook's end)
_untaken = 0.0  # the first push or wake no turn or tool call took up yet; 0 none
_shown = False  # the status last written shows the agent silent
# Told of every take-up (ADR-007 amendment step 2, receipt.py): True at a turn's
# begin or end, which takes up what came during the turn too; False for a tool call.
_taken: list[Callable[[bool], None]] = []


def ack_wait_sec() -> int:
    """How long a push may wait untaken while no turn runs (MM_ACK_WAIT_SEC,
    default 45 s)."""
    s = os.environ.get("MM_ACK_WAIT_SEC", "")
    return int(s) if s.isdigit() and int(s) > 0 else 45


def window_sec() -> int:
    """The window a sign lasts outside a turn: MM_LEASE_SEC (default 900 s)."""
    s = os.environ.get("MM_LEASE_SEC", "")
    return int(s) if s.isdigit() and int(s) > 0 else 900


def sign() -> None:
    """A sign of the agent that takes nothing up (a hook's or the listener's read)."""
    global _sign
    _sign = time.time()


def take_up() -> None:
    """A sign that takes up what was pushed: a tool call, a post by the agent."""
    global _untaken
    sign()
    _untaken = 0.0
    _tell(False)


def turn(running: bool) -> None:
    """A turn begins (it takes up what waits) or ends; either takes up what
    came during the turn before (RT-6.12 step 4: Claude Code queues it)."""
    global _turn, _untaken
    sign()
    if running:
        _untaken = 0.0
    _turn = running
    _tell(True)


def in_turn() -> bool:
    """A turn runs: from the prompt hook's working to the Stop hook's end."""
    return _turn


def on_take_up(fn: Callable[[bool], None]) -> None:
    """Call fn(boundary) at each take-up; boundary: a turn begins or ends."""
    if fn not in _taken:
        _taken.append(fn)


def _tell(boundary: bool) -> None:
    for fn in list(_taken):
        fn(boundary)


def pushed() -> None:
    """A push went out, or a wake is about to: untaken until a turn or a tool
    call, unless a turn runs now."""
    global _untaken
    if not _turn and not _untaken:
        _untaken = time.time()


def silent_since(now: float | None = None, *, listening: bool = False) -> float:
    """Since when the agent shows no sign (the last sign, epoch s); 0 while it
    is alive. `listening`: idle in a session started as a channel, which has
    nothing to prove while nothing waits untaken."""
    now = time.time() if now is None else now
    if _turn:
        return 0.0
    if _untaken and now - _untaken >= ack_wait_sec():
        return _sign
    if not listening and now - _sign >= window_sec():
        return _sign
    return 0.0


def due(*, listening: bool = False) -> float:
    """When the agent turns silent without a further sign (epoch s); 0 never
    by itself (a turn runs) or already silent."""
    if _turn or silent_since(listening=listening):
        return 0.0
    at = [_untaken + ack_wait_sec()] if _untaken else []
    if not listening:
        at.append(_sign + window_sec())
    return min(at, default=0.0)


def shown(silent: bool) -> None:
    """The status was written, showing the agent silent or not."""
    global _shown
    _shown = silent


def reset() -> None:
    """A new process's state: alive now, no turn, nothing untaken (tests)."""
    global _sign, _turn, _untaken, _shown
    _sign, _turn, _untaken, _shown = time.time(), False, 0.0, False


def is_shown() -> bool:
    """The status last written shows the agent silent."""
    return _shown

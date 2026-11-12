"""RT-6.6 step 1 (ADR-007 amendment step 1, phase D): the agent's sign of
life, as the MCP server counts it (life.py)."""

import time

import pytest

from mattermost_ai_agent_bus import life


@pytest.fixture
def clock(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    life.reset()
    return now


def test_without_a_sign_the_window_ends_in_silence(clock):
    """Outside a turn the agent is silent once the window (MM_LEASE_SEC) passes
    without a sign; the next sign makes it alive again."""
    start = clock[0]
    clock[0] += 899
    assert life.silent_since() == 0 and life.due() == start + 900
    clock[0] += 1
    assert life.silent_since() == start and life.due() == 0
    life.sign()
    assert life.silent_since() == 0


def test_the_window_follows_the_setting(clock, monkeypatch):
    monkeypatch.setenv("MM_LEASE_SEC", "60")
    clock[0] += 60
    assert life.silent_since()
    monkeypatch.setenv("MM_LEASE_SEC", "zero")
    assert life.window_sec() == 900 and not life.silent_since()


def test_a_running_turn_is_alive_until_it_ends(clock):
    """Without the ping a turn counts as alive however long it runs, a push
    during it included; after its end the window counts from there."""
    life.turn(True)
    life.pushed()  # queued by Claude Code for the next turn: the turn's
    clock[0] += 3600
    assert life.silent_since() == 0 and life.due() == 0
    life.turn(False)
    end = clock[0]
    clock[0] += 899
    assert life.silent_since() == 0
    clock[0] += 1
    assert life.silent_since() == end


def test_a_push_left_untaken_makes_it_silent_at_once(clock):
    """A push no turn takes up is silence after MM_ACK_WAIT_SEC, long before
    the window; a turn that begins, or a tool call, takes it up."""
    life.sign()
    last = clock[0]
    clock[0] += 10
    life.pushed()
    pushed = clock[0]
    clock[0] += 5
    life.pushed()  # a second push keeps the first one's time
    assert life.due() == pushed + 45
    clock[0] = pushed + 45
    assert life.silent_since() == last
    life.turn(True)
    assert life.silent_since() == 0
    life.turn(False)
    life.pushed()
    clock[0] += 45
    assert life.silent_since()
    life.take_up()
    assert life.silent_since() == 0


def test_a_read_alone_takes_nothing_up(clock):
    """A hook's or the listener's read is a sign, not a take-up: the push
    still waits."""
    life.pushed()
    clock[0] += 44
    life.sign()
    clock[0] += 1
    assert life.silent_since()


def test_the_wait_follows_the_setting(clock, monkeypatch):
    monkeypatch.setenv("MM_ACK_WAIT_SEC", "5")
    life.pushed()
    clock[0] += 5
    assert life.silent_since()
    monkeypatch.setenv("MM_ACK_WAIT_SEC", "0")
    assert life.ack_wait_sec() == 45


def test_a_listening_channel_has_nothing_to_prove(clock):
    """Idle in a session started as a channel: no window, only an untaken
    push makes it silent."""
    clock[0] += 3600
    assert life.silent_since(listening=True) == 0 and life.due(listening=True) == 0
    life.pushed()
    assert life.due(listening=True) == clock[0] + 45
    clock[0] += 45
    assert life.silent_since(listening=True)


def test_what_the_status_shows_is_kept(clock):
    assert not life.is_shown()
    life.shown(True)
    assert life.is_shown()
    life.reset()
    assert not life.is_shown()

"""RT-6.2 end of a turn, RT-6.3 listen window ended by the human,
RT-6.4 bus unavailable (docs/arc42/06-runtime-view.md)."""

from __future__ import annotations

import time

from conftest import ME, PROJECT

STOP = "hooks/stop-inbox-check.sh"
PROMPT = "hooks/turn-status.sh"
IN_PROJECT = {"MM_PROJECT_CHANNEL_ID": PROJECT}


def turn_state(run) -> dict[str, int]:
    f = run.state_dir / f"turn-{ME}.env"
    if not f.exists():
        return {}
    return {k: int(v) for k, v in (l.split("=") for l in f.read_text().split())}


def set_window(run, since: int, until: int) -> None:
    f = run.state_dir / f"turn-{ME}.env"
    s = turn_state(run) | {"LISTEN_SINCE": since, "LISTEN_UNTIL": until}
    f.write_text("".join(f"{k}={v}\n" for k, v in s.items()))


# --- RT-6.2 -------------------------------------------------------------------

def test_drain_blocks_at_most_three_times(mm, run):
    """RT-6.2 step 1: pending mentions/DMs block the stop, bounded per turn."""
    mm.unread_dm(2)
    for _ in range(3):
        r = run(STOP)
        assert r.code == 2 and "2 unread message(s)" in r.reason
    assert run(STOP).code == 0


def test_signoff_requested_once_per_turn(mm, run):
    """RT-6.2 step 2: a busy latest post asks for IDLE/BYE once."""
    mm.own_post("HELLO working on x")
    r = run(STOP)
    assert r.code == 2 and "IDLE" in r.reason
    assert run(STOP).code == 0


def test_no_signoff_without_own_post(mm, run):
    """RT-6.2 step 2: an agent that never posted is not part of the coordination."""
    assert run(STOP).code == 0


def test_listen_window_opens_in_project_channel(mm, run):
    """RT-6.2 step 3: after the sign-off, a project channel gets a listen window."""
    mm.own_post("IDLE done x", 1000)
    r = run(STOP, **IN_PROJECT)
    assert r.code == 2 and "timeout_sec=100" in r.reason
    assert any(f"{s} s left" in r.reason for s in range(895, 901))
    status = mm.custom_status()
    assert status["emoji"] == "ear" and status["duration"] == "date_and_time"
    assert status["expires_at"].endswith("Z")


def test_listen_window_resumes_with_rest(mm, run):
    """RT-6.2 step 4: events handled, the window resumes with the remaining time."""
    mm.own_post("IDLE done x", 1000)
    run(STOP, **IN_PROJECT)
    now = int(time.time())
    set_window(run, now - 100, now + 300)
    r = run(STOP, **IN_PROJECT)
    assert r.code == 2 and "timeout_sec=100" in r.reason
    assert any(f"{s} s left" in r.reason for s in range(295, 301))


def test_listen_slices_stay_below_background_limit(mm, run):
    """RT-6.2 step 3: each call stays under the runtime's ~120 s background
    limit; the last slice is the rest of the window."""
    mm.own_post("IDLE done x", 1000)
    run(STOP, **IN_PROJECT)
    now = int(time.time())
    set_window(run, now - 100, now + 40)
    r = run(STOP, **IN_PROJECT)
    assert r.code == 2
    assert any(f"timeout_sec={s}" in r.reason for s in range(35, 41))
    set_window(run, now - 100, now + 900)
    r = run(STOP, MM_LISTEN_CALL_SEC="50", **IN_PROJECT)
    assert r.code == 2 and "timeout_sec=50" in r.reason


def test_own_answer_opens_fresh_window_without_second_signoff(mm, run):
    """RT-6.2 step 4: an answer on the bus restarts the window; no second IDLE."""
    mm.own_post("IDLE done x", 1000)
    run(STOP, MM_LISTEN_SEC="60", **IN_PROJECT)
    now = int(time.time())
    set_window(run, now - 100, now + 10)
    mm.own_post("ACK will do")
    r = run(STOP, MM_LISTEN_SEC="60", **IN_PROJECT)
    assert r.code == 2 and "timeout_sec=60" in r.reason


def test_window_over_goes_idle(mm, run):
    """RT-6.2 step 5: window expired → 💤 idle, turn state removed."""
    mm.own_post("IDLE done x", 1000)
    run(STOP, **IN_PROJECT)
    set_window(run, 1, 2)
    assert run(STOP, **IN_PROJECT).code == 0
    assert mm.custom_status()["emoji"] == "zzz"
    assert turn_state(run) == {}


def test_no_window_after_bye_or_when_off_or_without_project(mm, run):
    """RT-6.2 step 3: BYE, MM_LISTEN_SEC=0 and no project channel skip the window."""
    mm.own_post("BYE done", 1000)
    assert run(STOP, **IN_PROJECT).code == 0
    mm.own_post("IDLE done", 2000)
    assert run(STOP, MM_LISTEN_SEC="0", **IN_PROJECT).code == 0
    assert run(STOP).code == 0


def test_stateless_fallback_without_bot_id(mm, run):
    """RT-6.2 error handling: bot id unknown → stop_hook_active ends the turn."""
    r = run(STOP, stdin='{"stop_hook_active": true}', MM_BOT_USER_ID="not-an-id")
    assert r.code == 0


# --- RT-6.3 -------------------------------------------------------------------

def test_next_prompt_clears_turn_state(mm, run):
    """RT-6.3 step 3: after ESC the next prompt sets working and starts fresh."""
    mm.own_post("IDLE done x", 1000)
    run(STOP, **IN_PROJECT)
    assert turn_state(run)
    assert run(PROMPT).code == 0
    assert turn_state(run) == {}
    assert mm.custom_status()["emoji"] == "hammer_and_wrench"


def test_listening_status_expires_with_window(mm, run):
    """RT-6.3 step 2: the 👂 status carries the window end as its expiry."""
    mm.own_post("IDLE done x", 1000)
    run(STOP, **IN_PROJECT)
    until = turn_state(run)["LISTEN_UNTIL"]
    expires = mm.custom_status()["expires_at"]
    assert time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)) == expires


# --- RT-6.4 -------------------------------------------------------------------

def test_bus_unreachable_never_blocks(run):
    """RT-6.4: Mattermost unreachable → every hook exits 0."""
    dead = {"MM_CHAT_URL": "http://127.0.0.1:9", "MM_PROJECT_CHANNEL_ID": PROJECT}
    assert run(STOP, **dead).code == 0
    assert run(PROMPT, **dead).code == 0


def test_no_token_never_blocks(run):
    """RT-6.4: no bus configured → no opinion."""
    assert run(STOP, MM_BOT_TOKEN=None).code == 0

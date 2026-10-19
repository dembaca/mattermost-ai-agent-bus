"""mm-agent-sweep.sh: clean up session files a failed teardown left behind (RT-6.1)."""

from __future__ import annotations

import os

SWEEP = "bin/mm-agent-sweep.sh"
DEAD_PID = "999999"  # far above any pid in use on a test machine


def session(run, name: str, *, token: str = "bot-token-for-tests", pid: str = DEAD_PID,
            legacy: bool = False) -> str:
    d = run.state_dir
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{name}.env"
    lines = ["# ephemeral session — do not commit",
             f"{'MM_BOT_NAME' if legacy else 'MM_REG_BOT_NAME'}={name}",
             f"MM_BOT_USERNAME=agent-{name}", f"MM_BOT_TOKEN={token}",
             f"MM_SESSION_PID={pid}"]
    f.write_text("\n".join(lines) + "\n")
    return f.name


def deletes(mm) -> list[str]:
    return [p for m, p, _ in mm.writes if m == "DELETE"]


def test_unregisters_a_dead_session_and_removes_its_file(mm, run):
    f = session(run, "mac-proj-aaa111")
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "unregistered mac-proj-aaa111" in r.stderr
    assert deletes(mm) == ["/register/v1/agents/mac-proj-aaa111"]
    assert not (run.state_dir / f).exists()


def test_a_bot_whose_token_is_rejected_is_gone_already(mm, run):
    """A 401 for the bot's own token means the bot no longer exists: the file is
    stale, not a failure to keep forever."""
    f = session(run, "mac-proj-bbb222", token="revoked")
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "gone    mac-proj-bbb222" in r.stderr and "FAILED" not in r.stderr
    assert not (run.state_dir / f).exists()
    assert "1 removed, 0 failed" in r.stderr


def test_a_failure_says_why_and_keeps_the_file(mm, run):
    f = session(run, "mac-proj-ccc333")
    mm.deny.add("/register/v1/agents/mac-proj-ccc333")
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "FAILED mac-proj-ccc333: unregister failed http=403" in r.stderr
    assert (run.state_dir / f).exists()


def test_a_live_session_is_left_alone(mm, run):
    f = session(run, "mac-proj-ddd444", pid=str(os.getpid()))
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "ACTIVE  mac-proj-ddd444" in r.stderr
    assert deletes(mm) == [] and (run.state_dir / f).exists()


def test_files_from_before_the_registration_name_are_swept_too(mm, run):
    f = session(run, "mac-proj-eee555", legacy=True)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None)
    assert "unregistered mac-proj-eee555" in r.stderr
    assert not (run.state_dir / f).exists()


def state_file(run, name: str, *, days_old: float) -> str:
    """A per-bot state file (no token), aged by its mtime."""
    import time
    d = run.state_dir
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text("STATE=idle\n")
    t = time.time() - days_old * 86400
    os.utime(f, (t, t))
    return name


GONE = "g" * 26
LIVE = "l" * 26


def test_state_files_are_not_sessions(mm, run):
    """RT-6.1: presence, listen, turn and coord files are no sessions to skip noisily."""
    state_file(run, f"presence-{GONE}.env", days_old=0)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert "skip" not in r.stderr and deletes(mm) == []


def test_old_state_of_a_bot_without_session_is_removed(mm, run):
    """RT-6.1: leftovers of bots that are gone go; a live session's state stays."""
    session(run, "mac-proj-fff666", pid=str(os.getpid()))  # live, uid below
    (run.state_dir / "mac-proj-fff666.env").write_text(
        (run.state_dir / "mac-proj-fff666.env").read_text() + f"MM_BOT_USER_ID={LIVE}\n")
    old = [state_file(run, f"{k}-{GONE}.env", days_old=3)
           for k in ("coord", "listen", "presence", "turn")]
    old.append(state_file(run, f"stats-{GONE}.jsonl", days_old=3))
    fresh = state_file(run, f"coord-{'f' * 26}.env", days_old=0.1)  # touched today
    live = state_file(run, f"presence-{LIVE}.env", days_old=3)
    dry = run(SWEEP, MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert "would remove 5 state file(s)" in dry.stdout
    assert all((run.state_dir / f).exists() for f in old)
    r = run(SWEEP, "--apply", MM_BOT_TOKEN=None, MM_BOT_USER_ID=None)
    assert "removed 5 state file(s) of bots without a session" in r.stderr
    assert not any((run.state_dir / f).exists() for f in old)
    assert (run.state_dir / fresh).exists() and (run.state_dir / live).exists()

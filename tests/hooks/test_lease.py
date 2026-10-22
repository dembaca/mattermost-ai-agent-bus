"""RT-6.6 coordinator crown and presence lease (docs/arc42/06-runtime-view.md)."""

from __future__ import annotations

import time

from conftest import ME

LEASE = "bin/mm-agent-lease.sh"
STATUS = "bin/mm-agent-status.sh"
ROSTER = "bin/mm-agent-roster.sh"
OLDER = "o" * 26
YOUNGER = "y" * 26


def coord(run) -> dict[str, int]:
    f = run.state_dir / f"coord-{ME}.env"
    if not f.exists():
        return {}
    return {k: int(v) for k, v in (l.split("=") for l in f.read_text().split())}


def holds(run) -> bool:
    return coord(run).get("CROWN_UNTIL", 0) > time.time()


def me(mm, emoji="hammer_and_wrench", **kw):
    mm.agent(ME, "agent-me", 2000, emoji, **kw)


def test_every_status_is_a_lease(mm, run):
    """RT-6.6 step 1: a status always carries an expiry, so a dead session's runs out."""
    run(STATUS, "working")
    s = mm.custom_status()
    assert s["emoji"] == "hammer_and_wrench" and s["duration"] == "date_and_time"
    assert s["expires_at"].endswith("Z")


def test_oldest_reachable_takes_the_free_crown(mm, run):
    """RT-6.6 step 2: no crown anywhere → the oldest reachable agent takes it."""
    me(mm)
    mm.agent(YOUNGER, "agent-young", 3000, "hammer_and_wrench")
    mm.agent(OLDER, "agent-old-idle", 1000, "zzz")           # idle: not reachable
    mm.agent("s" * 26, "agent-stale", 500, "hammer_and_wrench", expires_in=None)
    run(STATUS, "working")
    assert run(LEASE, "tick").code == 0
    assert holds(run)
    s = mm.custom_status()
    assert s["emoji"] == "crown" and s["text"].startswith("coordinator · working")


def test_younger_agent_does_not_claim(mm, run):
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")
    run(LEASE, "tick")
    assert not holds(run)


def test_take_waits_for_the_holder_then_takes(mm, run):
    """RT-6.6 step 4: on ACK the new coordinator's take waits for the release."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown")
    r = run(LEASE, "take")
    assert "pending" in r.stdout and "@agent-old" in r.stdout
    assert not holds(run) and coord(run)["PENDING"] == 1
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")   # released
    run(LEASE, "tick")
    assert holds(run) and coord(run)["PENDING"] == 0


def test_release_leaves_room_for_the_successor(mm, run):
    """RT-6.6 step 4: after a release the oldest agent does not grab it back."""
    me(mm)
    run(LEASE, "take")
    assert holds(run)
    assert run(LEASE, "release").stdout.strip() == "released"
    run(LEASE, "tick")
    assert not holds(run)
    assert mm.custom_status()["emoji"] != "crown"


def test_two_crowns_the_younger_yields(mm, run):
    """RT-6.6 error handling: a race leaves two crowns; the older keeps it."""
    me(mm, "crown")
    run(LEASE, "take")
    mm.agent(OLDER, "agent-old", 1000, "crown")
    run(LEASE, "tick")
    assert not holds(run)


def test_idle_coordinator_loses_the_crown_when_its_lease_runs_out(mm, run):
    """RT-6.6 step 5: idle extends nothing; once the lease ends the crown is gone."""
    me(mm)
    run(LEASE, "take")
    until = coord(run)["CROWN_UNTIL"]
    run(STATUS, "idle")
    assert coord(run)["CROWN_UNTIL"] == until
    assert mm.custom_status()["emoji"] == "crown"
    f = run.state_dir / f"coord-{ME}.env"
    f.write_text(f.read_text().replace(f"CROWN_UNTIL={until}", "CROWN_UNTIL=1"))
    run(STATUS, "renew")
    assert mm.custom_status()["emoji"] == "zzz" and not holds(run)


def test_roster_lists_coordinator_first_and_skips_the_unreachable(mm, run):
    """RT-6.6: the crown holder heads the roster; idle and stale bots are left out."""
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")
    mm.agent(YOUNGER, "agent-young", 3000, "crown")
    mm.agent("i" * 26, "agent-idle", 1500, "zzz")
    mm.agent("s" * 26, "agent-stale", 500, "hammer_and_wrench", expires_in=None)
    lines = run(ROSTER).stdout.splitlines()
    assert [l.split("\t")[0] for l in lines] == ["agent-young", "agent-old"]
    assert len(run(ROSTER, "--all").stdout.splitlines()) == 4
    assert run(LEASE, "who").stdout.strip() == "agent-young"


def test_roster_json_for_the_mcp_server(mm, run):
    """RT-6.14: the same rows as JSON, coordinator first and marked, for
    coordinator("roster") — one place for the reachability rules."""
    import json

    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")
    mm.agent(YOUNGER, "agent-young", 3000, "crown")
    mm.agent("i" * 26, "agent-idle", 1500, "zzz")
    rows = json.loads(run(ROSTER, "--json").stdout)
    assert [(r["username"], r["coordinator"]) for r in rows] == [
        ("agent-young", True), ("agent-old", False)]
    assert rows[1]["emoji"] == "hammer_and_wrench"

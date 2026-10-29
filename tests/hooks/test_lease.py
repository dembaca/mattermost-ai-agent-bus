"""RT-6.6 the status by hand and from the host session hooks: mm-agent-status.sh
(docs/arc42/06-runtime-view.md). The lease and the crown are the MCP server's
(mcp/tests/test_lease.py). By hand the script is the server's client (RT-6.17);
only the host hooks, with the token on fd 3, write a status themselves."""

from __future__ import annotations

import asyncio
import os
import time

import pytest
from conftest import CHANNEL, ME, TEAM, hook_api
from mattermost_ai_agent_bus import lease

STATUS = "bin/mm-agent-status.sh"


def live_crown(run) -> None:
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / f"coord-{ME}.env").write_text(
        f"CROWN_UNTIL={int(time.time()) + 600}\nPENDING=0\nYIELD_UNTIL=0\n")


def server_env(mm, run) -> dict[str, str]:
    """The environment the MCP server of this session runs with."""
    return {"PATH": os.environ["PATH"], "HOME": str(run.state_dir.parents[1]),
            "XDG_STATE_HOME": str(run.state_dir.parent), "MM_CHAT_URL": mm.url,
            "MM_BOT_TOKEN": "bot-token-for-tests", "MM_BOT_USER_ID": ME, "MM_TEAM_ID": TEAM,
            "MM_CHANNEL_ID": CHANNEL}


def test_without_a_server_every_status_is_a_plain_lease(mm, run):
    """RT-6.6 step 1: the host's first status (no MCP server yet) carries an
    expiry and no crown when coord-<uid>.env holds none."""
    assert run(STATUS, "idle", "proj", fd_token=True).code == 0
    s = mm.custom_status()
    assert s["emoji"] == "zzz" and s["duration"] == "date_and_time"
    assert s["expires_at"].endswith("Z")
    assert (run.state_dir / f"presence-{ME}.env").read_text() == "STATE=idle\nLABEL=proj\n"


def test_without_a_server_the_status_names_the_home(mm, run):
    """RT-6.6 step 1, ADR-002: the host hooks write the form the server
    writes (lease.homed): the project channel is the home; past 100
    characters the label is cut, never the home."""
    assert run(STATUS, "working", "l" * 200, fd_token=True, MM_PROJECT_CHANNEL="team-a").code == 0
    text = mm.custom_status()["text"]
    assert len(text) == 100 and text.endswith(" · ~team-a")
    assert text == lease.homed("working · " + "l" * 200, "team-a")


def test_without_a_server_a_live_crown_keeps_its_text(mm, run):
    """RT-6.6 step 1: written directly, a crown whose lease still runs keeps
    the coordinator text until that lease ends, not longer."""
    live_crown(run)
    assert run(STATUS, "working", "proj", fd_token=True).code == 0
    s = mm.custom_status()
    assert s["emoji"] == "crown" and s["text"] == "coordinator · working · proj · ~agents"
    crown = int((run.state_dir / f"coord-{ME}.env").read_text().split()[0].split("=")[1])
    assert s["expires_at"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(crown))


@pytest.mark.parametrize("failure", ["error", "slow"])
def test_a_failed_hand_over_writes_nothing(mm, run, monkeypatch, failure):
    """RT-6.6, RT-6.17: the session's server answers /v1/me but its presence
    fails (502) or times out: the host hook's call exits 0 and writes no status
    itself, so it cannot clobber the crown the server holds; by hand it says so."""
    live_crown(run)

    async def set_status(client, state, label=""):
        if failure == "slow":
            await asyncio.sleep(1.5)
        raise RuntimeError("Mattermost down")

    monkeypatch.setattr(lease, "set_status", set_status)
    with hook_api(server_env(mm, run)) as bus:
        r = run(STATUS, "working", "proj", MM_BUS_MAX_TIME="1", fd_token=True, **bus)
        by_hand = run(STATUS, "working", "proj", MM_BUS_MAX_TIME="1", bus=None, **bus)
        time.sleep(1)  # the slow route ends before the server closes
    assert r.code == 0 and by_hand.code == 1 and "mm-bus:" in by_hand.stderr
    assert not [w for w in mm.writes if w[0] in ("PUT", "DELETE")]


def test_with_the_server_it_keeps_the_crown(mm, run):
    """RT-6.6, RT-6.17: by hand (runtimes without turn hooks) the state goes to
    the session's MCP server, which shows the 👑 it holds; no token needed."""
    live_crown(run)
    with hook_api(server_env(mm, run)) as bus:
        r = run(STATUS, "working", "proj", bus=None, **bus)
        assert r.code == 0
        until = str(int(time.time()) + 300)
        run(STATUS, "listening", until, bus=None, **bus)
    statuses = [b for m, p, b in mm.writes if p == "/api/v4/users/me/status/custom"]
    assert statuses[0]["emoji"] == "crown" and statuses[0]["text"] == "coordinator · working · proj · ~agents"
    assert statuses[1]["text"].startswith("coordinator · listening · until")


def test_another_sessions_server_is_not_asked(mm, run):
    """RT-6.6, RT-6.17: a server that serves another bot than MM_BOT_USER_ID
    names is never asked: by hand nothing changes (exit 1); the host hook
    writes its own bot's status itself."""
    live_crown(run)
    with hook_api(server_env(mm, run)) as bus:
        r = run(STATUS, "working", "proj", MM_BOT_USER_ID="z" * 26, bus=None, **bus)
        assert r.code == 1 and "another bot" in r.stderr and mm.writes == []
        run(STATUS, "working", "proj", MM_BOT_USER_ID=ME, fd_token=True,
            MM_BUS_KEY="wrong-key", MM_BUS_ENDPOINT=bus["MM_BUS_ENDPOINT"])
    assert mm.custom_status()["emoji"] == "crown"  # written directly, the lease's crown


def test_by_hand_without_a_server_it_says_what_to_do(mm, run):
    """RT-6.17 error handling: no token in an agent's shell, so without its
    session's MCP server the script changes nothing, names what to start
    (exit 3), and leaves offline to the server."""
    r = run(STATUS, "working", "proj", bus=None)
    assert r.code == 3 and "start the MCP server of this session" in r.stderr
    assert run(STATUS, "offline", bus=None).code == 2
    assert mm.writes == []


def test_offline_ends_the_status_and_the_lease_files(mm, run):
    """RT-6.1: the host's session end: offline, no custom status, no lease files."""
    live_crown(run)
    run(STATUS, "working", fd_token=True)
    assert run(STATUS, "offline", fd_token=True).code == 0
    assert mm.custom_status() is None
    assert ("PUT", f"/api/v4/users/{ME}/status", {"user_id": ME, "status": "offline"}) in mm.writes
    assert not list(run.state_dir.glob(f"*-{ME}.env"))


def test_a_wrong_call_says_how(run):
    assert run(STATUS, "listening", "soon").code == 2
    assert run(STATUS, "renew").code == 2

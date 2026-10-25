"""RT-6.6 crown prefix and heartbeat in the MCP server (docs/arc42/06-runtime-view.md)."""

import asyncio
import time

import pytest

from mattermost_ai_agent_bus import lease
from mattermost_ai_agent_bus.model import RosterEntry

UID = "u" * 26


def _coord(tmp_path, monkeypatch, **values):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = tmp_path / "mm-agent-bus"
    d.mkdir(exist_ok=True)
    (d / f"coord-{UID}.env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))


def test_coordinator_posts_start_with_the_crown(tmp_path, monkeypatch):
    """RT-6.6: bot posts never show a status, so the crown goes into the post."""
    _coord(tmp_path, monkeypatch, CROWN_UNTIL=int(time.time()) + 600)
    assert lease.with_crown("ACK", UID) == "👑 ACK"
    assert lease.with_crown("👑 ACK", UID) == "👑 ACK"


def test_no_prefix_without_a_live_crown(tmp_path, monkeypatch):
    _coord(tmp_path, monkeypatch, CROWN_UNTIL=1)
    assert lease.with_crown("ACK", UID) == "ACK"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "none"))
    assert lease.with_crown("ACK", UID) == "ACK"


@pytest.mark.asyncio
async def test_heartbeat_ticks_and_survives_errors(monkeypatch):
    """RT-6.6 step 1: the heartbeat renews the lease; a failure never stops it."""
    calls: list[str] = []

    class _Client:
        token = "t"
        session = None

    async def fake_run(client, command):
        calls.append(command)
        if len(calls) == 1:
            raise RuntimeError("bus down")
        return ""

    async def get_client():
        return _Client()

    monkeypatch.setattr(lease, "run", fake_run)
    monkeypatch.setattr(lease, "heartbeat_sec", lambda: 0.01)
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    task = asyncio.create_task(lease.heartbeat(get_client))
    await asyncio.sleep(0.05)
    task.cancel()
    assert calls.count("tick") >= 2


def test_roster_rows_become_status_words():
    """RT-6.14 step 4: the status text's word after "coordinator", else the
    lease emoji's; the custom status stays in lease.py."""
    rows = [{"username": "agent-b", "emoji": "crown", "coordinator": True, "crown": True,
             "text": "coordinator · working · bus"},
            {"username": "agent-a", "emoji": "ear", "text": ""},
            {"username": "agent-c", "emoji": "zzz", "presence": "away", "reachable": True}]
    assert [lease.entry(r) for r in rows] == [
        RosterEntry("agent-b", "working", coordinator=True, crown=True),
        RosterEntry("agent-a", "listening"),
        RosterEntry("agent-c", "idle"),
    ]


@pytest.mark.asyncio
async def test_roster_reads_mm_agent_roster_json(monkeypatch):
    """RT-6.6: rows from mm-agent-roster.sh --json; anything else reads as none."""
    out = '[{"username": "agent-a", "text": "working · bus"}, 3]'

    async def script(*_):
        return out
    monkeypatch.setattr(lease, "_script", script)
    assert await lease.roster(None) == [RosterEntry("agent-a", "working")]
    out = "not json"
    assert await lease.roster(None) == []

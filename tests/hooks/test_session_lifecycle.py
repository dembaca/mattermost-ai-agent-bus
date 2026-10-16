"""RT-6.1 host-managed session lifecycle (docs/arc42/06-runtime-view.md)."""

from __future__ import annotations

import json

START = "adapters/corral/10-mm-session-start.sh"
HOST = {"MM_BOT_TOKEN": None, "MM_BOT_USER_ID": None, "MM_TEAM_ID": None,
        "MM_CHANNEL_ID": None, "MM_TEAM": "team", "MM_CHANNEL": "agents",
        "CORRAL_SESSION_ID": "sess-abc123"}


def test_no_project_channel_no_bot(mm, run):
    """RT-6.1 step 2: without a project channel nothing is registered."""
    r = run(START, MM_REG_SECRET="test-secret", **HOST)
    assert r.code == 0 and r.stdout == ""
    assert "no MM_PROJECT_CHANNEL" in r.stderr
    assert not any(p == "/register/v1/agents" for _, p, _ in mm.writes)


def test_project_channel_registers_and_contributes_no_secret(mm, run):
    """RT-6.1 steps 3-5: registers, joins the project channel, hands over only the bot token."""
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x", **HOST)
    assert r.code == 0, r.stderr
    contribution = json.loads(r.stdout)
    env = contribution["env"]
    assert env["MM_BOT_TOKEN"] == "bot-token-for-tests"
    assert env["MM_PROJECT_CHANNEL"] == "proj-x"
    assert "MM_REG_SECRET" not in env and "MM_BOT_NAME" not in env
    assert "test-secret" not in r.stdout
    assert any(p.endswith("/members") for m, p, _ in mm.writes if m == "POST")


def test_register_always_without_project_channel(mm, run):
    """RT-6.1 step 2: MM_REGISTER=always registers every session."""
    r = run(START, MM_REG_SECRET="test-secret", MM_REGISTER="always", **HOST)
    assert r.code == 0, r.stderr
    assert json.loads(r.stdout)["env"]["MM_BOT_TOKEN"]

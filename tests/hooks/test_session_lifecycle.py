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
    assert "MM_REG_SECRET" not in env and "MM_REG_BOT_NAME" not in env
    assert "test-secret" not in r.stdout
    assert any(p.endswith("/members") for m, p, _ in mm.writes if m == "POST")


def test_register_always_without_project_channel(mm, run):
    """RT-6.1 step 2: MM_REGISTER=always registers every session."""
    r = run(START, MM_REG_SECRET="test-secret", MM_REGISTER="always", **HOST)
    assert r.code == 0, r.stderr
    assert json.loads(r.stdout)["env"]["MM_BOT_TOKEN"]


# --- RT-6.8 -------------------------------------------------------------------

def register_body(mm) -> dict:
    return next(b for m, p, b in mm.writes if p == "/register/v1/agents" and m == "POST")


def test_trusted_humans_come_from_the_host_list_only(mm, run):
    """RT-6.8 step 1: nothing inferred from MM_REG_SECRET (the registrar's shared
    secret); unknown names and bots are left out."""
    from conftest import OWNER
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x",
            MM_TRUSTED_HUMANS="dembaca, @kollegin, agent-x, ghost", **HOST)
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["env"]["MM_TRUSTED_HUMANS"] == "dembaca,kollegin"
    assert out["env"]["MM_TRUSTED_HUMAN_USER_IDS"] == f"{OWNER},{'k' * 26}"
    assert "trusted @dembaca, @kollegin" in out["status"][0]
    assert "'agent-x' not found" in r.stderr and "'ghost' not found" in r.stderr
    assert not any(p == "/api/v4/users/me" and "test-secret" in json.dumps(b)
                   for _, p, b in mm.writes)


def test_no_list_no_trusted_human(mm, run):
    """RT-6.8 error handling: without MM_TRUSTED_HUMANS approvals stay off, said in the banner."""
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x", **HOST)
    out = json.loads(r.stdout)
    assert "MM_TRUSTED_HUMANS" not in out["env"]
    assert "set MM_TRUSTED_HUMANS" in r.stderr and "trusted none" in out["status"][0]
    r = run(START, MM_REG_SECRET="test-secret", MM_OWNER="dembaca",
            MM_PROJECT_CHANNEL="proj-x", **HOST)
    assert json.loads(r.stdout)["env"]["MM_TRUSTED_HUMANS"] == "dembaca"


def test_registration_says_whom_the_agent_answers_to(mm, run):
    """RT-6.8 step 1: responsible, trusted and kind go to the registrar, which can
    put them into the bot's profile; the host does not patch the profile itself."""
    from conftest import ME
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x",
            MM_TRUSTED_HUMANS="dembaca,kollegin", **HOST)
    body = register_body(mm)
    assert body["responsible"] == "dembaca" and body["trusted"] == ["dembaca", "kollegin"]
    assert body["kind"] == "session"
    env = json.loads(r.stdout)["env"]
    assert env["MM_RESPONSIBLE_HUMAN"] == "dembaca" and env["MM_AGENT_KIND"] == "session"
    assert not any(p == f"/api/v4/users/{ME}/patch" for _, p, _ in mm.writes)
    assert "for @dembaca" in json.loads(r.stdout)["status"][0]


def test_autonomous_agent(mm, run):
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x",
            MM_RESPONSIBLE_HUMAN="@ops-team", MM_AGENT_KIND="autonomous", **HOST)
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["env"]["MM_RESPONSIBLE_HUMAN"] == "ops-team"
    assert out["env"]["MM_AGENT_KIND"] == "autonomous"
    assert "autonomous" in out["agentNotes"][0]
    assert register_body(mm)["kind"] == "autonomous"

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

def test_trusted_human_defaults_to_the_registration_pat_user(mm, run):
    """RT-6.8 step 1: the host looks the PAT's user up; the PAT stays outside."""
    from conftest import OWNER, PAT
    r = run(START, MM_REG_SECRET=PAT, MM_PROJECT_CHANNEL="proj-x", **HOST)
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["env"]["MM_TRUSTED_HUMANS"] == "dembaca"
    assert out["env"]["MM_TRUSTED_HUMAN_USER_IDS"] == OWNER
    assert PAT not in r.stdout
    assert "@dembaca" in out["agentNotes"][0] and "trusted @dembaca" in out["status"][0]


def test_trusted_humans_list_overrides_the_pat(mm, run):
    """RT-6.8 step 1: a team lists its humans; unknown names and bots are left out."""
    from conftest import OWNER, PAT
    r = run(START, MM_REG_SECRET=PAT, MM_PROJECT_CHANNEL="proj-x",
            MM_TRUSTED_HUMANS="dembaca, @kollegin, agent-x, ghost", **HOST)
    out = json.loads(r.stdout)
    assert out["env"]["MM_TRUSTED_HUMANS"] == "dembaca,kollegin"
    assert out["env"]["MM_TRUSTED_HUMAN_USER_IDS"] == f"{OWNER},{'k' * 26}"
    assert "trusted @dembaca, @kollegin" in out["status"][0]
    assert "'agent-x' not found" in r.stderr and "'ghost' not found" in r.stderr


def test_no_pat_no_trusted_human_and_old_owner_var(mm, run):
    """RT-6.8 error handling: no PAT → off, said in the banner; MM_OWNER still works."""
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x", **HOST)
    out = json.loads(r.stdout)
    assert "MM_TRUSTED_HUMANS" not in out["env"] and "approvals over the bus stay off" in r.stderr
    assert "trusted none" in out["status"][0]
    r = run(START, MM_REG_SECRET="test-secret", MM_OWNER="dembaca",
            MM_PROJECT_CHANNEL="proj-x", **HOST)
    assert json.loads(r.stdout)["env"]["MM_TRUSTED_HUMANS"] == "dembaca"


def test_profile_says_whom_the_agent_answers_to(mm, run):
    """RT-6.8 step 1: responsible and trusted humans go into the bot's profile,
    written with the PAT — a bot cannot change its own profile."""
    from conftest import ME, PAT
    r = run(START, MM_REG_SECRET=PAT, MM_PROJECT_CHANNEL="proj-x",
            MM_TRUSTED_HUMANS="dembaca,kollegin", **HOST)
    env = json.loads(r.stdout)["env"]
    assert env["MM_RESPONSIBLE_HUMAN"] == "dembaca" and env["MM_AGENT_KIND"] == "session"
    patch = [b for m, p, b in mm.writes if p == f"/api/v4/users/{ME}/patch"]
    assert patch == [{"position": "for @dembaca · trusts @dembaca, @kollegin"}]
    assert "for @dembaca" in json.loads(r.stdout)["status"][0]


def test_autonomous_agent_and_profile_without_permission(mm, run):
    """RT-6.8 error handling: an autonomous agent says so; a PAT that may not
    edit bots leaves the profile alone and the session goes on."""
    from conftest import ME, PAT
    mm.deny.add(f"/api/v4/users/{ME}/patch")
    r = run(START, MM_REG_SECRET=PAT, MM_PROJECT_CHANNEL="proj-x",
            MM_RESPONSIBLE_HUMAN="@ops-team", MM_AGENT_KIND="autonomous", **HOST)
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["env"]["MM_RESPONSIBLE_HUMAN"] == "ops-team"
    assert out["env"]["MM_AGENT_KIND"] == "autonomous"
    assert "autonomous" in out["agentNotes"][0]
    assert "could not write the bot profile" in r.stderr

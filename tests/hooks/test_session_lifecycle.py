"""RT-6.1 host-managed session lifecycle (docs/arc42/06-runtime-view.md)."""

from __future__ import annotations

import json
import os
import subprocess

from conftest import ROOT

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
    """RT-6.1 steps 3-5: registers, joins the project channel, hands over no
    credential: the token stays with the host process (RT-6.18)."""
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x", **HOST)
    assert r.code == 0, r.stderr
    contribution = json.loads(r.stdout)
    env = contribution["env"]
    assert "MM_BOT_TOKEN" not in env and "MATTERMOST_TOKEN" not in env
    assert "bot-token-for-tests" not in r.stdout
    assert env["MM_BUS_MCP_ENDPOINT"].startswith("tcp://127.0.0.1:") and env["MM_BUS_KEY"]
    assert env["MM_PROJECT_CHANNEL"] == "proj-x"
    assert "MM_REG_SECRET" not in env and "MM_REG_BOT_NAME" not in env
    assert env["MM_AGENT_PREFIX"] == "agent-"
    assert "test-secret" not in r.stdout
    assert any(p.endswith("/members") for m, p, _ in mm.writes if m == "POST")


def test_register_always_without_project_channel(mm, run):
    """RT-6.1 step 2: MM_REGISTER=always registers every session."""
    r = run(START, MM_REG_SECRET="test-secret", MM_REGISTER="always", **HOST)
    assert r.code == 0, r.stderr
    assert json.loads(r.stdout)["env"]["MM_BUS_KEY"]


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


def test_a_short_profile_is_warned_about(mm, run):
    """RT-6.8 step 1: the registrar may write only some of the trusted names. The
    bot reads its profile back and the banner names the missing ones; a profile
    that cannot be read is not compared."""
    env = dict(MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x", **HOST)
    mm.bot_description = "for @dembaca · trusts @dembaca · via agent-registrar"
    r = run(START, MM_TRUSTED_HUMANS="dembaca,kollegin", **env)
    assert "lacks some of the 2 trusted names (kollegin)" in r.stderr and "maxTrusted" in r.stderr
    mm.bot_description = "for @dembaca · trusts @dembaca, @kollegin · via agent-registrar"
    assert "lacks some" not in run(START, MM_TRUSTED_HUMANS="dembaca,Kollegin", **env).stderr
    mm.bot_description = None
    assert "lacks some" not in run(START, MM_TRUSTED_HUMANS="dembaca,kollegin", **env).stderr


def test_an_unbound_bot_is_warned_about(mm, run):
    """RT-6.16 step 1: registered with the shared secret, the bot is unbound and
    has no owner split; the banner says so, with or without trusted names."""
    env = dict(MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x", **HOST)
    mm.bot_description = "unbound · claims @dembaca · trusts @dembaca · via agent-registrar"
    for trusted in ("dembaca", ""):
        err = run(START, MM_TRUSTED_HUMANS=trusted, **env).stderr
        assert "is unbound (registered with the shared secret)" in err and "enrollment token" in err
    mm.bot_description = "for @dembaca · trusts @dembaca · via agent-registrar"
    assert "unbound" not in run(START, MM_TRUSTED_HUMANS="dembaca", **env).stderr


def test_autonomous_agent(mm, run):
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x",
            MM_RESPONSIBLE_HUMAN="@ops-team", MM_AGENT_KIND="autonomous", **HOST)
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["env"]["MM_RESPONSIBLE_HUMAN"] == "ops-team"
    assert out["env"]["MM_AGENT_KIND"] == "autonomous"
    assert "autonomous" in out["agentNotes"][0]
    assert register_body(mm)["kind"] == "autonomous"


def test_channel_push_reaches_the_sandbox(mm, run):
    """RT-6.12 trigger: the MCP server and the Stop hook inside the sandbox read
    MM_CHANNEL_PUSH; the host hook passes it on, no corral passthrough needed."""
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x",
            MM_CHANNEL_PUSH="1", **HOST)
    assert json.loads(r.stdout)["env"]["MM_CHANNEL_PUSH"] == "1"
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x", **HOST)
    assert "MM_CHANNEL_PUSH" not in json.loads(r.stdout)["env"]


def test_stats_channel_reaches_the_sandbox(mm, run):
    """RT-6.9 step 2: the MCP server inside the sandbox needs MM_STATS_CHANNEL."""
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x",
            MM_STATS_CHANNEL="agents-stats", **HOST)
    assert json.loads(r.stdout)["env"]["MM_STATS_CHANNEL"] == "agents-stats"
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x", **HOST)
    assert "MM_STATS_CHANNEL" not in json.loads(r.stdout)["env"]


def test_agent_note_names_humans_without_mentioning_them(mm, run):
    """RT-6.11: a note that says "@dembaca" teaches the agent to mention its human
    in every post; only ask_human may do that."""
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x",
            MM_TRUSTED_HUMANS="dembaca,kollegin", **HOST)
    note = json.loads(r.stdout)["agentNotes"][0]
    assert "Responsible for you: dembaca." in note
    assert "Trusted humans on the bus: dembaca, kollegin." in note and "ask_human" in note
    assert "@dembaca" not in note and "@kollegin" not in note


def test_agent_note_names_guest_channels_with_a_tilde(mm, run):
    """RT-6.22 step 1: the note says "~guest", not $HOME: bash 5 expands a "~"
    in ${var//#/~}."""
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x,guest-a,guest-b",
            **HOST)
    assert r.code == 0, r.stderr
    note = json.loads(r.stdout)["agentNotes"][0]
    assert "Guest channels ~guest-a, ~guest-b:" in note
    assert str(run.state_dir.parent) not in note


def test_the_registrar_prefix_reaches_the_sandbox(mm, run):
    """RT-6.1 step 5, arc42 §7.1: only the host knows the registered name, so it
    derives the prefix and hands it over; the name itself stays withheld."""
    mm.prefix = "bot-"
    r = run(START, MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x", **HOST)
    assert r.code == 0, r.stderr
    env = json.loads(r.stdout)["env"]
    assert env["MM_AGENT_PREFIX"] == "bot-" and env["MM_BOT_USERNAME"].startswith("bot-")
    assert "MM_REG_BOT_NAME" not in env
    assert "MM_AGENT_PREFIX=bot-" in (run.state_dir / "sess-abc123.env").read_text()


def test_shell_prefix_helper(tmp_path):
    """arc42 §7.1: mm_agent_prefix, the same rule as session_file.agent_prefix."""
    def prefix(**env: str) -> str:
        return subprocess.run(
            ["bash", "-c", f'source "{ROOT}/bin/mm-agent-lib.sh"; mm_agent_prefix'],
            env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), **env},
            capture_output=True, text=True, timeout=10).stdout

    assert prefix() == "agent-"
    assert prefix(MM_BOT_USERNAME="bot-mac-1", MM_REG_BOT_NAME="mac-1") == "bot-"
    assert prefix(MM_BOT_USERNAME="bot-mac-1", MM_REG_BOT_NAME="other") == "agent-"
    assert prefix(MM_BOT_USERNAME="mac-1", MM_REG_BOT_NAME="mac-1") == "agent-"
    assert prefix(MM_AGENT_PREFIX="ai-", MM_BOT_USERNAME="bot-mac-1",
                  MM_REG_BOT_NAME="mac-1") == "ai-"

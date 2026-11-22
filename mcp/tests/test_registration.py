"""The server drives the registrar session through registration.RegistrationPort
(arc42 §5.2): the bot's start, take-over, keep and end, against FakeRegistrar,
with no registrar and no Mattermost (RT-6.1, RT-6.10, RT-6.18)."""

from __future__ import annotations

import json

import pytest
from fakes import BOT, FakeRegistrar

from mattermost_ai_agent_bus import ownpost, server
from mattermost_ai_agent_bus.session_file import SessionInfo

KEPT = SessionInfo(url="https://mm.example", name="agent-old", username="agent-agent-old",
                   user_id="k" * 26, bot_token="t")


@pytest.fixture
def exit_state(monkeypatch):
    monkeypatch.setattr(server, "_exit_handled", False)
    monkeypatch.setattr(server, "_confirmed", None)
    monkeypatch.setattr(server, "_hook_api", None)
    monkeypatch.setattr(server.stats, "summary", lambda *a: None)


def test_the_server_takes_over_the_kept_bot(monkeypatch):
    """RT-6.10: the bot of the server that ended becomes this server's, and
    the stats count for it."""
    users: list[str] = []
    monkeypatch.setattr(server.stats, "set_user", users.append)
    reg = FakeRegistrar(kept=KEPT)
    server._adopt(reg)
    assert reg.session == KEPT and users == [KEPT.user_id]
    server._adopt(reg)  # nothing left to take over
    assert users == [KEPT.user_id]


def test_a_kept_bot_signs_off_with_no_bye(monkeypatch, exit_state):
    """RT-6.10: a bot kept for the next server goes without BYE and without
    being unregistered."""
    byes: list[str] = []
    monkeypatch.setattr(ownpost, "bye_sync", lambda *_: byes.append("bye") or "BYE posted")
    reg = FakeRegistrar(keeps=True)
    reg.session = reg.info
    monkeypatch.setattr(server, "_client", reg)
    server._cleanup_on_exit()
    assert byes == [] and reg.calls == ["shutdown_sync"] and reg.session is None


def test_a_bot_not_kept_says_bye_then_goes(monkeypatch, exit_state):
    """RT-6.10: BYE first, then unregistered."""
    order: list[str] = []
    monkeypatch.setattr(ownpost, "bye_sync", lambda _c, uid: order.append(f"bye {uid}") or "BYE posted")
    reg = FakeRegistrar()
    reg.session = reg.info
    monkeypatch.setattr(server, "_client", reg)
    server._cleanup_on_exit()
    assert order == [f"bye {BOT}"] and reg.calls == ["shutdown_sync"]


def test_the_host_keeps_the_bot_offline_only(monkeypatch, exit_state):
    """RT-6.18 step 9: corral's postEnd keeps the bot; the server only goes."""
    reg = FakeRegistrar()
    reg.session = reg.info
    monkeypatch.setattr(server, "_client", reg)
    server._keep_on_exit()
    assert reg.calls == ["keep_sync"]
    server._cleanup_on_exit()  # handled already
    assert reg.calls == ["keep_sync"]


@pytest.mark.anyio
async def test_session_start_registers_and_sets_up(monkeypatch):
    """RT-6.1: a new bot is registered, then joins its channels."""
    async def no_attestation(_client):
        return None

    monkeypatch.setattr(server, "corral_sandbox", lambda: None)
    monkeypatch.setattr(server, "_roots_pending", False)
    monkeypatch.setattr(server.attest, "start", no_attestation)
    monkeypatch.setattr(server.stats, "set_user", lambda _uid: None)
    reg = FakeRegistrar(setup={"project_channel": {"name": "dev", "joined": True}})
    reg.reaped = ["agent-gone"]
    monkeypatch.setattr(server, "_get_client", lambda: reg)
    out = json.loads(await server.session_start("x"))
    assert out["user_id"] == BOT and out["project_channel"]["joined"]
    assert out["reaped_stale_bots"] == ["agent-gone"]
    assert reg.calls == ["session_start x", "finish_session_setup"]


@pytest.mark.anyio
async def test_session_start_returns_this_sessions_bot(monkeypatch):
    """RT-6.10: after a take-over the bot is reused, nothing set up again."""
    monkeypatch.setattr(server, "corral_sandbox", lambda: None)
    monkeypatch.setattr(server, "_roots_pending", False)
    reg = FakeRegistrar()
    reg.session = KEPT
    monkeypatch.setattr(server, "_get_client", lambda: reg)
    out = json.loads(await server.session_start("x"))
    assert out["reused"] and out["user_id"] == KEPT.user_id
    assert reg.calls == ["session_start x"]


@pytest.mark.anyio
async def test_session_end_unregisters_the_bot(monkeypatch):
    """RT-6.1: the tool ends this server's bot and names it."""
    monkeypatch.setattr(server, "_inbox", None)
    reg = FakeRegistrar()
    reg.session = reg.info
    monkeypatch.setattr(server, "_get_client", lambda: reg)
    out = json.loads(await server.session_end())
    assert out == {"ok": True, "ended": "agent-x"} and reg.session is None

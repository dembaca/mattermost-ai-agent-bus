"""RT-6.8: the profile the registrar writes against the trusted list the bus holds."""

from __future__ import annotations

import httpx
import pytest

from mattermost_ai_agent_bus import profile
from mattermost_ai_agent_bus.client import MattermostAdapter

NAMES = [f"user{i}" for i in range(12)]


def _desc(*names):
    return f"for @{names[0]} · trusts @{', @'.join(names)} · via agent-registrar"


def test_a_profile_with_every_name_needs_no_warning():
    assert profile.warning("agent-x", NAMES[:3], _desc(*NAMES[:3])) == ""
    assert profile.warning("agent-x", ["Alice"], _desc("alice")) == ""


def test_names_the_profile_lacks_are_listed():
    text = profile.warning("agent-x", NAMES, _desc(*NAMES[:10]))
    assert "lacks 2 of 12" in text and "user10, user11" in text and "maxTrusted" in text
    assert "lacks 1 of 1" in profile.warning("agent-x", ["bob"], "Registered via agent-registrar")


def test_an_unreadable_profile_is_not_compared():
    assert profile.warning("agent-x", NAMES, None) == ""


def _client(handler):
    c = MattermostAdapter(base_url="https://mm.example", reg_secret="s",
                         register_url="https://mm.example/register/v1/agents")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def _handler(user=None, bot=None):
    """The registrar, and a server whose user object / bots endpoint answer as given."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v4/users/uid90000000000000000000000":
            return httpx.Response(200, json=user) if user is not None else httpx.Response(403)
        if req.url.path == "/api/v4/bots/uid90000000000000000000000":
            return httpx.Response(200, json=bot) if bot is not None else httpx.Response(403)
        return httpx.Response(200, json={"username": "agent-x", "user_id": "uid90000000000000000000000", "bot_token": "t"})
    return handler


@pytest.mark.asyncio
async def test_session_start_warns_about_a_short_profile(monkeypatch):
    """Step 1: the registration carries the whole list; the bot reads its profile
    back from its user object and the result says what is missing."""
    monkeypatch.setenv("MM_TRUSTED_HUMANS", ",".join(NAMES))
    c = _client(_handler(user={"bot_description": _desc(*NAMES[:10])}))
    await c.session_start("proj")
    assert "lacks 2 of 12" in c.profile_warning and "user10, user11" in c.profile_warning
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "user0,user1")
    await c.session_end("proj")
    await c.session_start("proj")
    assert c.profile_warning == ""


@pytest.mark.asyncio
async def test_the_bots_endpoint_is_the_second_try(monkeypatch):
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "user0,user1")
    c = _client(_handler(user={"id": "uid90000000000000000000000"}, bot={"description": _desc("user0")}))
    await c.session_start("proj")
    assert "lacks 1 of 2" in c.profile_warning and "user1" in c.profile_warning


@pytest.mark.asyncio
async def test_an_unreadable_profile_gives_no_warning(monkeypatch, caplog):
    monkeypatch.setenv("MM_TRUSTED_HUMANS", ",".join(NAMES))
    c = _client(_handler())
    with caplog.at_level("INFO"):
        await c.session_start("proj")
    assert c.profile_warning == "" and "profile not readable" in caplog.text

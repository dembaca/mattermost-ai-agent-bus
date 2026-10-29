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
    monkeypatch.delenv("MM_RESPONSIBLE_HUMAN", raising=False)
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


# WP3: the registrar's bound and unbound descriptions, as it writes them.
BOUND = "for @alice · autonomous · trusts @alice, @bob +2 · via agent-registrar"
UNBOUND = "unbound · claims @alice · autonomous · trusts @alice, @bob · via agent-registrar"
BARE = "unbound · via agent-registrar"
NONE_WRITTEN = "for @alice · trusts @ +3 · via agent-registrar"


def test_a_bound_description():
    """`autonomous` is no name; `+2` marks a cut list."""
    assert profile.owner(BOUND) == "alice" and not profile.unbound(BOUND)
    assert profile.listed(BOUND) == {"alice", "bob"} and profile.cut(BOUND)


def test_an_unbound_description_names_no_owner():
    """RT-6.16 step 1: `claims @alice` is unchecked and never the owner."""
    assert profile.unbound(UNBOUND) and profile.owner(UNBOUND) == ""
    assert profile.listed(UNBOUND) == {"alice", "bob"} and not profile.cut(UNBOUND)
    assert profile.unbound(BARE) and profile.owner(BARE) == "" and profile.listed(BARE) == set()
    assert not profile.unbound("for @unbound · via agent-registrar")


def test_a_cut_list_with_no_name_written():
    assert profile.listed(NONE_WRITTEN) == set() and profile.cut(NONE_WRITTEN)
    assert profile.owner(NONE_WRITTEN) == "alice"


@pytest.mark.asyncio
async def test_session_start_warns_about_an_unbound_bot(monkeypatch):
    """RT-6.16 step 1: registered with the shared secret, the bot is unbound;
    the result says so, trusted names or not."""
    monkeypatch.delenv("MM_TRUSTED_HUMANS", raising=False)
    c = _client(_handler(user={"bot_description": BARE}))
    await c.session_start("proj")
    assert "@agent-x is unbound" in c.profile_warning and "enrollment token" in c.profile_warning


UID, OWNER = "uid90000000000000000000000", "own90000000000000000000000"


def _owner_client(description, bot, owner_user=None):
    """A server with the bot's description, its /bots answer (None: 403) and its owner's account."""
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        if req.url.path == f"/api/v4/users/{UID}":
            return httpx.Response(200, json={"id": UID, "is_bot": True, "bot_description": description})
        if req.url.path == f"/api/v4/bots/{UID}":
            return httpx.Response(200, json=bot) if bot is not None else httpx.Response(403)
        if req.url.path == "/api/v4/users/ids":
            return httpx.Response(200, json=[owner_user] if owner_user else [])
        return httpx.Response(404)
    c = _client(handler)
    c.token = "t"
    return c, seen


@pytest.mark.asyncio
@pytest.mark.parametrize(("description", "bot", "owner_user", "want"), [
    # the native owner: read with read_others_bots, and it agrees
    (BOUND, {"owner_id": OWNER}, {"id": OWNER, "username": "Alice"}, "alice"),
    # a bot made by hand, no `for @`: its native owner
    ("", {"owner_id": OWNER}, {"id": OWNER, "username": "carol"}, "carol"),
    # 403 without read_others_bots: the description decides
    (BOUND, None, None, "alice"),
    # native owner and `for @` disagree: no owner, so foreign
    (BOUND, {"owner_id": OWNER}, {"id": OWNER, "username": "bob"}, ""),
    # unbound, or written by the registrar without `for @`: the registrar's
    # own account owns it, a human user or not, and is never an owner
    (UNBOUND, {"owner_id": OWNER}, {"id": OWNER, "username": "admin"}, ""),
    ("Registered via agent-registrar", {"owner_id": OWNER}, {"id": OWNER, "username": "admin"}, ""),
    # an owner that is no human (a bot, a plugin id): ignored
    ("", {"owner_id": OWNER}, {"id": OWNER, "username": "agent-z", "is_bot": True}, ""),
    ("", {"owner_id": "com.mattermost.calls"}, None, ""),
], ids=["native", "native-hand-made", "unreadable-fallback", "mismatch", "unbound",
        "registrar-no-owner", "owner-is-a-bot", "owner-is-a-plugin"])
async def test_agent_owner(description, bot, owner_user, want, caplog):
    """RT-6.16 step 1, WP3: ChatPort.agent_owner in the adapter."""
    c, seen = _owner_client(description, bot, owner_user)
    with caplog.at_level("WARNING"):
        assert await c.agent_owner(UID) == want
    assert ("native owner @bob but description for @alice" in caplog.text) == (want == "" and description == BOUND)
    n = len(seen)
    assert await c.agent_owner(UID) == want and len(seen) == n  # cached


def test_the_owner_warning_only_on_a_known_mismatch():
    """ADR-003: the own bot's owner against the responsible human."""
    assert profile.owner_warning("agent-x", "alice", "Alice") == ""
    assert profile.owner_warning("agent-x", "", "alice") == ""
    assert profile.owner_warning("agent-x", "alice", "") == ""
    text = profile.owner_warning("agent-x", "bob", "alice")
    assert "@agent-x is owned by @bob, not by the responsible human @alice" in text


@pytest.mark.asyncio
@pytest.mark.parametrize(("responsible", "warns"), [("alice", False), ("carol", True), ("", False)])
async def test_session_start_warns_when_the_own_bot_has_another_owner(monkeypatch, caplog, responsible, warns):
    """RT-6.16 step 1, ADR-003: the own bot's owner (agent_owner: native owner,
    else `for @`) differs from MM_RESPONSIBLE_HUMAN: a warning in the result
    and the log, nothing else."""
    monkeypatch.delenv("MM_TRUSTED_HUMANS", raising=False)
    monkeypatch.delenv("MM_OWNER", raising=False)
    monkeypatch.setenv("MM_RESPONSIBLE_HUMAN", responsible)
    c = _client(_handler(user={"bot_description": "for @alice · via agent-registrar"}))
    with caplog.at_level("WARNING"):
        await c.session_start("proj")
    assert ("owned by @alice, not by the responsible human @carol" in c.profile_warning) == warns
    assert ("owner vs responsible human" in caplog.text) == warns

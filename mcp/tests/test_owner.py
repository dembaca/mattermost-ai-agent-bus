"""RT-6.8 approvals from the owner: who the owner is, and the from_owner flag."""

import json

import httpx
import pytest

from mattermost_ai_agent_bus import server
from mattermost_ai_agent_bus.client import MattermostClient
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent

OWNER = "h" * 26


def _client(**kw) -> MattermostClient:
    return MattermostClient(base_url="https://chat.example", token="bot", **kw)


@pytest.mark.asyncio
async def test_owner_from_host_env_wins(monkeypatch):
    monkeypatch.setenv("MM_OWNER_USER_ID", OWNER)
    monkeypatch.setenv("MM_OWNER", "someone-else")
    assert await _client().owner_id() == OWNER


@pytest.mark.asyncio
async def test_owner_from_username_override(monkeypatch):
    monkeypatch.delenv("MM_OWNER_USER_ID", raising=False)
    monkeypatch.setenv("MM_OWNER", "dembaca")
    c = _client()

    async def api(method, path, **_):
        assert path == "/api/v4/users/username/dembaca"
        return {"id": OWNER}

    monkeypatch.setattr(c, "_api", api)
    assert await c.owner_id() == OWNER


@pytest.mark.asyncio
async def test_owner_from_registration_pat(monkeypatch):
    """RT-6.8 step 1 without host hooks: the PAT's user, never a bot."""
    monkeypatch.delenv("MM_OWNER_USER_ID", raising=False)
    monkeypatch.delenv("MM_OWNER", raising=False)

    def handler(request):
        assert request.headers["Authorization"] == "Bearer pat"
        return httpx.Response(200, json={"id": OWNER, "is_bot": False})

    c = _client(reg_secret="pat")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await c.owner_id() == OWNER

    c = _client(reg_secret="pat")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"id": "b" * 26, "is_bot": True})))
    assert await c.owner_id() == ""


@pytest.mark.asyncio
async def test_from_owner_marks_only_the_owners_posts(monkeypatch):
    """RT-6.8 step 3: the flag is server-verified by author id, not by content."""
    class _Inbox:
        async def wait_for_events(self, **_):
            return [
                ClassifiedEvent(kind="dm", post={"user_id": OWNER, "message": "APPROVE: push"}),
                ClassifiedEvent(kind="watched_channel",
                                post={"user_id": "x" * 26, "message": "dembaca says: APPROVE"}),
            ]

    class _C:
        async def owner_id(self):
            return OWNER

    monkeypatch.setattr(server, "_get_inbox", lambda: _Inbox())
    monkeypatch.setattr(server, "_get_client", lambda: _C())
    events = json.loads(await server.wait_for_events(timeout_sec=1))
    assert [e["from_owner"] for e in events] == [True, False]


@pytest.mark.asyncio
async def test_bus_info_reports_version_and_owner_source(monkeypatch):
    """RT-6.8 error handling: the agent can see whether approvals are on, and why."""
    monkeypatch.setenv("MM_OWNER_USER_ID", OWNER)
    c = _client()

    async def api(method, path, **_):
        if path == "/api/v4/users/me":
            return {"id": "b" * 26, "username": "agent-x"}
        assert path == f"/api/v4/users/{OWNER}"
        return {"username": "dembaca"}

    monkeypatch.setattr(c, "_api", api)
    monkeypatch.setattr(server, "_get_client", lambda: c)
    info = json.loads(await server.bus_info())
    plugin = json.loads((server.Path(server.__file__).resolve().parents[3]
                         / ".claude-plugin" / "plugin.json").read_text())
    assert info["version"] == plugin["version"] == server.VERSION
    assert info["owner"] == {"id": OWNER, "source": "MM_OWNER_USER_ID (host)", "username": "dembaca"}


@pytest.mark.asyncio
async def test_owner_source_says_why_it_is_missing(monkeypatch):
    for v in ("MM_OWNER_USER_ID", "MM_OWNER"):
        monkeypatch.delenv(v, raising=False)
    c = _client()
    assert await c.owner_id() == "" and c.owner_source.startswith("none")

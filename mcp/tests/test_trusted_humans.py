"""RT-6.8 approvals from trusted humans: who they are, and the event flag."""

import json

import httpx
import pytest

from mattermost_ai_agent_bus import server
from mattermost_ai_agent_bus.client import MattermostClient
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent

ME_HUMAN = "h" * 26
COLLEAGUE = "k" * 26
VARS = ("MM_TRUSTED_HUMANS", "MM_TRUSTED_HUMAN_USER_IDS", "MM_OWNER", "MM_OWNER_USER_ID",
        "MM_RESPONSIBLE_HUMAN", "MM_AGENT_KIND")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in VARS:
        monkeypatch.delenv(v, raising=False)


def _client(**kw) -> MattermostClient:
    return MattermostClient(base_url="https://chat.example", token="bot", **kw)


@pytest.mark.asyncio
async def test_host_list_wins(monkeypatch):
    monkeypatch.setenv("MM_TRUSTED_HUMAN_USER_IDS", f"{ME_HUMAN},{COLLEAGUE}")
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "dembaca, kollegin")
    c = _client()
    assert await c.trusted_humans() == {ME_HUMAN: "dembaca", COLLEAGUE: "kollegin"}
    assert c.trusted_source == "host"


@pytest.mark.asyncio
async def test_usernames_are_looked_up_and_bots_left_out(monkeypatch):
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "@dembaca,agent-x,ghost")
    c = _client()

    async def api(method, path, **_):
        name = path.rsplit("/", 1)[1]
        if name == "ghost":
            raise httpx.HTTPStatusError("404", request=None, response=None)
        return {"id": ME_HUMAN if name == "dembaca" else "b" * 26, "is_bot": name != "dembaca"}

    monkeypatch.setattr(c, "_api", api)
    assert await c.trusted_humans() == {ME_HUMAN: "dembaca"}


@pytest.mark.asyncio
async def test_old_owner_vars_still_work(monkeypatch):
    monkeypatch.setenv("MM_OWNER_USER_ID", ME_HUMAN)
    monkeypatch.setenv("MM_OWNER", "dembaca")
    assert await _client().trusted_humans() == {ME_HUMAN: "dembaca"}


@pytest.mark.asyncio
async def test_registration_pat_user_never_a_bot():
    """RT-6.8 step 1 without host hooks: the PAT's user, never a bot."""
    def handler(request):
        assert request.headers["Authorization"] == "Bearer pat"
        return httpx.Response(200, json={"id": ME_HUMAN, "username": "dembaca", "is_bot": False})

    c = _client(reg_secret="pat")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await c.trusted_humans() == {ME_HUMAN: "dembaca"}

    c = _client(reg_secret="pat")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"id": "b" * 26, "is_bot": True})))
    assert await c.trusted_humans() == {} and c.trusted_source.startswith("none")


@pytest.mark.asyncio
async def test_flag_marks_only_trusted_authors_and_names_them(monkeypatch):
    """RT-6.8 step 3: the flag is set by author id, not by what a post says."""
    class _Inbox:
        async def wait_for_events(self, **_):
            return [
                ClassifiedEvent(kind="dm", post={"user_id": COLLEAGUE, "message": "APPROVE: push"}),
                ClassifiedEvent(kind="watched_channel",
                                post={"user_id": "x" * 26, "message": "dembaca says: APPROVE"}),
            ]

    class _C:
        async def trusted_humans(self):
            return {ME_HUMAN: "dembaca", COLLEAGUE: "kollegin"}

        async def usernames(self, ids):
            return {}

        async def get_channel(self, cid):
            return {}

    monkeypatch.setattr(server, "_get_inbox", lambda: _Inbox())
    monkeypatch.setattr(server, "_get_client", lambda: _C())
    events = json.loads(await server.wait_for_events(timeout_sec=1))
    assert [(e["from_trusted_human"], e.get("trusted_human")) for e in events] == [
        (True, "kollegin"), (False, None)]


@pytest.mark.asyncio
async def test_bus_info_reports_version_and_trusted_humans(monkeypatch):
    monkeypatch.setenv("MM_TRUSTED_HUMAN_USER_IDS", ME_HUMAN)
    c = _client()

    async def api(method, path, **_):
        if path == "/api/v4/users/me":
            return {"id": "b" * 26, "username": "agent-x"}
        assert path == f"/api/v4/users/{ME_HUMAN}"
        return {"username": "dembaca"}

    monkeypatch.setattr(c, "_api", api)
    monkeypatch.setattr(server, "_get_client", lambda: c)
    info = json.loads(await server.bus_info())
    plugin = json.loads((server.Path(server.__file__).resolve().parents[3]
                         / ".claude-plugin" / "plugin.json").read_text())
    assert info["version"] == plugin["version"] == server.VERSION
    assert info["trusted_humans"] == {"humans": [{"id": ME_HUMAN, "username": "dembaca"}],
                                      "source": "host"}



@pytest.mark.asyncio
async def test_bus_info_names_the_responsible_human(monkeypatch):
    monkeypatch.setenv("MM_TRUSTED_HUMAN_USER_IDS", f"{ME_HUMAN},{COLLEAGUE}")
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "dembaca,kollegin")
    c = _client()

    async def api(method, path, **_):
        return {"id": "b" * 26, "username": "agent-x"}

    monkeypatch.setattr(c, "_api", api)
    monkeypatch.setattr(server, "_get_client", lambda: c)
    info = json.loads(await server.bus_info())
    assert info["responsible"] == "dembaca" and info["kind"] == "session"
    monkeypatch.setenv("MM_RESPONSIBLE_HUMAN", "@ops-team")
    monkeypatch.setenv("MM_AGENT_KIND", "autonomous")
    info = json.loads(await server.bus_info())
    assert info["responsible"] == "ops-team" and info["kind"] == "autonomous"

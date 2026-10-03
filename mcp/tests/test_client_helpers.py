"""Tests for client helpers (no live Mattermost)."""

import pytest

from mattermost_ai_agent_bus.client import MattermostClient, ws_url_from_http


@pytest.mark.parametrize(
    "http,expected",
    [
        ("https://chat.example.com", "wss://chat.example.com/api/v4/websocket"),
        ("https://chat.example.com/", "wss://chat.example.com/api/v4/websocket"),
        (
            "http://localhost:8065",
            "ws://localhost:8065/api/v4/websocket",
        ),
        (
            "https://chat.example.com/sub",
            "wss://chat.example.com/sub/api/v4/websocket",
        ),
        (
            "https://chat.example.com/api/v4",
            "wss://chat.example.com/api/v4/websocket",
        ),
    ],
)
def test_ws_url_from_http(http, expected):
    assert ws_url_from_http(http) == expected


def test_from_env_requires_url(monkeypatch):
    monkeypatch.delenv("MM_CHAT_URL", raising=False)
    monkeypatch.delenv("MATTERMOST_URL", raising=False)
    with pytest.raises(RuntimeError, match="MM_CHAT_URL"):
        MattermostClient.from_env()


def test_from_env_aliases(monkeypatch):
    monkeypatch.setenv("MATTERMOST_URL", "https://mm.example/")
    monkeypatch.setenv("MATTERMOST_TOKEN", "tok")
    monkeypatch.setenv("MM_TEAM", "t1")
    monkeypatch.setenv("MM_CHANNEL", "c1")
    monkeypatch.delenv("MM_CHAT_URL", raising=False)
    monkeypatch.delenv("MM_BOT_TOKEN", raising=False)
    c = MattermostClient.from_env()
    assert c.base_url == "https://mm.example"
    assert c.token == "tok"
    assert c.team_name == "t1"
    assert c.channel_name == "c1"
    assert c.register_url == "https://mm.example/register/v1/agents"


def test_unexpanded_placeholders_are_treated_as_unset(monkeypatch):
    """A plugin manifest passes "${VAR}"; unset vars can arrive verbatim.

    Regression: the literal "${MM_PROJECT_CHANNEL}" was taken for a channel name,
    so every post_message 404'd while an explicit channel_id still worked.
    """
    monkeypatch.setenv("MM_CHAT_URL", "https://mm.example")
    monkeypatch.setenv("MM_CHANNEL", "agents")
    monkeypatch.setenv("MM_PROJECT_CHANNEL", "${MM_PROJECT_CHANNEL}")
    monkeypatch.setenv("MM_REGISTER_URL", "${MM_REGISTER_URL}")
    monkeypatch.setenv("MM_REG_SECRET", "${MM_REG_SECRET}")
    monkeypatch.setenv("MM_BOT_TOKEN", "${MM_BOT_TOKEN}")
    c = MattermostClient.from_env()
    assert c.project_channel_name == ""
    assert c.work_channel_name == "agents"
    assert c.register_url == "https://mm.example/register/v1/agents"
    assert c.reg_secret is None
    assert c.token is None


def test_placeholder_like_value_that_is_real_is_kept(monkeypatch):
    """Only a bare ${NAME} is ignored — a real name containing $ is not."""
    monkeypatch.setenv("MM_CHAT_URL", "https://mm.example")
    monkeypatch.setenv("MM_PROJECT_CHANNEL", "proj-${weird}-name")
    c = MattermostClient.from_env()
    assert c.project_channel_name == "proj-${weird}-name"


def test_from_env_project_channel_defaults_to_empty(monkeypatch):
    monkeypatch.setenv("MM_CHAT_URL", "https://mm.example")
    monkeypatch.setenv("MM_CHANNEL", "agents")
    monkeypatch.delenv("MM_PROJECT_CHANNEL", raising=False)
    c = MattermostClient.from_env()
    assert c.project_channel_name == ""
    # Without a project channel the work channel is the default channel.
    assert c.work_channel_name == "agents"


def test_project_channel_becomes_work_channel(monkeypatch):
    monkeypatch.setenv("MM_CHAT_URL", "https://mm.example")
    monkeypatch.setenv("MM_CHANNEL", "agents")
    monkeypatch.setenv("MM_PROJECT_CHANNEL", "proj-bus")
    c = MattermostClient.from_env()
    # The default channel is kept: the agent stays addressable there.
    assert c.channel_name == "agents"
    assert c.project_channel_name == "proj-bus"
    assert c.work_channel_name == "proj-bus"


@pytest.mark.asyncio
async def test_get_channel_id_caches_per_name(monkeypatch):
    """Both channels resolve independently and each is fetched only once."""
    c = MattermostClient(
        base_url="https://mm.example",
        token="tok",
        channel_name="agents",
        project_channel_name="proj-bus",
    )
    calls: list[str] = []

    async def fake_api(method, path, **kwargs):
        calls.append(path)
        if path.startswith("/api/v4/teams/name/"):
            return {"id": "team1"}
        return {"id": f"cid-{path.rsplit('/', 1)[-1]}"}

    monkeypatch.setattr(c, "_api", fake_api)

    assert await c.get_channel_id() == "cid-proj-bus"  # default = work channel
    assert await c.get_channel_id("agents") == "cid-agents"
    assert await c.get_channel_id() == "cid-proj-bus"  # cached, no new call

    channel_calls = [p for p in calls if "/channels/name/" in p]
    assert len(channel_calls) == 2


@pytest.mark.asyncio
async def test_session_start_name_validation():
    c = MattermostClient(
        base_url="https://mm.example",
        reg_secret="sec",
        register_url="https://mm.example/register/v1/agents",
    )
    with pytest.raises(ValueError):
        await c.session_start("X")

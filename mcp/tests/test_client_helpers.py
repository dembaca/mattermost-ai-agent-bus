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


@pytest.mark.asyncio
async def test_session_start_name_validation():
    c = MattermostClient(
        base_url="https://mm.example",
        reg_secret="sec",
        register_url="https://mm.example/register/v1/agents",
    )
    with pytest.raises(ValueError):
        await c.session_start("X")

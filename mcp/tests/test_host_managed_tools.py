"""Host-managed sessions are not offered the lifecycle tools (RT-6.1)."""

import asyncio

from mattermost_ai_agent_bus import server


def _names():
    return {t.name for t in asyncio.run(server.mcp.list_tools())}


def test_lifecycle_tools_hidden_when_host_managed(monkeypatch):
    removed = []
    monkeypatch.setattr(server.mcp, "remove_tool", removed.append)
    monkeypatch.setattr(server, "HOST_MANAGED", True)
    monkeypatch.setattr(server, "PUSH", True)
    server._hide_lifecycle_tools()
    assert removed == ["session_start", "session_end"]


def test_channel_ok_only_for_a_channel(monkeypatch):
    """RT-6.12: without MM_CHANNEL_PUSH there is no check to confirm."""
    removed = []
    monkeypatch.setattr(server.mcp, "remove_tool", removed.append)
    monkeypatch.setattr(server, "HOST_MANAGED", False)
    monkeypatch.setattr(server, "PUSH", False)
    server._hide_lifecycle_tools()
    assert removed == ["channel_ok"]


def test_lifecycle_tools_offered_without_a_token(monkeypatch):
    monkeypatch.setattr(server, "HOST_MANAGED", False)
    monkeypatch.setattr(server, "PUSH", True)
    server._hide_lifecycle_tools()
    assert {"session_start", "session_end"} <= _names()

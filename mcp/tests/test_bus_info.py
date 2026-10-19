"""bus_info off the bus says so instead of failing (RT-6.1, step 3)."""

import asyncio
import json

from mattermost_ai_agent_bus import server


def test_off_the_bus(monkeypatch):
    for var in ("MM_CHAT_URL", "MATTERMOST_URL", "MM_BOT_TOKEN", "MATTERMOST_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server, "_client", None)
    out = json.loads(asyncio.run(server.bus_info()))
    assert out["on_bus"] is False and out["version"] == server.VERSION
    assert "required" in out["reason"]
    assert server._client is None

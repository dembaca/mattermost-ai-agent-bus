"""RT-6.9 overhead counters in the MCP server: counting, summary, the STATS post."""

import json

import httpx
import pytest

from mattermost_ai_agent_bus import server, stats
from mattermost_ai_agent_bus.client import MattermostClient

UID = "u" * 26


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    monkeypatch.delenv("MM_STATS_CHANNEL", raising=False)
    return tmp_path


def test_tool_results_and_posts_are_counted():
    text = server._out("list_recent", [{"id": "p1", "message": "hi"}])
    stats.record("post", chars=42)
    stats.record("wake", reason="digest")
    s = stats.summary("0.21.0", 29500)
    assert s["tools"] == {"list_recent": [1, len(text)]}
    assert s["posts"] == [1, 42] and s["wakes"] == {"digest": 1} and s["static"] == 29500
    assert "message" not in json.dumps(s)  # counts only, no content


def test_static_text_is_measured():
    assert server._static_chars() > 10000


def test_post_goes_to_the_stats_channel_only_when_configured(monkeypatch, state):
    client = MattermostClient(base_url="https://chat.example", token="bot", team_name="ai-agents")
    stats.record("turn")
    data = stats.summary("0.21.0", 1)
    assert stats.post_sync(client, data) == "off"

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/teams/name/ai-agents"):
            return httpx.Response(200, json={"id": "t1"})
        if request.url.path.endswith("/channels/name/agents-stats"):
            return httpx.Response(200, json={"id": "c9"})
        if request.url.path == "/api/v4/posts":
            body = json.loads(request.content)
            assert body["channel_id"] == "c9" and body["message"].startswith('STATS {"v":"0.21.0"')
        return httpx.Response(201, json={})

    monkeypatch.setenv("MM_STATS_CHANNEL", "~agents-stats")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    assert stats.post_sync(client, data, http=http) == "posted"
    assert ("POST", "/api/v4/channels/c9/members") in seen
    assert not (state / "mm-agent-bus" / f"stats-{UID}.jsonl").exists()

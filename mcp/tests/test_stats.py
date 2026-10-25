"""RT-6.9 overhead counters in the MCP server: counting, summary, the STATS post."""

import json
import time

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


def test_default_channel_and_off(monkeypatch, state):
    assert stats.channel() == "agents-stats"
    monkeypatch.setenv("MM_STATS_CHANNEL", "off")
    stats.record("turn")
    assert stats.channel() == "" and stats.summary("0.21.0", 1) is None


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


def test_post_goes_to_the_stats_channel(monkeypatch, state):
    client = MattermostClient(base_url="https://chat.example", token="bot", team_name="ai-agents")
    stats.record("turn")
    data = stats.summary("0.21.0", 1)

    def missing(request):
        if request.url.path.endswith("/teams/name/ai-agents"):
            return httpx.Response(200, json={"id": "t1"})
        return httpx.Response(404, json={})

    assert stats.post_sync(client, data, http=httpx.Client(
        transport=httpx.MockTransport(missing))).startswith("no channel ~agents-stats")
    assert not (state / "mm-agent-bus" / f"stats-{UID}.jsonl").exists()  # does not pile up
    stats.record("turn")

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


def _write_events(*events):
    f = stats._path()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("".join(json.dumps(e) + "\n" for e in events))


def test_turns_run_from_the_prompt_to_the_stop_that_lets_go():
    """RT-6.9: a start with no end after it (interrupted) gets no duration; the
    bus's turns are told apart by the hook; forced rounds do not end a turn."""
    _write_events(
        {"k": "turn", "t": 1000, "by": "human"}, {"k": "block", "t": 1100, "kind": "drain"},
        {"k": "turn_end", "t": 1300},                       # one turn, 300 s, rounds inside
        {"k": "turn", "t": 2000, "by": "bus"}, {"k": "turn_end", "t": 2060},
        {"k": "turn", "t": 3000, "by": "human"},            # interrupted: no end
        {"k": "turn", "t": 4000, "by": "human"}, {"k": "turn_end", "t": 4010},
        {"k": "turn_end", "t": 5000},                       # an end without a start
    )
    s = stats.summary("0.36.0", 1)
    assert s["turns"] == 4 and s["turns_timed"] == 3
    assert (s["turn_sec"], s["turn_max_sec"]) == (370, 300)
    assert (s["bus_turns"], s["bus_turns_timed"], s["bus_turn_sec"], s["bus_turn_max_sec"]) == (1, 1, 60, 60)


def test_a_runtime_without_turn_hooks_sends_no_turn_data():
    """RT-6.9: absent, never zero, so a harness cannot look idle by lacking hooks."""
    stats.record("post", chars=3)
    s = stats.summary("0.36.0", 1)
    assert "turn_sec" not in s and "turns_timed" not in s
    assert s["asks"] == 0 and s["asks_open"] == 0 and s["ask_wait_sec"] == 0


def test_asks_are_counted_with_their_waits_and_what_stays_open():
    """RT-6.9: asks posted, answered (first answer only), wait sum and max, and
    the asks still open at the end with their ages."""
    now = int(time.time())
    _write_events(
        {"k": "ask", "t": now - 5000, "id": "a1"}, {"k": "ask", "t": now - 4000, "id": "a2"},
        {"k": "ask", "t": now - 900, "id": "a3"},
        {"k": "answer", "t": now - 4500, "id": "a1", "wait": 500},
        {"k": "answer", "t": now - 4400, "id": "a1", "wait": 600},   # a second one: not counted
        {"k": "answer", "t": now - 3000, "id": "a2", "wait": 1000},
    )
    s = stats.summary("0.36.0", 1)
    assert (s["asks"], s["asks_answered"], s["ask_wait_sec"], s["ask_wait_max_sec"]) == (3, 2, 1500, 1000)
    assert s["asks_open"] == 1 and s["ask_open_sec"] == s["ask_open_max_sec"]
    assert 895 <= s["ask_open_max_sec"] <= 905
    assert "a1" not in json.dumps(s)  # counts and seconds only


@pytest.mark.parametrize("client,label", [
    ("claude-code", "claude-code"), ("Claude-Code", "claude-code"), ("cursor-vscode", "cursor"),
    ("opencode", "opencode"), ("codex-mcp-client", "codex"), ("codex", "codex"),
    ("  Some Client  ", "some client"), ("x" * 50, "x" * 32),
])
def test_the_harness_is_named_after_the_mcp_client(client, label, monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert stats.harness(client) == label


def test_without_a_client_name_the_environment_decides(monkeypatch):
    monkeypatch.delenv("CLAUDECODE", raising=False)
    assert stats.harness("") == "other"
    monkeypatch.setenv("CLAUDECODE", "1")
    assert stats.harness("") == "claude-code"


@pytest.mark.asyncio
async def test_the_initialize_handshake_names_the_harness(monkeypatch):
    """RT-6.9: the middleware reads clientInfo.name; the summary says `rt`."""
    from types import SimpleNamespace
    monkeypatch.setattr(stats, "_client_name", "")
    monkeypatch.delenv("CLAUDECODE", raising=False)
    seen = []

    async def next_(ctx):
        seen.append(ctx.method)
        return {}

    await server._note_client(SimpleNamespace(method="tools/list", params={}), next_)
    await server._note_client(SimpleNamespace(
        method="initialize", params={"clientInfo": {"name": "cursor-vscode", "version": "1"}}), next_)
    assert seen == ["tools/list", "initialize"]
    stats.record("turn")
    assert stats.summary("0.36.0", 1)["rt"] == "cursor"
    assert server._note_client in server.mcp._lowlevel_server.middleware

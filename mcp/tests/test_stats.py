"""RT-6.9 overhead counters in the MCP server: counting, summary, the STATS post."""

import asyncio
import json
import time

import httpx
import pytest

from mattermost_ai_agent_bus import overhead, server, stats
from mattermost_ai_agent_bus.client import MattermostAdapter

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
    assert stats.channel() == "" and stats.summary("0.21.0") is None


def test_tool_results_and_posts_are_counted():
    text = server._out("list_recent", [{"id": "p1", "message": "hi"}])
    stats.record("post", chars=42)
    stats.record("wake", reason="digest")
    s = stats.summary("0.21.0", overhead.Fixed(2000, 3000, 22000))
    assert s["tools"] == {"list_recent": [1, len(text)]}
    assert s["posts"] == [1, 42] and s["wakes"] == {"digest": 1}
    assert "message" not in json.dumps(s)  # counts only, no content


def test_stats_report_reads_the_channel_with_the_bots_session(monkeypatch):
    """RT-6.9 step 3: a session reads the report without the host's token,
    through the bot it already has: the newest versions, STATS lines only."""
    from fakes import FakeChat

    def line(v, minutes):
        return {"id": f"p{v}", "channel_id": "c" * 26, "user_id": "x" * 26, "create_at": 1,
                "message": "STATS " + json.dumps({"v": v, "min": minutes, "turns": 3,
                                                  "static": 4800, "skill": 22078})}
    chat = FakeChat(channel="agents-stats", posts=[
        line("0.52.0", 60), line("0.53.0", 30), line("0.9.0", 60),
        {"id": "x", "user_id": "x" * 26, "message": "STATS {broken"},
        {"id": "y", "user_id": "x" * 26, "message": "hello"}])
    monkeypatch.setattr(server, "_get_client", lambda: chat)
    out = json.loads(asyncio.run(server.stats_report(versions=2)))
    assert out["channel"] == "agents-stats"
    assert [(r["version"], r["hours"], r["static"], r["skill"]) for r in out["versions"]] == [
        ("0.52.0", 1.0, 4800, 22078), ("0.53.0", 0.5, 4800, 22078)]
    monkeypatch.setenv("MM_STATS_CHANNEL", "elsewhere")
    assert "cannot read ~elsewhere" in asyncio.run(server.stats_report())
    monkeypatch.setenv("MM_STATS_CHANNEL", "off")
    assert "stats are off" in asyncio.run(server.stats_report())


def test_static_is_per_turn_text_and_the_skill_its_own_field():
    """RT-6.9 step 2: `static` is what every turn carries, the instructions
    and the tool docs; the skill is in the context only once loaded, so it
    is a field of its own (`skill`), and `tok.skill` says whether it was."""
    stats.record("turn")
    f = server._fixed()
    s = stats.summary("0.53.0", f)
    assert s["static"] == f.instructions + f.tools == len(server._instructions()) + f.tools
    assert s["skill"] == f.skill > 10_300 > s["static"]  # + get_file (RT-6.23)


_REAL_CLIENT = httpx.Client


def _mock_http(monkeypatch, handler):
    monkeypatch.setattr("mattermost_ai_agent_bus.client.httpx.Client",
                        lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw))


def test_post_goes_to_the_stats_channel(monkeypatch, state):
    client = MattermostAdapter(base_url="https://chat.example", token="bot", team_name="ai-agents")
    stats.record("turn")
    data = stats.summary("0.21.0")

    def missing(request):
        if request.url.path.endswith("/teams/name/ai-agents"):
            return httpx.Response(200, json={"id": "t1000000000000000000000000"})
        return httpx.Response(404, json={})

    _mock_http(monkeypatch, missing)
    assert stats.post_sync(client, data).startswith("no channel ~agents-stats")
    assert not (state / "mm-agent-bus" / f"stats-{UID}.jsonl").exists()  # does not pile up
    stats.record("turn")

    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/teams/name/ai-agents"):
            return httpx.Response(200, json={"id": "t1000000000000000000000000"})
        if request.url.path.endswith("/channels/name/agents-stats"):
            return httpx.Response(200, json={"id": "c9000000000000000000000000"})
        if request.url.path == "/api/v4/posts":
            body = json.loads(request.content)
            assert body["channel_id"] == "c9000000000000000000000000" and body["message"].startswith('STATS {"v":"0.21.0"')
        return httpx.Response(201, json={})

    monkeypatch.setenv("MM_STATS_CHANNEL", "~agents-stats")
    _mock_http(monkeypatch, handler)
    assert stats.post_sync(client, data) == "posted"
    assert ("POST", "/api/v4/channels/c9000000000000000000000000/members") in seen
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
    s = stats.summary("0.36.0")
    assert s["turns"] == 4 and s["turns_timed"] == 3
    assert (s["turn_sec"], s["turn_max_sec"]) == (370, 300)
    assert (s["bus_turns"], s["bus_turns_timed"], s["bus_turn_sec"], s["bus_turn_max_sec"]) == (1, 1, 60, 60)


def test_a_runtime_without_turn_hooks_sends_no_turn_data():
    """RT-6.9: absent, never zero, so a harness cannot look idle by lacking hooks."""
    stats.record("post", chars=3)
    s = stats.summary("0.36.0")
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
    s = stats.summary("0.36.0")
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
    assert stats.summary("0.36.0")["rt"] == "cursor"
    assert server._note_client in server.mcp._lowlevel_server.middleware

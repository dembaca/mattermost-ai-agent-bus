"""RT-6.9: the bus's token overhead, measured from a Claude Code transcript."""

import json

import pytest

from mattermost_ai_agent_bus import overhead, stats

BUS = "mcp__plugin_mattermost-ai-agent-bus_mattermost-agent-bus__"
FIXED = overhead.Fixed(instructions=400, tools=4000, skill=20000)
T0 = "2026-10-24T10:00:00.000Z"


class Transcript:
    def __init__(self):
        self.rows = []
        self.n = 0

    def user(self, text, origin=None, meta=False):
        row = {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text},
               "timestamp": T0}
        if origin:
            row["origin"] = {"kind": origin}
        if meta:
            row["isMeta"] = True
        self.rows.append(row)

    def results(self, *pairs):
        self.rows.append({"type": "user", "isSidechain": False, "timestamp": T0, "message": {
            "role": "user", "content": [{"type": "tool_result", "tool_use_id": i, "content": body}
                                        for i, body in pairs]}})

    def request(self, *tools, read=100_000, out=200, ts=T0, w1h=0):
        """One model request: its usage on each content entry, as Claude Code writes it."""
        self.n += 1
        rid = f"req_{self.n}"
        usage = {"input_tokens": 10, "cache_read_input_tokens": read,
                 "cache_creation_input_tokens": 1000 + w1h, "output_tokens": out,
                 "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": w1h}}
        blocks = [{"type": "text", "text": "ok"}] + [
            {"type": "tool_use", "id": tid, "name": name, "input": inp} for tid, name, inp in tools]
        for b in blocks:
            self.rows.append({"type": "assistant", "isSidechain": False, "requestId": rid,
                              "timestamp": ts, "message": {"id": rid, "usage": usage, "content": [b]}})

    def other(self, row):
        self.rows.append(row)

    def write(self, path):
        path.write_text("\n".join(json.dumps(r) for r in self.rows) + "\n")
        return path


def measure(t, tmp_path, since=0):
    return overhead.measure(t.write(tmp_path / "t.jsonl"), since, FIXED)


def test_work_carries_only_the_fixed_share(tmp_path):
    """RT-6.9 step 2: a human's turn without the bus: the fixed text's share of
    the input, nothing of the output."""
    t = Transcript()
    t.user("fix the bug", origin="human")
    t.request(("b1", "Bash", {"command": "pytest"}))
    t.results(("b1", "ok"))
    t.request()
    r = measure(t, tmp_path)
    assert r["req"] == 2 and r["bus_req"] == 0
    share = (400 + 4000) / 4 / 101_010
    assert r["bus"]["read"] == round(2 * 100_000 * share) and r["bus"]["out"] == 0
    assert r["fixed"] == 1100
    assert r["all"] == {"in": 20, "read": 200_000, "w5m": 2000, "w1h": 0, "out": 400}


def test_requests_the_bus_caused_count_whole(tmp_path):
    """RT-6.9 step 2: an agent's message, the Stop hook, the listener and a round
    after bus tool results only: their whole usage is overhead."""
    t = Transcript()
    t.user('<channel source="plugin:mattermost-ai-agent-bus:mattermost-agent-bus" kind="mention" '
           'from_trusted_human="false">@me ACK?</channel>', origin="channel", meta=True)
    t.request(("m1", BUS + "reply_in_thread", {"root_id": "r", "message": "ACK"}))
    t.results(("m1", '{"id":"p"}'))
    t.request()                                   # after a bus result only
    t.user("Stop hook feedback:\nBefore this turn ends: … Reply in your HELLO thread", meta=True)
    t.request(("l1", "Bash", {"command": "bin/mm-agent-listen.sh --until 1", "run_in_background": True}))
    t.results(("l1", "Command running in background with ID: x"))
    t.request()
    t.user("<task-notification>\n<tool-use-id>l1</tool-use-id>\n<status>completed</status>",
           origin="task-notification")
    t.request(read=50_000)
    r = measure(t, tmp_path)
    assert r["req"] == r["bus_req"] == 5
    assert r["bus"] == r["all"]


def test_a_trusted_humans_message_is_work(tmp_path):
    """RT-6.9: a prompt over the bus stands for one typed in the terminal."""
    t = Transcript()
    t.user('<channel source="plugin:mattermost-ai-agent-bus:mattermost-agent-bus" kind="dm" '
           'from_trusted_human="true">please fix the bug</channel>', origin="channel", meta=True)
    t.request()
    assert measure(t, tmp_path)["bus_req"] == 0


def test_bus_data_stays_in_the_context_until_compaction(tmp_path):
    """RT-6.9 step 2: tool results, loaded schemas and the skill raise the share of
    every later request; a compaction drops what the bus put in."""
    t = Transcript()
    t.other({"type": "attachment", "attachment": {"type": "deferred_tools_delta",
                                                   "addedNames": [BUS + "get_thread"]}})
    t.user("look at the thread", origin="human")
    t.request(("s1", "ToolSearch", {"query": "select:get_thread"}),
              ("k1", "Skill", {"skill": "mattermost-ai-agent-bus:mattermost-ai-agent-bus"}))
    schema = '<function>{"name": "' + BUS + 'get_thread"}</function>'
    t.results(("s1", f"<functions>{schema}<function>other</function></functions>"), ("k1", "loaded"))
    t.request(("g1", BUS + "get_thread", {"post_id": "x" * 26}), ("b1", "Bash", {"command": "ls"}))
    t.results(("g1", "y" * 40_000), ("b1", "z" * 40_000))
    t.request(out=10_000)
    t.other({"type": "system", "subtype": "compact_boundary"})
    t.user("go on", origin="human")
    t.request()
    r = measure(t, tmp_path)
    tool_call = len(json.dumps({"post_id": "x" * 26}))
    assert r["bus_req"] == 0 and r["fixed"] == (400 + 20000) // 4
    shares = [400 / 4 / 101_010,                                         # deferred, no skill yet
              (20400 + len(schema)) / 4 / 101_010,                      # skill and schema loaded
              (20400 + len(schema) + tool_call + 40_000) / 4 / 101_010,  # + the bus's result
              20400 / 4 / 101_010]                                      # compacted
    assert r["bus"]["read"] == round(sum(100_000 * s for s in shares))
    assert r["bus"]["out"] == round(tool_call / 4)


def test_only_requests_of_this_session_count(tmp_path):
    t = Transcript()
    t.user("earlier", origin="human")
    t.request(ts="2026-10-23T10:00:00.000Z")
    t.user("now", origin="human")
    t.request()
    since = overhead._ts({"timestamp": "2026-10-24T09:00:00.000Z"})
    assert measure(t, tmp_path, since)["req"] == 1
    assert overhead.measure(tmp_path / "missing.jsonl", 0, FIXED) is None


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", "u" * 26)
    monkeypatch.delenv("MM_STATS_CHANNEL", raising=False)
    return tmp_path


def test_the_summary_carries_the_tokens(state, tmp_path):
    """RT-6.9 step 2: the hook names the transcript, the summary measures it."""
    t = Transcript()
    t.user("hi", origin="human")
    t.request(ts="2030-01-01T00:00:00.000Z")
    path = t.write(tmp_path / "t.jsonl")
    stats.record("turn")
    stats.record("transcript", path=str(path))
    s = stats.summary("0.32.0", 1, FIXED)
    assert s["tok"]["req"] == 1 and s["tok"]["all"]["read"] == 100_000
    stats.record("transcript", path=str(tmp_path / "gone.jsonl"))
    assert "tok" not in stats.summary("0.32.0", 1, FIXED)

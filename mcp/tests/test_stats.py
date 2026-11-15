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
    monkeypatch.setattr(stats, "_unposted", [])
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
    # + get_file (RT-6.23), channel names (RT-6.22); + ask_human's kind, advice, ask_mode (RT-6.25)
    # + coordinator's scope (RT-6.14 step 9); + mandated on events, the instructions (RT-6.26)
    # + ask_human's alternatives and `choice` on reactions (RT-6.11)
    # + answers as a list of texts and `chosen` on reactions (RT-6.11, 0.71.15)
    # + the hint on wait_for_events and the instructions' line on asks (RT-6.11, 0.71.30), only_human (RT-6.25, 0.71.32), props (RT-6.16, 0.71.34), no `question` argument (RT-6.11, 0.73.0), the prefix in the instructions (RT-6.16, 0.74.0), which fields the floor reads (RT-6.25 step 1, 0.86.1), for_crown on events and in the instructions (RT-6.6 step 5, 0.88.0), answer in the thread in the instructions (RT-6.2 step 2, 0.89.0), the attach tool, `files` on post_message and reply_in_thread and their line in the instructions (RT-6.33, 0.90.0)
    assert s["skill"] == f.skill > 11_000 and s["static"] < 12_480


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
        {"k": "ask", "t": now - 5000, "id": "q1"}, {"k": "ask", "t": now - 4000, "id": "q2"},
        {"k": "ask", "t": now - 900, "id": "q3"},
        {"k": "answer", "t": now - 4500, "id": "q1", "wait": 500},
        {"k": "answer", "t": now - 4400, "id": "q1", "wait": 600},   # a second one: not counted
        {"k": "answer", "t": now - 3000, "id": "q2", "wait": 1000},
    )
    s = stats.summary("0.36.0")
    assert (s["asks"], s["asks_answered"], s["ask_wait_sec"], s["ask_wait_max_sec"]) == (3, 2, 1500, 1000)
    assert s["asks_open"] == 1 and s["ask_open_sec"] == s["ask_open_max_sec"]
    assert 895 <= s["ask_open_max_sec"] <= 905
    assert "q1" not in json.dumps(s)  # counts and seconds only


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


# --- RT-6.9 step 2: a day's parts; step 1: saved waiting, the crown ----------

def _events():
    return [json.loads(line) for line in stats._path().read_text().splitlines()]


def _append(*events):
    with stats._path().open("a") as fh:
        fh.write("".join(json.dumps(e) + "\n" for e in events))


def test_a_day_part_carries_what_is_still_open():
    """RT-6.9 step 2: a part counts what ended in it; a turn still running and
    an ask still open go into the next part with its record and count there,
    once, with their full durations."""
    now = int(time.time())
    day = now - stats.PART_SEC - 100
    _write_events(
        {"k": "turn", "t": day, "by": "human"}, {"k": "turn_end", "t": day + 60},
        {"k": "ask", "t": day + 100, "id": "q1"},
        {"k": "turn", "t": day + 200, "by": "human"}, {"k": "turn_end", "t": day + 260},
        {"k": "turn", "t": now - 50, "by": "human"},           # still running at the cut
        {"k": "transcript", "t": day, "path": "/nowhere/t.jsonl"},
    )
    assert stats.due(now) < 0
    one = stats.cut("0.91.0", now=now)
    assert (one["part"], "final" in one, len(one["sid"])) == (1, False, 12)
    int(one["sid"], 16)                                         # random hex, no id of the bus
    assert (one["turns"], one["turns_timed"], one["turn_sec"]) == (2, 2, 120)
    assert (one["asks"], one["asks_open"], one["min"]) == (0, 0, round((stats.PART_SEC + 100) / 60))
    kept = _events()
    assert kept[0] == {"k": "part", "t": now, "sid": one["sid"], "n": 2, "hooks": 1}
    assert {"k": "ask", "t": day + 100, "id": "q1", "busy": 1} in kept   # a human turn came after it
    assert {"k": "turn", "t": now - 50, "by": "human"} in kept
    assert {"k": "transcript", "t": day, "path": "/nowhere/t.jsonl"} in kept
    assert stats.due(now) == stats.PART_SEC
    stats.record("turn_end")
    stats.record("answer", id="q1", wait=stats.PART_SEC, at=now + 5)
    two = stats.summary("0.91.0")
    assert (two["sid"], two["part"], two["final"]) == (one["sid"], 2, True)
    assert (two["turns"], two["turns_timed"]) == (1, 1) and 50 <= two["turn_sec"] <= 60
    assert (two["asks"], two["asks_answered"], two["ask_wait_sec"]) == (1, 1, stats.PART_SEC)
    assert two["asks_idle"] == 0                                # the human was back before the cut


def test_saved_waiting_counts_answers_while_the_terminal_was_idle():
    """RT-6.9 step 1: an ask answered on the bus with no human prompt since the
    ask saved the time until the human's next prompt; an answer no prompt
    followed by the end is open, a lower bound; a prompt between ask and
    answer means the human was at the terminal: nothing saved."""
    now = int(time.time())
    _write_events(
        {"k": "turn", "t": now - 9000, "by": "human"},
        {"k": "ask", "t": now - 8900, "id": "q1"},
        {"k": "turn", "t": now - 8000, "by": "bus"},            # the answer's push: not the human
        {"k": "answer", "t": now - 7990, "id": "q1", "wait": 900, "at": now - 8000},
        {"k": "turn", "t": now - 7400, "by": "human"},          # back after 600 s
        {"k": "ask", "t": now - 7000, "id": "q2"},
        {"k": "turn", "t": now - 6500, "by": "human"},          # at the terminal meanwhile
        {"k": "answer", "t": now - 6000, "id": "q2", "wait": 1000, "at": now - 6000},
        {"k": "ask", "t": now - 5000, "id": "q3"},
        {"k": "answer", "t": now - 1000, "id": "q3", "wait": 4000},   # no `at`: when counted
    )
    s = stats.summary("0.91.0")
    assert (s["asks_answered"], s["asks_idle"]) == (3, 2)
    assert (s["saved_n"], s["saved_sec"], s["saved_max_sec"]) == (1, 600, 600)
    assert s["saved_open_n"] == 1 and 995 <= s["saved_open_sec"] <= 1005
    assert "q1" not in json.dumps(s)


def test_an_answer_waiting_for_the_human_counts_in_the_part_they_come_back():
    """RT-6.9 step 2: an idle answer no human prompt followed by the cut is
    carried, not counted open, and saves its time in the part the human is back."""
    now = int(time.time())
    day = now - stats.PART_SEC - 100
    _write_events({"k": "turn", "t": day, "by": "human"}, {"k": "turn_end", "t": day + 10},
                  {"k": "ask", "t": day + 20, "id": "q1"},
                  {"k": "answer", "t": now - 300, "id": "q1", "wait": 1000, "at": now - 300})
    one = stats.cut("0.91.0", now=now)
    assert (one["asks_answered"], one["asks_idle"], one["saved_n"], one["saved_open_n"]) == (1, 1, 0, 0)
    assert {"k": "saving", "t": now - 300, "at": now - 300} in _events()
    _append({"k": "turn", "t": now + 900, "by": "human"})
    two = stats.summary("0.91.0")
    assert (two["asks_answered"], two["asks_idle"], two["saved_n"], two["saved_sec"]) == (0, 0, 1, 1200)
    assert two["saved_open_n"] == 0


def test_a_runtime_without_turn_hooks_sends_no_saved_waiting():
    """RT-6.9: without the human's turns idle cannot be told: absent, not zero."""
    now = int(time.time())
    _write_events({"k": "ask", "t": now - 100, "id": "q1"},
                  {"k": "answer", "t": now - 50, "id": "q1", "wait": 50, "at": now - 50})
    s = stats.summary("0.91.0")
    assert s["asks_answered"] == 1 and "asks_idle" not in s and "saved_n" not in s


def test_crown_seconds_count_until_the_crown_ends_or_its_lease_ran_out(monkeypatch):
    """RT-6.9 step 1: crown_sec from the heartbeat's notes; a crown still on
    counts until now, or until its lease ran out while no server ran; at a cut
    a crown still held goes on in the next part."""
    now = int(time.time())
    _write_events({"k": "post", "t": now - 5000, "chars": 1},
                  {"k": "crown", "t": now - 4000, "on": 1}, {"k": "crown", "t": now - 3000, "on": 0},
                  {"k": "crown", "t": now - 2000, "on": 1})
    monkeypatch.setattr(stats, "_crown_until", lambda: now - 1500)
    assert stats.summary("0.91.0")["crown_sec"] == 1500
    monkeypatch.setattr(stats, "_crown_until", lambda: now + 60)
    one = stats.cut("0.91.0", now=now)
    assert one["crown_sec"] == 3000
    assert {"k": "crown", "t": now, "on": 1} in _events()
    assert stats.summary("0.91.0")["crown_sec"] <= 2


def test_the_heartbeat_notes_each_change_of_the_crown(monkeypatch):
    """RT-6.9, RT-6.6: on and off, the off at the lease's end when that lies
    behind; a heartbeat that starts without the crown ends one the counters
    still show on (it lapsed while no server ran)."""
    from mattermost_ai_agent_bus import lease
    now = int(time.time())
    monkeypatch.setattr(lease, "crown_until", lambda uid: now + 60)
    assert lease._count_crown(UID, True, None) is True
    assert lease._count_crown(UID, True, True) is True          # no change: nothing noted
    monkeypatch.setattr(lease, "crown_until", lambda uid: now - 30)
    assert lease._count_crown(UID, False, True) is False
    crowns = [e for e in _events() if e["k"] == "crown"]
    assert [e["on"] for e in crowns] == [1, 0] and crowns[1]["t"] == now - 30
    _write_events({"k": "crown", "t": now - 100, "on": 1})
    assert stats.crown_open()
    lease._count_crown(UID, False, None)
    assert not stats.crown_open()


async def test_a_day_old_part_is_posted_by_the_heartbeat(monkeypatch):
    """RT-6.9 step 2: stall.watch's stats step joins the channel first (a
    server that cannot reach it cuts nothing), posts the day's part once."""
    from mattermost_ai_agent_bus import stall
    now = int(time.time())
    _write_events({"k": "turn", "t": now - stats.PART_SEC - 5, "by": "human"},
                  {"k": "turn_end", "t": now - stats.PART_SEC})
    posted = []

    class Client:
        token = "bot"
        session = None  # the bot's id from MM_BOT_USER_ID

        async def join_channel(self, name):
            posted.append(name)
            return "c" * 26

        async def post(self, message, *, channel_id=None, **kw):
            if message.startswith("STATS "):
                posted.append((channel_id, message))

    class Down(Client):
        async def join_channel(self, name):
            raise OSError("offline")

    monkeypatch.setattr(stats, "_source", lambda: ("0.91.0", None))
    await stall.watch(Down())
    assert stats.due() < 0                                      # nothing cut
    await stall.watch(Client())                                 # the heartbeat's watch posts it
    stat_posts = [p for p in posted if isinstance(p, tuple)]
    assert len(stat_posts) == 1 and stat_posts[0][0] == "c" * 26
    line = json.loads(stat_posts[0][1].removeprefix("STATS "))
    assert (line["v"], line["part"], line["turns"], line["turn_sec"]) == ("0.91.0", 1, 1, 5)
    assert 0 < await stats.tick(Client(), UID) <= stats.PART_SEC
    assert len([p for p in posted if isinstance(p, tuple)]) == 1


async def test_a_part_whose_post_failed_is_posted_before_the_next(monkeypatch):
    """RT-6.9 step 2: a post that fails after the cut keeps its line; the next
    tick posts it before anything else, and the session's end before its own."""
    now = int(time.time())
    _write_events({"k": "turn", "t": now - stats.PART_SEC - 5, "by": "human"})
    posted = []

    class Flaky:
        token = "bot"
        fail = True

        async def join_channel(self, name):
            return "c" * 26

        async def post(self, message, *, channel_id=None, **kw):
            if self.fail:
                raise OSError("blip")
            posted.append(message)

    monkeypatch.setattr(stats, "_source", lambda: ("0.91.0", None))
    client = Flaky()
    assert await stats.tick(client, UID) is None
    assert len(stats._unposted) == 1 and stats.due() > 0       # cut, kept, not cut again
    client.fail = False
    assert await stats.tick(client, UID) > 0
    assert [json.loads(m.removeprefix("STATS "))["part"] for m in posted] == [1]
    assert stats._unposted == []
    stats._unposted.append("STATS {\"part\":7}")
    sent = []

    class Sync:
        token = "bot"

        def post_by_name_sync(self, name, message, *, join=""):
            sent.append(message)
            return True

    stats.record("turn")
    assert stats.post_sync(Sync(), stats.summary("0.91.0")) == "posted"
    assert sent[0] == "STATS {\"part\":7}" and json.loads(sent[1].removeprefix("STATS "))["final"]
    assert not stats._path().exists() and not (stats._path().parent / f"stats-{UID}.lock").exists()


def test_a_cut_that_died_is_recovered_and_late_lines_go_on():
    """RT-6.9 step 2: counters a crashed cut left beside the file go back into
    it (nothing of that cut was carried, so nothing counts twice); a line a
    hook appended to the moved file after it was read goes on with the next part."""
    now = int(time.time())
    f = stats._path()
    _write_events({"k": "turn", "t": now - stats.PART_SEC - 50, "by": "human"},
                  {"k": "turn_end", "t": now - stats.PART_SEC - 40},
                  {"k": "turn", "t": now - 10, "by": "human"})        # running
    f.rename(f.with_name(f.name + ".cut"))                          # a cut that died here
    _write_events({"k": "post", "t": now - 5, "chars": 7})            # a hook since
    s = stats.summary("0.91.0")
    assert (s["turns"], s["posts"]) == (2, [1, 7]) and not f.with_name(f.name + ".cut").exists()

    real = stats._summarize

    def with_a_late_line(*a, **kw):
        with f.with_name(f.name + ".cut").open("a") as fh:          # a hook still writing to it
            fh.write(json.dumps({"k": "turn_end", "t": now - 1}) + "\n")
        return real(*a, **kw)

    stats._summarize = with_a_late_line
    try:
        one = stats.cut("0.91.0", now=now)
    finally:
        stats._summarize = real
    assert (one["turns"], one["turns_timed"], one["posts"]) == (1, 1, [1, 7])   # recovered: counted
    kept = _events()
    assert [e["k"] for e in kept].count("turn") == 1                 # carried once
    assert {"k": "turn_end", "t": now - 1} in kept                   # the late line goes on
    two = stats.summary("0.91.0")
    assert (two["turns"], two["turns_timed"], two["turn_sec"], two["posts"]) == (1, 1, 9, [0, 0])


def test_the_report_counts_a_session_once_and_splits_by_role():
    """RT-6.9 step 3: a session's parts (one sid) are one session; lines
    without a sid (before 0.91.0) one each; a part holding the crown more
    than half its time is the coordinator's, so one session can be both."""
    saved = {"saved_n": 0, "saved_sec": 0, "saved_max_sec": 0, "saved_open_n": 0, "saved_open_sec": 0}
    lines = [
        {"v": "0.91.0", "sid": "5e55", "part": 1, "rt": "claude-code", "min": 1440, "turns": 10,
         "crown_sec": 80000, "posts": [2, 100], "asks": 2, "asks_answered": 2, "asks_idle": 1,
         **saved, "saved_n": 1, "saved_sec": 600, "saved_max_sec": 600},
        {"v": "0.91.0", "sid": "5e55", "part": 2, "final": True, "rt": "claude-code", "min": 60, "turns": 2,
         "crown_sec": 0, "posts": [1, 50], "asks": 1, "asks_answered": 1, "asks_idle": 1,
         **saved, "saved_open_n": 1, "saved_open_sec": 300},
        {"v": "0.91.0", "sid": "a1b2", "part": 1, "final": True, "rt": "claude-code", "min": 120,
         "turns": 4, "crown_sec": 600, "posts": [0, 0]},
        {"v": "0.91.0", "rt": "cursor", "min": 30, "turns": 0},
    ]
    r = stats.report(lines)[0]
    assert (r["sessions"], r["parts"], r["post_chars_per_session"]) == (3, 4, 50)
    assert [(x["role"], x["parts"], x["sessions"], x["hours"]) for x in r["roles"]] == [
        ("coordinator", 1, 1, 24.0), ("unknown", 1, 1, 0.5), ("worker", 2, 2, 3.0)]
    s = r["saved"]
    assert (s["sessions"], s["answered"], s["idle"], s["idle_pct"]) == (1, 3, 2, 67)
    assert (s["saved_n"], s["mean_saved_sec"], s["saved_open_n"], s["saved_open_sec"]) == (1, 600, 1, 300)

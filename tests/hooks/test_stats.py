"""RT-6.9 coordination overhead counters (docs/arc42/06-runtime-view.md)."""

from __future__ import annotations

import json
import time

from conftest import ME, PROJECT

STOP = "hooks/stop-inbox-check.sh"
PROMPT = "hooks/turn-status.sh"
LISTEN = "bin/mm-agent-listen.sh"
REPORT = "bin/mm-agent-stats.sh"
IN_PROJECT = {"MM_PROJECT_CHANNEL_ID": PROJECT}


def events(run) -> list[dict]:
    f = run.state_dir / f"stats-{ME}.jsonl"
    return [json.loads(l) for l in f.read_text().splitlines()] if f.exists() else []


def test_hooks_and_listener_count_what_the_plugin_causes(mm, run):
    """RT-6.9 step 1: turns, forced rounds and wake-ups land in the counter file."""
    run(PROMPT)
    mm.unread_dm(1)
    run(STOP)                                           # drain round
    run(LISTEN, "--until", str(int(time.time()) + 5), MM_LISTEN_POLL_SEC="1", **IN_PROJECT)
    kinds = [(e["k"], e.get("kind") or e.get("reason")) for e in events(run)]
    assert ("turn", None) in kinds
    assert ("block", "drain") in kinds
    assert ("wake", "dm") in kinds
    assert all(isinstance(e["t"], int) for e in events(run))


def test_a_turn_ends_at_the_stop_that_lets_go(mm, run):
    """RT-6.9 step 1: the start is noted by the prompt hook, the end by the Stop
    hook that does not block; forced rounds belong to the same turn."""
    run(PROMPT)
    mm.unread_dm(1)
    assert all(run(STOP).blocked for _ in range(3))     # three drain rounds: no end
    assert [e["k"] for e in events(run)] == ["turn", "block", "block", "block"]
    assert run(STOP).passed                              # the count is stale: let go
    kinds = [e["k"] for e in events(run)]
    assert kinds.count("turn") == 1 and kinds[-1] == "turn_end"
    assert kinds.count("turn_end") == 1
    assert events(run)[0]["by"] == "human"


def test_the_bus_starts_a_turn_by_a_listener_that_ended_on_a_message(mm, run):
    """RT-6.9 step 1: a turn the listener's wake-up started is the bus's; a
    prompt with the listener gone, or still waiting, is the human's."""
    f = run.state_dir / f"listen-{ME}.env"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("PID=0\nENDED=event\n")
    run(PROMPT)
    f.write_text("PID=0\nENDED=over\n")               # fresh: the window's end woke it
    run(PROMPT)
    f.write_text("PID=0\nENDED=\n")                   # nothing woke it: the human typed
    run(PROMPT)
    run(PROMPT)                                         # no window at all
    assert [e["by"] for e in events(run) if e["k"] == "turn"] == ["bus", "bus", "human", "human"]


def test_report_aggregates_per_version(mm, run):
    """RT-6.9 step 3: the report groups the STATS lines by version; the share
    counts the plugin's turns against all turns, its own included (never > 100 %).
    `turns` already holds the turns a listener's end started: they count once,
    and from 0.36.0 bus_turns (pushes too) replaces the wake-ups."""
    def stats(v, minutes, turns, wakes, blocks, chars, **more):
        return {"id": f"s-{v}", "user_id": "x" * 26, "create_at": 1, "message":
                "STATS " + json.dumps({"v": v, "min": minutes, "turns": turns,
                                        "wakes": {"addressed": wakes}, "blocks": {"listen": blocks},
                                        "tools": {"wait_for_events": [wakes, chars]},
                                        "posts": [1, 120], "compact": [0, 0], "static": 29500, **more})}
    mm.posts[:0] = [stats("0.20.0", 60, 20, 4, 2, 4000), stats("0.21.0", 120, 40, 2, 2, 1000),
                    stats("0.36.0", 60, 10, 2, 1, 1100, bus_turns=3),
                    {"id": "noise", "user_id": "x" * 26, "create_at": 1, "message": "hello"}]
    r = run(REPORT, "report", "agents-stats", "--json")
    assert r.code == 0, r.stderr
    rows = {row["version"]: row for row in json.loads(r.stdout)}
    # 4 wake-ups + 2 forced rounds of 20 prompts + 2 forced rounds
    assert rows["0.20.0"]["plugin_turns_per_h"] == 6.0 and rows["0.20.0"]["plugin_share_pct"] == 27
    assert rows["0.21.0"]["plugin_turns_per_h"] == 2.0 and rows["0.21.0"]["bus_chars_per_turn"] == 24
    # 3 bus-started prompts (not the 2 wake-ups) + 1 forced round of 10 + 1
    assert rows["0.36.0"]["plugin_turns_per_h"] == 4.0 and rows["0.36.0"]["plugin_share_pct"] == 36
    assert rows["0.21.0"]["static"] == 29500
    assert "0.21.0" in run(REPORT, "report", "agents-stats").stdout



def test_the_prompt_hook_names_the_transcript(mm, run):
    """RT-6.9 step 1: where Claude Code books the tokens, for the summary."""
    run(PROMPT, stdin=json.dumps({"prompt": "hi", "transcript_path": "/x/s.jsonl"}))
    assert {"k": "transcript", "path": "/x/s.jsonl"}.items() <= events(run)[-1].items()


def test_report_shows_tokens_per_version(mm, run):
    """RT-6.9 step 3: sessions with token counts get a second table, in
    input-token equivalents."""
    tok = {"req": 100, "bus_req": 10, "fixed": 1500,
           "all": {"in": 0, "read": 1_000_000, "w5m": 0, "w1h": 0, "out": 20_000},
           "bus": {"in": 0, "read": 100_000, "w5m": 0, "w1h": 0, "out": 2_000}}
    mm.posts[:0] = [{"id": "s-tok", "user_id": "x" * 26, "create_at": 1, "message": "STATS " + json.dumps(
        {"v": "0.32.0", "min": 120, "turns": 5, "static": 28000, "tok": tok})}]
    rows = {r["version"]: r for r in json.loads(run(REPORT, "report", "agents-stats", "--json").stdout)}
    t = rows["0.32.0"]["tok"]
    assert (t["all"], t["bus"], t["bus_share_pct"]) == (200_000, 20_000, 10.0)
    assert (t["req_per_h"], t["bus_req_per_h"], t["bus_per_h"], t["fixed"]) == (50, 5.0, 10_000, 1500)
    out = run(REPORT, "report", "agents-stats").stdout
    assert "bus tok/h" in out and "10%" in out


def test_report_groups_turns_and_asks_by_harness(mm, run):
    """RT-6.9 step 3: turns and asks get a table each, one row per version and
    harness. A harness without turn hooks (cursor) has no turn data: a dash,
    never a zero; sessions before 0.36.0 carry neither and weigh nothing."""
    n = iter(range(100))

    def line(v, rt, minutes=60, **fields):
        return {"id": f"s{next(n)}", "user_id": "x" * 26, "create_at": 1, "message":
                "STATS " + json.dumps({"v": v, "rt": rt, "min": minutes, "static": 28000, **fields})}
    quiet = dict(asks=0, asks_answered=0, ask_wait_sec=0, ask_wait_max_sec=0,
                 asks_open=0, ask_open_sec=0, ask_open_max_sec=0)
    mm.posts[:0] = [
        line("0.35.0", "claude-code", turns=10),
        line("0.36.0", "claude-code", turns=10, turns_timed=8, turn_sec=1200, turn_max_sec=600,
             bus_turns=4, bus_turns_timed=3, bus_turn_sec=90, bus_turn_max_sec=60,
             asks=3, asks_answered=2, ask_wait_sec=600, ask_wait_max_sec=500,
             asks_open=1, ask_open_sec=5400, ask_open_max_sec=5400),
        line("0.36.0", "claude-code", turns=2, turns_timed=2, turn_sec=30, turn_max_sec=20,
             bus_turns=0, bus_turns_timed=0, bus_turn_sec=0, bus_turn_max_sec=0, **quiet),
        line("0.36.0", "cursor", minutes=120, turns=0, asks=1, asks_answered=1, ask_wait_sec=300,
             ask_wait_max_sec=300, asks_open=0, ask_open_sec=0, ask_open_max_sec=0),
    ]
    rows = {r["version"]: {h["harness"]: h for h in r["harness"]}
            for r in json.loads(run(REPORT, "report", "agents-stats", "--json").stdout)}
    assert rows["0.35.0"]["claude-code"]["turns"] is None and rows["0.35.0"]["claude-code"]["asks"] is None
    cc, cur = rows["0.36.0"]["claude-code"], rows["0.36.0"]["cursor"]
    t, a = cc["turns"], cc["asks"]
    assert (t["sessions"], t["turns_per_prompt"], t["mean_sec"], t["max_sec"]) == (2, 1.5, 123, 600)
    assert (t["bus_mean_sec"], t["bus_max_sec"], t["bus_share_pct"], t["sec_per_input"]) == (30, 60, 7, 123)
    assert (a["sessions"], a["asks_per_h"], a["answered_pct"], a["mean_wait_sec"]) == (2, 1.5, 67, 300)
    assert (a["max_wait_sec"], a["open"], a["open_max_sec"], a["waiting_pct"]) == (500, 1, 5400, 83)
    assert cur["turns"] is None                                  # no turn hooks: absent, not zero
    assert (cur["asks"]["asks_per_h"], cur["asks"]["waiting_pct"]) == (0.5, 4)
    out = run(REPORT, "report", "agents-stats").stdout
    cells = [l.split() for l in out.splitlines()]
    assert ["0.36.0", "claude-code", "2", "1.5", "2", "min", "10", "min", "7%", "2", "min"] in cells
    assert ["0.36.0", "cursor", "-", "-", "-", "-", "-", "-"] in cells
    assert ["0.36.0", "cursor", "1", "0.5", "100%", "5", "min", "5", "min", "0", "4%"] in cells
    assert ["0.36.0", "claude-code", "2", "1.5", "67%", "5", "min", "8", "min", "1", "83%"] in cells


def test_the_tool_reports_what_the_script_reports(mm, run):
    """RT-6.9 step 3: stats_report (stats.report) gives the same JSON as
    `mm-agent-stats.sh report --json`, for old lines (static with the skill,
    no rt, wake-ups instead of bus_turns) and new ones (skill apart, tok.skill)."""
    from mattermost_ai_agent_bus import stats

    tok = {"req": 90, "bus_req": 7, "fixed": 1201, "skill": 1,
           "all": {"in": 3, "read": 900_001, "w5m": 17, "w1h": 5, "out": 20_003},
           "bus": {"in": 1, "read": 90_001, "w5m": 3, "w1h": 0, "out": 2_001}}
    lines = [
        {"v": "0.20.0", "min": 61, "turns": 20, "wakes": {"addressed": 4}, "blocks": {"listen": 3},
         "tools": {"wait_for_events": [4, 4001]}, "posts": [1, 121], "compact": [1, 333], "static": 29500},
        {"v": "0.36.0", "rt": "claude-code", "min": 37, "turns": 7, "bus_turns": 3, "static": 28000,
         "turns_timed": 5, "turn_sec": 1001, "turn_max_sec": 600, "bus_turns_timed": 2,
         "bus_turn_sec": 95, "bus_turn_max_sec": 60, "asks": 3, "asks_answered": 2,
         "ask_wait_sec": 605, "ask_wait_max_sec": 500, "asks_open": 1, "ask_open_sec": 5401,
         "ask_open_max_sec": 5401, "tok": {k: v for k, v in tok.items() if k != "skill"}},
        {"v": "0.36.0", "rt": "cursor", "min": 125, "turns": 0, "asks": 1, "asks_answered": 1,
         "ask_wait_sec": 301, "ask_wait_max_sec": 301, "asks_open": 0, "ask_open_sec": 0,
         "ask_open_max_sec": 0},
        {"v": "0.53.0", "rt": "claude-code", "min": 45, "turns": 9, "bus_turns": 1, "static": 4800,
         "skill": 22078, "blocks": {"drain": 2}, "tok": tok},
        {"v": "0.53.0", "rt": "claude-code", "min": 15, "turns": 2, "bus_turns": 0, "static": 4800,
         "skill": 22078, "tok": tok | {"skill": 0}},
    ]
    mm.posts[:0] = [{"id": f"s{i}", "user_id": "x" * 26, "create_at": 1,
                     "message": "STATS " + json.dumps(line)} for i, line in enumerate(lines)]
    r = run(REPORT, "report", "agents-stats", "--json")
    assert r.code == 0, r.stderr
    assert json.loads(r.stdout) == stats.report(lines)
    new = stats.report(lines)[-1]
    assert (new["static"], new["skill"], new["tok"]["skill_loaded"]) == (4800, 22078, 1)
    assert "1/2" in run(REPORT, "report", "agents-stats").stdout       # skill loaded in 1 of 2


def test_off_counts_nothing(mm, run):
    run(PROMPT, MM_STATS_CHANNEL="off")
    assert events(run) == []

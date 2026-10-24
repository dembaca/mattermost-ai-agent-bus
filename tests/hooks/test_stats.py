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


def test_report_aggregates_per_version(mm, run):
    """RT-6.9 step 3: the report groups the STATS lines by version; the share
    counts the plugin's turns against all turns, its own included (never > 100 %)."""
    def stats(v, minutes, turns, wakes, blocks, chars):
        return {"id": f"s-{v}", "user_id": "x" * 26, "create_at": 1, "message":
                "STATS " + json.dumps({"v": v, "min": minutes, "turns": turns,
                                        "wakes": {"addressed": wakes}, "blocks": {"listen": blocks},
                                        "tools": {"wait_for_events": [wakes, chars]},
                                        "posts": [1, 120], "compact": [0, 0], "static": 29500})}
    mm.posts[:0] = [stats("0.20.0", 60, 20, 4, 2, 4000), stats("0.21.0", 120, 40, 2, 2, 1000),
                    {"id": "noise", "user_id": "x" * 26, "create_at": 1, "message": "hello"}]
    r = run(REPORT, "report", "agents-stats", "--json")
    assert r.code == 0, r.stderr
    rows = {row["version"]: row for row in json.loads(r.stdout)}
    assert rows["0.20.0"]["plugin_turns_per_h"] == 6.0 and rows["0.20.0"]["plugin_share_pct"] == 23
    assert rows["0.21.0"]["plugin_turns_per_h"] == 2.0 and rows["0.21.0"]["bus_chars_per_turn"] == 23
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


def test_off_counts_nothing(mm, run):
    run(PROMPT, MM_STATS_CHANNEL="off")
    assert events(run) == []

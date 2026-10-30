"""RT-6.7 handoff state after compaction (docs/arc42/06-runtime-view.md)."""

from __future__ import annotations

import json

COMPACT = "hooks/after-compact.sh"


def context(r) -> str:
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"] if r.stdout else ""


def test_compaction_hands_back_the_own_latest_posts(mm, run):
    """RT-6.7 steps 1-2: the newest own posts and the HELLO thread come back."""
    mm.own_post("HELLO @agent-test\nproject: x", 1000)
    for i in range(4):
        mm.own_post(f"PROGRESS {i}", 2000 + i)
    mm.own_post("IDLE done: a; open: b; next: c", 9000)
    mm.posts.insert(0, {"id": "other", "user_id": "z" * 26, "create_at": 9500,
                        "message": "not mine"})
    r = run(COMPACT)
    c = context(r)
    assert r.code == 0 and json.loads(r.stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    lines = [l for l in c.splitlines() if l.startswith("- [")]
    assert len(lines) == 3 and "IDLE done: a; open: b; next: c" in lines[0]
    assert "not mine" not in c
    hello = next(p["id"] for p in mm.posts if p["message"].startswith("HELLO"))
    assert f"root {hello}" in c and "wait_for_events(timeout_sec=1)" in c


def test_compaction_names_the_files_of_an_own_post(mm, run):
    """RT-6.7, RT-6.23 step 1: an own post's files follow its text as a push
    names them, so get_file can fetch one after the compaction."""
    fid = "f" * 26
    mm.own_post("RESULT · see the screenshot", 5000)
    mm.posts[0] |= {"file_ids": [fid], "metadata": {"files": [{"id": fid, "name": "shot.png",
                                                               "extension": "png"}]}}
    mm.own_post("IDLE · nothing attached", 6000)
    lines = [l for l in context(run(COMPACT)).splitlines() if l.startswith("- [")]
    assert lines[1].endswith(f"] RESULT · see the screenshot · files: {fid} shot.png")
    assert lines[0].endswith("] IDLE · nothing attached")


def test_nothing_to_hand_back_without_own_posts(mm, run):
    """RT-6.7 error handling: no own posts, no bus → no output, the session goes on."""
    assert run(COMPACT).stdout == ""
    assert run(COMPACT, MM_BOT_TOKEN=None).stdout == ""


def test_signoff_asks_for_a_handoff_with_next_step(mm, run):
    """RT-6.7 step 0: the sign-off is written to survive compaction."""
    mm.own_post("HELLO working on x")
    r = run("hooks/stop-inbox-check.sh")
    assert r.blocked and "next step" in r.reason and "compacted" in r.reason

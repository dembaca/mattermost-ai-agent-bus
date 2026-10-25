"""RT-6.2 end of a turn, RT-6.3 listen window ended by the human,
RT-6.4 bus unavailable (docs/arc42/06-runtime-view.md)."""

from __future__ import annotations

import json
import os
import subprocess
import time

from conftest import CHANNEL, ME, PROJECT, ROOT, TEAM

STOP = "hooks/stop-inbox-check.sh"
PROMPT = "hooks/turn-status.sh"
IN_PROJECT = {"MM_PROJECT_CHANNEL_ID": PROJECT}


def turn_state(run) -> dict[str, int]:
    f = run.state_dir / f"turn-{ME}.env"
    if not f.exists():
        return {}
    return {k: int(v) for k, v in (l.split("=") for l in f.read_text().split())}


LISTEN = "bin/mm-agent-listen.sh"


def window(run) -> dict[str, str]:
    f = run.state_dir / f"listen-{ME}.env"
    if not f.exists():
        return {}
    return dict(l.split("=", 1) for l in f.read_text().splitlines())


def set_window(run, **values) -> None:
    f = run.state_dir / f"listen-{ME}.env"
    f.parent.mkdir(parents=True, exist_ok=True)
    w = window(run) | {k: str(v) for k, v in values.items()}
    f.write_text("".join(f"{k}={v}\n" for k, v in w.items()))


def until_in(reason: str) -> int:
    return int(reason.split("--until ", 1)[1].split("`", 1)[0])


# --- RT-6.2 -------------------------------------------------------------------

def test_drain_blocks_at_most_three_times(mm, run):
    """RT-6.2 step 1: pending mentions/DMs block the stop, bounded per turn."""
    mm.unread_dm(2)
    for _ in range(3):
        r = run(STOP)
        assert r.blocked and "2 unread message(s)" in r.reason
    assert run(STOP).passed


def test_signoff_requested_once_per_turn(mm, run):
    """RT-6.2 step 2: a busy latest post asks for IDLE/BYE once."""
    mm.own_post("HELLO working on x")
    r = run(STOP)
    assert r.blocked and "IDLE" in r.reason
    assert run(STOP).passed


def test_coordinators_signoff_counts_with_its_crown(mm, run):
    """RT-6.2 step 2, RT-6.6: the 👑 before a coordinator's IDLE is no new post."""
    mm.own_post("👑 IDLE · done: x · next: y")
    assert run(STOP, MM_LISTEN_SEC="0").passed


BUS_PROMPT = json.dumps({"prompt": '<channel source="plugin:mattermost-ai-agent-bus:mattermost-agent-bus" kind="dm">hi</channel>'})


def test_turn_woken_by_the_channel_needs_no_signoff(mm, run):
    """RT-6.12 step 6: a turn a channel push started answers the bus and needs
    no sign-off of its own; a turn the human started still asks for one, in
    words that fit a session the channel keeps reachable."""
    mm.own_post("Done, merged.", 1000)
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / f"channel-{ME}.env").write_text("CONFIRMED=1\n")
    assert run(PROMPT, stdin=BUS_PROMPT).code == 0
    assert run(STOP, **IN_PROJECT).passed
    assert run(PROMPT, stdin=json.dumps({"prompt": "fix the tests"})).code == 0
    r = run(STOP, **IN_PROJECT)
    assert r.blocked and "still reach you through the channel" in r.reason
    # Without a confirmed channel the tag proves nothing: sign off as usual.
    (run.state_dir / f"channel-{ME}.env").unlink()
    assert run(PROMPT, stdin=BUS_PROMPT).code == 0
    r = run(STOP, **IN_PROJECT)
    assert r.blocked and "cannot reach you" in r.reason


def last_post(run, msg: str, at: int = 1000, hello: str = "h1") -> None:
    """The file whoever posts as the agent keeps (ownpost.py, mm-agent-post.sh)."""
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / f"last-{ME}.env").write_text(
        f"CHANNEL={CHANNEL}\nID=p1\nAT={at}\nMSG={msg}\nHELLO={hello}\n")


def test_own_last_post_comes_from_the_file_not_the_channel(mm, run):
    """RT-6.2 step 2: with last-<uid>.env the hook reads no posts: what the file
    says counts, whatever the channel would show. Without it, or with a file
    that says nothing usable, the channel is read as before."""
    mm.own_post("HELLO working on x")
    last_post(run, "IDLE · done: x")
    assert run(STOP, MM_LISTEN_SEC="0").passed
    mm.posts.clear()
    last_post(run, "PROGRESS · still going")
    r = run(STOP)
    assert r.blocked and "IDLE" in r.reason
    (run.state_dir / f"last-{ME}.env").write_text("CHANNEL=x\nAT=\n")
    mm.own_post("HELLO working on x")
    run.state_dir.joinpath(f"turn-{ME}.env").unlink()
    assert run(STOP).blocked


def test_listen_window_follows_the_files_post_time(mm, run):
    """RT-6.2 step 5: a fresh post since the window opened opens a fresh window,
    read from the file as from the channel."""
    last_post(run, "IDLE · done: x", at=int(time.time() * 1000) + 5000)
    set_window(run, UNTIL=int(time.time()) + 600, SINCE=int(time.time()) - 60)
    r = run(STOP, **IN_PROJECT)
    assert r.blocked and "mm-agent-listen.sh" in r.reason
    assert int(window(run)["SINCE"]) >= int(time.time()) - 5


def test_the_cli_post_keeps_the_file(mm, run):
    """RT-6.2: bin/mm-agent-post.sh keeps the same file for the work channel,
    with the HELLO thread; a post in another channel leaves it alone."""
    post = "bin/mm-agent-post.sh"
    assert run(post, PROJECT, "HELLO @agent-test\nproject: x", **IN_PROJECT).code == 0
    f = run.state_dir / f"last-{ME}.env"
    text = f.read_text()
    assert f"CHANNEL={PROJECT}" in text and "AT=5000" in text and f"HELLO={'n' * 26}" in text
    assert "MSG=HELLO @agent-test project: x" in text
    assert run(post, PROJECT, "👑 PROGRESS · building", "r" * 26, **IN_PROJECT).code == 0
    text = f.read_text()
    assert "MSG=PROGRESS · building" in text and f"HELLO={'n' * 26}" in text
    assert run(post, "d" * 26, "private", **IN_PROJECT).code == 0
    assert f.read_text() == text


def test_no_signoff_without_own_post(mm, run):
    """RT-6.2 step 2: an agent that never posted is not part of the coordination."""
    assert run(STOP).passed


def test_listen_window_opens_in_project_channel(mm, run):
    """RT-6.2 step 3: after the sign-off the agent is asked to start the
    background listener for the window; waiting costs no model calls."""
    mm.own_post("IDLE done x", 1000)
    r = run(STOP, **IN_PROJECT)
    assert r.blocked and "mm-agent-listen.sh" in r.reason and "run_in_background" in r.reason
    # The runtime's 30 min default would cut the window short.
    assert "timeout 5460000" in r.reason
    assert int(until_in(r.reason) - time.time()) in range(5390, 5401)
    status = mm.custom_status()
    assert status["emoji"] == "ear" and status["duration"] == "date_and_time"


def test_channel_session_needs_no_listener(mm, run):
    """RT-6.12: started as a channel and confirmed, the MCP server pushes the
    messages into the session, so the stop goes idle instead of asking for a
    listener."""
    mm.own_post("IDLE done x", 1000)
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / f"channel-{ME}.env").write_text("CONFIRMED=1\n")
    r = run(STOP, **IN_PROJECT)
    assert r.passed
    assert window(run).get("UNTIL", "0") == "0"
    assert mm.custom_status().get("emoji") != "ear"
    # MM_CHANNEL_PUSH=0 overrides: back to the listener.
    assert run(STOP, MM_CHANNEL_PUSH="0", **IN_PROJECT).blocked


def test_unconfirmed_channel_keeps_the_listener(mm, run):
    """RT-6.12 step 1: the start flag or MM_CHANNEL_PUSH alone proves nothing —
    the session may not receive a single push — so until the agent confirmed
    the check the stop asks for a listener as without a channel."""
    mm.own_post("IDLE done x", 1000)
    r = run(STOP, MM_CHANNEL_PUSH="1", **IN_PROJECT)
    assert r.blocked and "mm-agent-listen.sh" in r.reason


def test_running_listener_lets_the_turn_end(mm, run):
    """RT-6.2 step 4: with a listener waiting, the stop goes through and stays 👂."""
    mm.own_post("IDLE done x", 1000)
    run(STOP, **IN_PROJECT)
    sleeper = subprocess.Popen(["sleep", "30"])
    try:
        set_window(run, PID=sleeper.pid, STARTED=int(time.time()) + 1)
        writes = len(mm.writes)
        assert run(STOP, **IN_PROJECT).passed
        assert not any(b.get("emoji") == "zzz" for _, _, b in mm.writes[writes:])
    finally:
        sleeper.kill()


def test_listener_never_started_goes_idle(mm, run):
    """RT-6.2 error handling: asked once, no listener → idle instead of asking forever."""
    mm.own_post("IDLE done x", 1000)
    assert run(STOP, **IN_PROJECT).blocked
    assert run(STOP, **IN_PROJECT).passed
    assert mm.custom_status()["emoji"] == "zzz" and window(run) == {}


def test_listen_window_resumes_with_rest(mm, run):
    """RT-6.2 step 5: woken by a message and done, the window resumes with its rest."""
    mm.own_post("IDLE done x", 1000)
    run(STOP, **IN_PROJECT)
    now = int(time.time())
    set_window(run, SINCE=now - 100, UNTIL=now + 300, STARTED=now, PID=0, ENDED="event")
    r = run(STOP, **IN_PROJECT)
    assert r.blocked and until_in(r.reason) == now + 300


def test_drain_shows_a_banner(mm, run):
    """RT-6.2 step 1: the human sees why the turn has not ended yet."""
    mm.unread_dm(1)
    assert "1 unread" in run(STOP).banner


def test_signoff_and_listen_show_a_banner(mm, run):
    """RT-6.2 steps 2-3: the listen banner names the window end."""
    mm.own_post("HELLO working on x")
    assert "Signing off" in run(STOP).banner
    mm.own_post("IDLE done x")
    r = run(STOP, **IN_PROJECT)
    until = time.strftime("%H:%M", time.localtime(until_in(r.reason)))
    assert r.banner.startswith("👂") and f"until {until}" in r.banner
    assert "90 min" in r.banner and "next prompt" in r.banner


def test_own_answer_opens_fresh_window_without_second_signoff(mm, run):
    """RT-6.2 step 5: an answer on the bus restarts the window; no second IDLE."""
    mm.own_post("IDLE done x", 1000)
    run(STOP, MM_LISTEN_SEC="60", **IN_PROJECT)
    now = int(time.time())
    set_window(run, SINCE=now - 100, UNTIL=now + 10, STARTED=now, ENDED="event")
    mm.own_post("ACK will do")
    r = run(STOP, MM_LISTEN_SEC="60", **IN_PROJECT)
    assert r.blocked and until_in(r.reason) - now in range(58, 62)


def test_coordinator_listens_longer(mm, run):
    """RT-6.2 step 3, RT-6.6: the 👑 holder gets MM_LISTEN_COORD_SEC (default 7200)."""
    d = run.state_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"coord-{ME}.env").write_text(f"CROWN_UNTIL={int(time.time()) + 600}\n")
    mm.own_post("IDLE done x", 1000)
    r = run(STOP, **IN_PROJECT)
    assert r.blocked and int(until_in(r.reason) - time.time()) in range(7190, 7201)
    assert "120 min" in r.banner and "timeout 7200000" in r.reason


def test_window_over_goes_idle(mm, run):
    """RT-6.2 step 6: window over → 💤 idle, turn and window state removed."""
    mm.own_post("IDLE done x", 1000)
    run(STOP, **IN_PROJECT)
    set_window(run, SINCE=1, UNTIL=2, STARTED=int(time.time()), ENDED="over")
    assert run(STOP, **IN_PROJECT).passed
    assert mm.custom_status()["emoji"] == "zzz"
    assert turn_state(run) == {} and window(run) == {}


def test_no_window_after_bye_or_when_off_or_without_project(mm, run):
    """RT-6.2 step 3: BYE, MM_LISTEN_SEC=0 and no project channel skip the window."""
    mm.own_post("BYE done", 1000)
    assert run(STOP, **IN_PROJECT).passed
    mm.own_post("IDLE done", 2000)
    assert run(STOP, MM_LISTEN_SEC="0", **IN_PROJECT).passed
    assert run(STOP).passed


def test_stateless_fallback_without_bot_id(mm, run):
    """RT-6.2 error handling: bot id unknown → stop_hook_active ends the turn."""
    r = run(STOP, stdin='{"stop_hook_active": true}', MM_BOT_USER_ID="not-an-id")
    assert r.passed


def test_listener_wakes_on_a_dm(mm, run):
    """RT-6.2 step 4: the listener exits on a message and says what to do."""
    mm.unread_dm(1)
    until = int(time.time()) + 30
    r = run(LISTEN, "--until", str(until), MM_LISTEN_POLL_SEC="1", **IN_PROJECT)
    assert r.code == 0 and "wait_for_events(timeout_sec=1)" in r.stdout
    assert window(run)["ENDED"] == "event" and window(run)["UNTIL"] == str(until)


OTHER = "z" * 26


def other_post(mm, message, root_id="", user_id=OTHER, age_s=0, props=None):
    mm.posts.insert(0, {"id": f"o{len(mm.posts)}", "user_id": user_id, "root_id": root_id,
                        "create_at": int((time.time() - age_s) * 1000) + 2000,
                        "message": message, "type": "", "props": props})


def listen(run, window=4, **env):
    return run(LISTEN, "--until", str(int(time.time()) + window), MM_LISTEN_POLL_SEC="1",
               MM_BOT_USERNAME="agent-test", **IN_PROJECT, **env)


def test_listener_ignores_posts_not_for_it(mm, run):
    """RT-6.2 step 4: a post for nobody in particular does not wake a member."""
    other_post(mm, "general chatter about lunch")
    r = listen(run)
    assert "nothing new" in r.stdout


def test_listener_wakes_when_addressed(mm, run):
    """RT-6.2 step 4: @name, @channel or a reply in its thread wake it."""
    mm.own_post("HELLO @agent-test", 1000)
    my_root = mm.posts[0]["id"]
    cases = [
        ({"message": "hey @Agent-Test, can you look?"}, {}),
        ({"message": "@channel deploy freeze at 5"}, {}),
        ({"message": "answer", "root_id": my_root}, {}),
    ]
    for post, env in cases:
        mm.posts = [p for p in mm.posts if p["user_id"] == mm.posts[-1]["user_id"]]
        other_post(mm, **post)
        r = listen(run, **env)
        assert "for you" in r.stdout, post


TO_ALL = {"a2a": {"v": 1, "to": "all"}}


def test_listener_wakes_for_a_post_to_all_agents(mm, run):
    """RT-6.2 step 4, RT-6.11: agents write props.a2a.to = "all" instead of
    @channel, so the text names nobody; other props, whatever their shape, are
    a plain post, and one the MCP server gated wakes nobody (RT-6.16)."""
    for props, woken in ((TO_ALL, True), ({"a2a": {"to": "everyone"}}, False),
                         ({"a2a": "all"}, False), ("all", False), (None, False)):
        mm.posts = []
        other_post(mm, "main is red, hold merges", props=props)
        assert ("for you" in listen(run, window=3).stdout) is woken, props
    gate_file = run.state_dir / f"gate-{ME}.env"
    gate_file.parent.mkdir(parents=True, exist_ok=True)
    mm.posts = []
    other_post(mm, "main is red", props=TO_ALL)
    gate_file.write_text(f"GATED={mm.posts[0]['id']} {int(time.time())}\n")
    assert "nothing new" in listen(run, window=3).stdout


def unread_channel(mm, viewed_ms=0, unread=True):
    """The work channel as the bot's counters show it: a post unread, no mention."""
    mm.members.append({"channel_id": PROJECT, "msg_count": 0 if unread else 3,
                       "mention_count": 0, "last_viewed_at": viewed_ms})
    mm.channels.append({"id": PROJECT, "type": "O", "total_msg_count": 3})


def test_drain_notices_a_post_to_all_agents(mm, run):
    """RT-6.2 step 1, RT-6.11: a post for all agents holds no @channel, so no
    mention counter moves; the drain finds it in the channel's unread posts. Not
    a plain post, an own one, one the bot viewed since, or one the server gated."""
    unread_channel(mm)
    other_post(mm, "main is red, hold merges", props=TO_ALL)
    r = run(STOP)
    assert r.blocked and "1 unread message(s)" in r.reason
    mm.posts = []
    other_post(mm, "main is red")
    other_post(mm, "IDLE · done", props=TO_ALL, user_id=ME)
    other_post(mm, "odd", props={"a2a": "all"})
    assert run(STOP, MM_LISTEN_SEC="0").passed
    mm.posts = []
    other_post(mm, "main is red", props=TO_ALL)
    mm.members, mm.channels = [], []
    unread_channel(mm, viewed_ms=int(time.time() * 1000) + 60_000)
    assert run(STOP, MM_LISTEN_SEC="0").passed  # read since
    mm.members, mm.channels = [], []
    unread_channel(mm, unread=False)
    assert run(STOP, MM_LISTEN_SEC="0").passed  # no unread post: not even looked at
    mm.members, mm.channels = [], []
    unread_channel(mm)
    gate_file = run.state_dir / f"gate-{ME}.env"
    gate_file.parent.mkdir(parents=True, exist_ok=True)
    gate_file.write_text(f"GATED={mm.posts[0]['id']} {int(time.time())}\n")
    assert run(STOP, MM_LISTEN_SEC="0").passed


FYI = {"a2a": {"v": 1, "kind": "done", "intent": "fyi"}}
PERSON = "q" * 26


def fyi_authors(mm):
    """OTHER is an agent; PERSON is a person who wrote the same props by hand."""
    mm.users += [{"id": OTHER, "username": "agent-other", "is_bot": True},
                 {"id": PERSON, "username": "agent-person", "is_bot": False}]


def test_listener_holds_an_agents_fyi_post(mm, run):
    """RT-6.14, RT-6.2 step 4: an agent's fyi post (DONE, RESULT, IDLE…) does not
    wake one who only posted in its thread, nor the coordinator: it waits for
    the digest. It wakes the author of the thread's root, who waits for it, and
    whom it names. A person's props count for nothing."""
    fyi_authors(mm)
    mm.own_post("TASK · relay", 1000)
    mine = mm.posts[0]["id"]
    other_post(mm, "TASK · other work", user_id="y" * 26, age_s=60)
    theirs = mm.posts[0]["id"]
    mm.own_post("ACK", 2000)
    mm.posts[0]["root_id"] = theirs
    base = list(mm.posts)
    cases = [
        ({"root_id": theirs}, False), ({"root_id": mine}, True),
        ({"root_id": theirs, "user_id": PERSON}, True),
        ({"root_id": theirs, "message": "DONE · @agent-test rebase on main"}, True),
    ]
    for post, woken in cases:
        mm.posts = list(base)
        other_post(mm, **({"message": "DONE · merged", "props": FYI} | post))
        assert ("for you" in listen(run, window=3).stdout) is woken, post
    d = run.state_dir
    (d / f"coord-{ME}.env").write_text(f"CROWN_UNTIL={int(time.time()) + 600}\n")
    mm.posts = list(base)
    other_post(mm, "IDLE · done: x", props=FYI)
    assert "for you" not in listen(run, window=3).stdout
    mm.posts = list(base)
    other_post(mm, "IDLE · done: x", props=FYI)
    r = listen(run, window=10, MM_LISTEN_DIGEST_SEC="1")
    assert "1 post(s)" in r.stdout and "1 of them fyi" in r.stdout


def test_drain_ranks_fyi_posts(mm, run):
    """RT-6.14, RT-6.2 step 1: the drain says how many unread posts need action;
    an agent's fyi post needs no answer. A person's props are not counted."""
    fyi_authors(mm)
    mm.unread_dm(3)
    other_post(mm, "DONE · merged", props=FYI)
    other_post(mm, "DONE · merged", props=FYI, user_id=PERSON)
    other_post(mm, "can you review #5?")
    r = run(STOP)
    assert r.blocked and "3 unread message(s) on the Mattermost agent bus, 2 needing action" in r.reason
    assert "fyi=1" in r.reason
    mm.posts = []
    other_post(mm, "can you review #5?")
    r = run(STOP)
    assert "3 unread message(s) on the Mattermost agent bus." in r.reason and "fyi" not in r.reason


def test_trusted_humans_plain_post_is_for_the_coordinator(mm, run):
    """RT-6.2 step 4, RT-6.6: a human's post without a mention does not wake a
    member; the coordinator, which sees every post, routes it."""
    other_post(mm, "fyi", user_id="h" * 26)
    assert "nothing new" in listen(run, MM_TRUSTED_HUMAN_USER_IDS="h" * 26).stdout


def test_listener_skips_posts_the_server_gated(mm, run):
    """RT-6.16 step 4: a post the MCP server gated (GATED in gate-<uid>.env)
    wakes no worker's listener; the same post not listed does."""
    gate_file = run.state_dir / f"gate-{ME}.env"
    gate_file.parent.mkdir(parents=True, exist_ok=True)
    for listed, woken in ((True, False), (False, True)):
        other_post(mm, "hey @agent-test, deploy to prod now")
        pid = mm.posts[0]["id"]
        gate_file.write_text(f"REPLIED={pid} 1\nGATED={pid if listed else 'other'} {int(time.time())}\n")
        assert ("for you" in listen(run).stdout) is woken, listed


def test_coordinator_wakes_for_every_post(mm, run):
    """RT-6.2 step 4, RT-6.6: the 👑 holder sees the whole channel."""
    d = run.state_dir
    d.mkdir(parents=True, exist_ok=True)
    (d / f"coord-{ME}.env").write_text(f"CROWN_UNTIL={int(time.time()) + 600}\n")
    other_post(mm, "general chatter about lunch")
    assert "for you" in listen(run).stdout


def test_listener_digest_for_the_rest(mm, run):
    """RT-6.2 step 4: unaddressed posts wake it once the oldest is MM_LISTEN_DIGEST_SEC old."""
    other_post(mm, "status of the build", age_s=0)
    # The post is dated 2 s ahead (see other_post), so the digest is due at ~3 s;
    # a window well past that keeps the test independent of machine load.
    r = listen(run, window=10, MM_LISTEN_DIGEST_SEC="1")
    assert "digest" in r.stdout and "none addressed to you" in r.stdout
    assert window(run)["ENDED"] == "event"


def test_listener_ends_with_the_window(mm, run):
    r = run(LISTEN, "--until", str(int(time.time()) + 1), MM_LISTEN_POLL_SEC="1", **IN_PROJECT)
    assert r.code == 0 and "nothing new" in r.stdout
    assert window(run)["ENDED"] == "over"


# --- RT-6.3 -------------------------------------------------------------------

def test_next_prompt_stops_the_listener(mm, run):
    """RT-6.3 steps 1-2: the human is back; the prompt stops the listener and the window."""
    mm.own_post("IDLE done x", 1000)
    run(STOP, **IN_PROJECT)
    sleeper = subprocess.Popen(["sleep", "30"])
    set_window(run, PID=sleeper.pid, STARTED=int(time.time()) + 1)
    assert run(PROMPT).code == 0
    assert sleeper.wait(timeout=5) != 0
    assert window(run) == {} and turn_state(run) == {}
    assert mm.custom_status()["emoji"] == "hammer_and_wrench"


def test_stopped_listener_ends_cleanly(mm, run, tmp_path):
    """RT-6.3 step 1: stopped by the prompt hook, the listener exits 0, so the
    runtime does not list it as failed, and leaves the window file alone."""
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path),
           "XDG_STATE_HOME": str(tmp_path / "state"), "MM_CHAT_URL": mm.url,
           "MM_BOT_TOKEN": "bot-token-for-tests", "MM_BOT_USER_ID": ME, "MM_TEAM_ID": TEAM,
           "MM_CHANNEL_ID": CHANNEL, "MM_LISTEN_POLL_SEC": "1", **IN_PROJECT}
    p = subprocess.Popen(["bash", str(ROOT / LISTEN), "--until", str(int(time.time()) + 30)],
                         env=env, stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)
    (run.state_dir / f"listen-{ME}.env").unlink()   # what the prompt hook does
    p.terminate()
    out, _ = p.communicate(timeout=10)
    assert p.returncode == 0 and "nothing to do" in out
    assert not (run.state_dir / f"listen-{ME}.env").exists()


def test_turn_woken_by_a_message_keeps_the_window(mm, run):
    """RT-6.3: a turn the listener started keeps the window for the Stop hook."""
    set_window(run, UNTIL=int(time.time()) + 300, PID=0, ENDED="event")
    assert run(PROMPT).code == 0
    assert window(run)["ENDED"] == "event"


def test_window_that_ran_out_opens_no_next_one(mm, run):
    """RT-6.3: the end of a quiet window starts a turn; that turn keeps the
    ended window, so its Stop hook goes idle instead of opening the next."""
    mm.own_post("IDLE done x", 1000)
    now = int(time.time())
    set_window(run, SINCE=now - 400, UNTIL=now - 10, PID=0, ENDED="over")
    assert run(PROMPT).code == 0
    assert window(run)["ENDED"] == "over"
    assert run(STOP, **IN_PROJECT).passed


def test_listening_status_expires_with_window(mm, run):
    """RT-6.3 step 3, RT-6.6: the 👂 status ends with the window at the latest,
    and within one lease when nobody renews it."""
    fmt = "%Y-%m-%dT%H:%M:%SZ"
    mm.own_post("IDLE done x", 1000)
    r = run(STOP, MM_LISTEN_SEC="300", **IN_PROJECT)
    assert time.strftime(fmt, time.gmtime(until_in(r.reason))) == mm.custom_status()["expires_at"]
    (run.state_dir / f"listen-{ME}.env").unlink()   # next turn: a fresh default window
    r = run(STOP, **IN_PROJECT)
    assert mm.custom_status()["expires_at"] < time.strftime(fmt, time.gmtime(until_in(r.reason)))


# --- RT-6.4 -------------------------------------------------------------------

def test_bus_unreachable_never_blocks(run):
    """RT-6.4: Mattermost unreachable → every hook exits 0."""
    dead = {"MM_CHAT_URL": "http://127.0.0.1:9", "MM_PROJECT_CHANNEL_ID": PROJECT}
    assert run(STOP, **dead).passed
    assert run(PROMPT, **dead).code == 0


def test_no_token_never_blocks(run):
    """RT-6.4: no bus configured → no opinion."""
    assert run(STOP, MM_BOT_TOKEN=None).passed


ASK = "q" * 26
HUMAN = "h" * 26


def test_listener_wakes_for_a_reaction_to_an_open_ask(mm, run):
    """RT-6.11: a 👍 on an ask raises no unread counter; the listener looks at the
    open asks and wakes for a reaction the agent has not been handed yet."""
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / f"asks-{ME}.env").write_text(
        f"ASK={ASK} {int(time.time())}\nSEEN={ASK}:{HUMAN}:eyes\n")
    mm.reactions[ASK] = [{"user_id": HUMAN, "post_id": ASK, "emoji_name": "eyes"},
                         {"user_id": ME, "post_id": ASK, "emoji_name": "white_check_mark"}]
    quiet = run(LISTEN, "--until", str(int(time.time()) + 3), MM_LISTEN_POLL_SEC="1",
                **IN_PROJECT)
    assert window(run)["ENDED"] == "over", quiet.stdout  # seen and own: nothing new
    mm.reactions[ASK].append({"user_id": HUMAN, "post_id": ASK, "emoji_name": "+1"})
    r = run(LISTEN, "--until", str(int(time.time()) + 30), MM_LISTEN_POLL_SEC="1",
            **IN_PROJECT)
    assert r.code == 0 and "1 reaction(s) to your ask(s)" in r.stdout
    assert "resolve_ask" in r.stdout and window(run)["ENDED"] == "event"


def newer_plugin(tmp_path, version="99.0.0"):
    import json
    d = tmp_path / "cache" / "mattermost-ai-agent-bus" / version
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    reg = tmp_path / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"plugins": {"mattermost-ai-agent-bus@x": [{"installPath": str(d)}]}}))


def test_a_newer_installed_plugin_is_named_once_when_the_turn_ends(mm, run, tmp_path):
    """RT-6.2 step 6: a session keeps the plugin it started with; when a newer one
    is installed (an automatic update, too), the human learns once per version
    that a restart switches to it."""
    newer_plugin(tmp_path)
    r = run(STOP)
    assert r.passed and "plugin 99.0.0 is installed" in r.banner
    assert "quit, then claude --continue" in r.banner
    again = run(STOP)
    assert again.passed and again.banner == ""


def test_no_update_note_when_the_session_runs_the_newest(mm, run, tmp_path):
    newer_plugin(tmp_path, version="0.0.1")  # older than this tree
    assert run(STOP).banner == ""

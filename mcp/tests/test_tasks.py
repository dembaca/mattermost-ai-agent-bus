"""RT-6.14: A2A shapes on the bus — cards, task states, and what they save."""

import inspect
import json
import time

import pytest

from mattermost_ai_agent_bus import a2a, channel, server, tasks

ME, LEIT, A, B, HUMAN = "m" * 26, "l" * 26, "a" * 26, "b" * 26, "h" * 26
NAMES = {ME: "agent-me", LEIT: "agent-leit", A: "agent-a", B: "agent-b", HUMAN: "dembaca"}
NOW = 1_800_000_000_000


def P(pid, uid, message, *, root="", at=0, **extra):
    return {"id": pid, "user_id": uid, "message": message, "root_id": root,
            "create_at": NOW - 3_600_000 + at, "type": "", "delete_at": 0, **extra}


@pytest.mark.parametrize("message,kind", [
    ("ACK", "ACK"), ("👑 ACK noted, agent-me", "ACK"), ("**DONE** #46 merged", "DONE"),
    ("ACK – on it, ~20 min", "ACK"), ("IDLE · done: x", "IDLE"), ("PR #57 https://…", "RESULT"),
    ("NO – outside my sandbox", "NO"), ("No worries", None), ("ACKNOWLEDGED", None),
    ("OK", None), ("@agent-a ACK?", None), ("plain text", None),
])
def test_first_word_is_the_kind(message, kind):
    """RT-6.14 step 2: the bus's convention words, upper case, after 👑 or markdown."""
    assert a2a.word(message) == kind


def test_annotation_and_kind_from_props_or_text():
    """RT-6.14 step 2: props say it when there; old posts are read from their text."""
    assert a2a.annotate("👑 BLOCKED on review of #44") == {"v": 1, "kind": "blocked",
                                                         "state": "input-required"}
    assert a2a.annotate("thanks") is None
    assert a2a.annotate("IDLE · done") == {"v": 1, "kind": "idle"}
    assert a2a.kind_of({"message": "x", "props": {"a2a": {"kind": "result"}}}) == "RESULT"
    assert a2a.kind_of({"message": "👑 🙋 @dembaca — **Go?**"}) == a2a.ASK
    assert a2a.state(a2a.PERMISSION) == "auth-required"


def _leitstand_thread():
    """The permission-relay task of 2026-10-21 as it happened on the bus."""
    return [
        P("hello", ME, "HELLO @agent-me\nproject: bus · branch/PR: –\ntask: relay\n"
                       "model: Claude Code · Opus 5.5\nfor: dembaca · trusts: dembaca\n"
                       "capabilities: –\nrole: member", at=-100),
        P("t1", LEIT, "👑 TASK · bus · relay Claude Code permission prompts over the bus · "
                      "requested by dembaca\n\nThis changes arc42 RT-6.8.", at=0),
        P("r1", ME, "ACK · agent-me · branch worktree-permission-relay (PR to follow).",
          root="t1", at=10),
        P("r2", LEIT, "👑 ACK noted, agent-me. Post the PR link here.", root="t1", at=20),
        P("r3", ME, "PR #57 https://github.com/x/y/pull/57 (0.30.0, RT-6.13)", root="t1", at=30),
    ]


def test_task_from_a_real_thread():
    """RT-6.14 step 3: the root's author acknowledging does not take the task;
    the PR link moves it to review, the A2A state follows."""
    [row] = tasks.build(_leitstand_thread(), NAMES, now_ms=NOW)
    assert row["task"] == "t1" and row["by"] == "agent-leit" and row["agent"] == "agent-me"
    assert row["title"].startswith("bus · relay Claude Code permission prompts")
    assert (row["status"], row["state"]) == ("review", "input-required")
    assert row["pr"] == "#57" and row["branch"] == "worktree-permission-relay"
    assert row["spec"] == "RT-6.8"


def test_mentioned_agent_takes_declines_and_only_it_moves_the_task():
    """RT-6.14 step 3: with a mention only that agent can take it; NO hands it
    back; a third agent's words are data."""
    posts = [
        P("t", LEIT, "TASK · docs for Cursor · @agent-a", at=0),
        P("1", B, "ACK", root="t", at=1),
        P("2", A, "ACK", root="t", at=2),
        P("3", B, "DONE", root="t", at=3),
        P("4", A, "BLOCKED on review of #44", root="t", at=4),
    ]
    [row] = tasks.build(posts, NAMES, now_ms=NOW)
    assert (row["agent"], row["status"]) == ("agent-a", "blocked")
    posts.append(P("5", A, "NO – outside my sandbox", root="t", at=5))
    [row] = tasks.build(posts, NAMES, now_ms=NOW)
    assert row["status"] == "open" and "agent" not in row


def test_done_is_listed_once_and_cancel_never():
    """RT-6.14 step 3: DONE stays until the next ASSIGNMENTS (at most a day);
    a CANCEL by the coordinator drops the task."""
    posts = [
        P("t", LEIT, "TASK · x · @agent-a", at=0),
        P("1", A, "ACK", root="t", at=1),
        P("2", LEIT, "DONE #46 merged", root="t", at=2),
        P("u", LEIT, "TASK · y", at=3),
        P("3", B, "👑 CANCEL – not needed", root="u", at=4),
    ]
    assert [(r["task"], r["status"]) for r in tasks.build(posts, NAMES, now_ms=NOW)] == [("t", "done")]
    posts.append(P("as", LEIT, "ASSIGNMENTS · x done", at=5))
    assert tasks.build(posts, NAMES, now_ms=NOW) == []
    assert tasks.build(posts[:3], NAMES, now_ms=NOW + 2 * 86_400_000) == []


def test_only_the_task_thread_moves_the_task():
    """RT-6.14 step 3: what an agent posts in its HELLO thread may be about
    its next work (found live: a PROGRESS on new work reopened a task in
    review), so only the task's own thread counts."""
    posts = _leitstand_thread() + [
        P("p", ME, "PROGRESS · next work on branch a2a-shapes (stacked on #57)",
          root="hello", at=40),
        P("q", ME, "DONE", root="hello", at=50),
    ]
    [row] = tasks.build(posts, NAMES, now_ms=NOW)
    assert (row["status"], row["pr"], row["branch"]) == ("review", "#57", "worktree-permission-relay")


def test_open_asks_need_no_answer_yet():
    """RT-6.14 step 3, RT-6.11: an ask with a yes/no reaction or the human's
    reply is answered; the rest is what needs the human."""
    posts = [
        P("q1", A, "👑 🙋 @dembaca — **Go to install 02dadca on hogan?**", at=0),
        P("q2", B, "🙋 @dembaca — **Merge #44?**", at=1,
          metadata={"reactions": [{"emoji_name": "+1", "user_id": HUMAN}]}),
        P("q3", B, "🙋 @dembaca — **Rename the module?**", at=2),
        P("r", HUMAN, "yes, rename it", root="q3", at=3),
    ]
    assert tasks.open_asks(posts, NAMES) == [{
        "ask": "q1", "by": "agent-a", "human": "dembaca",
        "need": "Go to install 02dadca on hogan?", "at": tasks._when(NOW - 3_600_000)}]


def test_roster_merges_lease_and_card():
    """RT-6.14 step 4: status from the lease, the rest from the newest HELLO
    (props first, else its text)."""
    card = a2a.card(name="agent-a", task="docs", version="0.31.0", capabilities="gh push",
                    model="Claude Code · Sonnet 5.5", branch="feat/docs")
    posts = [P("ha", A, "HELLO @agent-a", props={"agent_card": card}, at=0),
             P("hb", B, "HELLO @agent-b\nproject: bus · branch/PR: #9\ntask: review\n"
                        "model: Cursor · GPT\nfor: dembaca · trusts: –\ncapabilities: –\n"
                        "role: member", at=1)]
    rows = [{"username": "agent-b", "emoji": "crown", "coordinator": True,
             "text": "coordinator · working · bus"},
            {"username": "agent-a", "emoji": "ear", "coordinator": False,
             "text": "listening · until 14:00, then idle"}]
    assert tasks.roster(rows, posts, NAMES) == [
        {"agent": "agent-b", "status": "working", "coordinator": True, "model": "Cursor · GPT",
         "role": "member", "task": "review", "branch": "#9"},
        {"agent": "agent-a", "status": "listening", "model": "Claude Code · Sonnet 5.5",
         "role": "member", "task": "docs", "branch": "feat/docs", "skills": ["gh push"]},
    ]


def test_table_names_agents_without_mentions():
    """RT-6.14 step 5: a table is no request — no @, so nobody is woken by it."""
    text = tasks.table([{"agent": "agent-a", "title": "a | b", "status": "review",
                         "spec": "RT-6.5", "pr": "#24"}])
    assert "| agent-a | a / b | review | RT-6.5 | #24 |" in text and "@" not in text
    assert "no open tasks" in tasks.table([])


def test_compact_cuts_status_posts_only():
    """RT-6.14 step 6: other agents' status posts shrink to their first line."""
    idle = "IDLE · done: " + "x" * 300 + "\nmore"
    assert len(tasks.compact(idle)) == tasks.LINE and tasks.compact(idle).endswith("…")
    assert tasks.compact("ASSIGNMENTS · a took x\n| Agent |\n|---|") == "ASSIGNMENTS · a took x…"
    for whole in ("TASK · " + "y" * 300, "HELLO @a\nproject: x", "please look at #46\nthanks"):
        assert tasks.compact(whole) == whole


def test_digest_lines_use_the_compact_view():
    """RT-6.14 step 6, RT-6.12 step 3: a digest line of a status post is its first line."""
    item = {"post": {"id": "p", "author": "agent-a", "at": "09:00",
                     "message": "IDLE · done: x\nopen: " + "y" * 400}}
    line = channel.digest([item], time.time())["content"].splitlines()[1]
    assert line.endswith("IDLE · done: x…")


class _Client:
    def __init__(self, posts):
        self.posts = posts
        self.posted = []

    async def get_me(self):
        return {"id": ME, "username": "agent-me"}

    async def list_recent(self, channel_id=None, per_page=20, since_ms=None):
        assert since_ms and per_page == 200
        return self.posts

    async def usernames(self, ids):
        return {i: NAMES[i] for i in ids if i in NAMES}

    async def get_channel(self, cid):
        return {"name": "agents"}

    async def thread(self, root):
        return {"posts": [p for p in self.posts if root in (p["id"], p.get("root_id"))]}

    async def post(self, message, channel_id=None, root_id=None, props=None):
        self.posted.append({"message": message, "root_id": root_id, "props": props})
        return {"id": "new", "message": message, "user_id": ME, "channel_id": "c"}

    async def unread_channels(self):
        return []


@pytest.fixture
def bus(monkeypatch):
    def make(posts, crown=False):
        c = _Client(posts)
        monkeypatch.setattr(server, "_get_client", lambda: c)
        monkeypatch.setattr(server, "_inbox", None)
        monkeypatch.setattr(server, "_my_threads", set())
        monkeypatch.setattr(server.lease, "crown_held", lambda uid: crown)

        async def my_id(_client):
            return ME
        monkeypatch.setattr(server, "_my_id", my_id)
        return c
    return make


@pytest.mark.asyncio
async def test_posts_carry_their_kind(bus):
    """RT-6.14 step 2: the server writes props.a2a; a plain message gets none."""
    c = bus([])
    await server.reply_in_thread(root_id="t", message="ACK – on it")
    await server.post_message(message="anyone seen #46?")
    assert c.posted[0]["props"] == {"a2a": {"v": 1, "kind": "ack", "state": "working"}}
    assert c.posted[1]["props"] is None


@pytest.mark.asyncio
async def test_coordinator_reads_tasks_and_asks_not_history(bus):
    """RT-6.14 step 5: one compact row per task instead of the channel."""
    bus(_leitstand_thread() + [P("q", A, "🙋 @dembaca — **Merge #57?**", at=40)])
    out = json.loads(await server.coordinator("tasks"))
    assert [r["task"] for r in out["tasks"]] == ["t1"] and out["asks"][0]["ask"] == "q"
    assert len(json.dumps(out)) < len(json.dumps(_leitstand_thread())) / 2


@pytest.mark.asyncio
async def test_coordinator_roster(bus, monkeypatch):
    """RT-6.14 step 4: lease rows from mm-agent-roster.sh --json, cards from HELLO."""
    bus(_leitstand_thread())

    async def rows(_client):
        return [{"username": "agent-me", "emoji": "hammer_and_wrench", "text": "working · bus"}]
    monkeypatch.setattr(server.lease, "roster", rows)
    out = json.loads(await server.coordinator("roster"))
    assert out == {"roster": [{"agent": "agent-me", "status": "working",
                               "model": "Claude Code · Opus 5.5", "role": "member",
                               "task": "relay"}]}


@pytest.mark.asyncio
async def test_assignments_table_comes_from_the_server(bus):
    """RT-6.14 step 5: the coordinator names what changed, the server adds
    the table; a table of its own, or no crown, is left alone."""
    c = bus(_leitstand_thread(), crown=True)
    await server.post_message(message="ASSIGNMENTS · #57 in review")
    text = c.posted[0]["message"]
    assert text.startswith("👑 ASSIGNMENTS · #57 in review\n| Agent | Task |")
    assert "| agent-me | bus · relay Claude Code" in text and "| review | RT-6.8 | #57 |" in text
    assert c.posted[0]["props"]["a2a"]["kind"] == "assignments"
    own = "ASSIGNMENTS · mine\n| Agent | Task |\n|---|---|\n| a | b |"
    await server.post_message(message=own)
    assert c.posted[1]["message"] == "👑 " + own
    c = bus(_leitstand_thread(), crown=False)
    await server.post_message(message="ASSIGNMENTS · x")
    assert c.posted[0]["message"] == "ASSIGNMENTS · x"


@pytest.mark.asyncio
async def test_assignments_go_out_when_the_channel_cannot_be_read(bus):
    """RT-6.14 step 5: no table rather than no post."""
    c = bus([], crown=True)

    async def broken(**kw):
        raise RuntimeError("HTTP 500")
    c.list_recent = broken
    await server.post_message(message="ASSIGNMENTS · x")
    assert c.posted[0]["message"] == "👑 ASSIGNMENTS · x"


@pytest.mark.asyncio
async def test_bystander_gets_status_posts_cut(bus):
    """RT-6.14 step 6: in wait_for_events a status post of another agent is
    cut to its first line — not in my own thread, not for the coordinator."""
    long = "IDLE · done: x\nopen: " + "y" * 400
    c = bus([P("mine", ME, "TASK · z", at=0)])
    items = [{"kind": "watched_channel", "post": {"id": "1", "message": long}},
             {"kind": "watched_channel", "post": {"id": "2", "root_id": "mine", "message": long}},
             {"kind": "mention", "post": {"id": "3", "message": long}}]
    await server._compact_bystander(items)
    assert [i["post"]["message"] for i in items] == ["IDLE · done: x…", long, long]
    bus([], crown=True)
    items = [{"kind": "watched_channel", "post": {"id": "1", "message": long}}]
    await server._compact_bystander(items)
    assert items[0]["post"]["message"] == long
    assert c.posted == []


def test_static_budget():
    """RT-6.14 acceptance: what every turn carries does not grow. SKILL.md
    (every turn) and coordinator.md (the crown holder) stay within their 0.30.0
    size; the coordinator tool doc grows by its two new actions only."""
    from pathlib import Path

    skill = Path(server.__file__).resolve().parents[3] / "skills" / "mattermost-ai-agent-bus"
    assert len((skill / "SKILL.md").read_text()) <= 21_950  # + RT-6.15: link PRs/tickets
    assert len((skill / "coordinator.md").read_text()) <= 4_931
    # cleandoc: Python 3.13 strips a docstring's indentation, older ones don't.
    assert len(inspect.cleandoc(server.coordinator.__doc__)) <= 425 + 90

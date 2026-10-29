"""RT-6.14: A2A shapes on the bus — cards, task states, and what they save."""

import inspect
import json
import time
from pathlib import Path

import pytest

from mattermost_ai_agent_bus import a2a, channel, server, tasks
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import RosterEntry

from fakes import FakeChat

ME, LEIT, A, B, HUMAN = "m" * 26, "l" * 26, "a" * 26, "b" * 26, "h" * 26
NAMES = {ME: "agent-me", LEIT: "agent-leit", A: "agent-a", B: "agent-b", HUMAN: "dembaca"}
NOW = 1_800_000_000_000


def P(pid, uid, message, *, root="", at=0, **extra):
    return post_of({"id": pid, "user_id": uid, "message": message, "root_id": root,
                    "create_at": NOW - 3_600_000 + at, "type": "", "delete_at": 0, **extra})


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
    assert a2a.annotate("👑 BLOCKED on review of #44") == {
        "v": 1, "kind": "blocked", "state": "input-required", "intent": "reply"}
    assert a2a.annotate("thanks") is None
    assert a2a.annotate("thanks", "all") == {"v": 1, "to": "all"}  # RT-6.11
    assert a2a.annotate("IDLE · done", "all") == {"v": 1, "kind": "idle", "intent": "fyi",
                                                  "to": "all"}
    assert a2a.kind_of({"message": "x", "props": {"a2a": {"v": 1, "to": "all"}}}) is None
    assert [a2a.to_all({"props": {"a2a": a}}) for a in ({"to": "all"}, {"to": "x"}, "all", None)] == [
        True, False, False, False]
    assert not a2a.to_all({"message": "x"}) and not a2a.to_all({"props": "all"})
    assert a2a.annotate("IDLE · done") == {"v": 1, "kind": "idle", "intent": "fyi"}
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
    a CANCEL by the coordinator (the roster's crown holder) drops the task."""
    posts = [
        P("t", LEIT, "TASK · x · @agent-a", at=0),
        P("1", A, "ACK", root="t", at=1),
        P("2", LEIT, "DONE #46 merged", root="t", at=2),
        P("u", LEIT, "TASK · y", at=3),
        P("3", B, "👑 CANCEL – not needed", root="u", at=4),
    ]
    crown = {"agent-b"}
    assert [(r["task"], r["status"])
            for r in tasks.build(posts, NAMES, now_ms=NOW, coordinators=crown)] == [("t", "done")]
    posts.append(P("as", LEIT, "ASSIGNMENTS · x done", at=5))
    assert tasks.build(posts, NAMES, now_ms=NOW, coordinators=crown) == []
    assert tasks.build(posts[:3], NAMES, now_ms=NOW + 2 * 86_400_000) == []


def test_the_mention_follows_the_registrar_prefix(monkeypatch):
    """RT-6.14 step 3, arc42 §7.1: with prefix bot- a @bot-… mention names the
    assignee, so another agent's ACK does not take the task."""
    monkeypatch.setenv("MM_AGENT_PREFIX", "bot-")
    names = {LEIT: "bot-leit", A: "bot-a", B: "bot-b"}
    posts = [
        P("t", LEIT, "TASK · docs for Cursor · @bot-a", at=0),
        P("1", B, "ACK", root="t", at=1),
        P("2", A, "ACK", root="t", at=2),
    ]
    [row] = tasks.build(posts, names, now_ms=NOW)
    assert row["agent"] == "bot-a"


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
    rows = [RosterEntry("agent-b", "working", coordinator=True, crown=True),
            RosterEntry("agent-a", "listening")]
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


def _Client(posts):  # noqa: N802
    return FakeChat(uid=ME, posts=posts, names=NAMES)


def _note(**a2a_props):
    return {"props": {"a2a": {"v": 1, **a2a_props}}}


def test_pr_and_branch_from_props_else_text():
    """RT-6.14 step 3: PR and branch come from props.a2a; a post without
    (older, a person's) is read from its text; props win over the text."""
    root = P("t", LEIT, "TASK · x · @agent-a", at=0)
    props_only = [root, P("1", A, "ACK · on it", root="t", at=1,
                          **_note(kind="ack", pr=61, branch="feat/x"))]
    [row] = tasks.build(props_only, NAMES, now_ms=NOW)
    assert (row["pr"], row["branch"]) == ("#61", "feat/x")
    text_only = [root, P("1", A, "RESULT PR #62 on branch feat/y", root="t", at=1)]
    [row] = tasks.build(text_only, NAMES, now_ms=NOW)
    assert (row["pr"], row["branch"]) == ("#62", "feat/y")
    both = [root, P("1", A, "RESULT PR #62 on branch feat/y", root="t", at=1,
                    **_note(kind="result", pr=63, branch="feat/z"))]
    [row] = tasks.build(both, NAMES, now_ms=NOW)
    assert (row["pr"], row["branch"]) == ("#63", "feat/z")


def test_the_crown_to_close_comes_from_roster_or_props_not_text():
    """RT-6.14 step 3: a CANCEL by someone other than the task's author
    counts from the roster's crown holder, or from an agent's post the server
    wrote while it held the crown; a 👑 typed in the text counts for nothing."""
    root = P("t", LEIT, "TASK · x", at=0)

    def rows(close, **kw):
        return tasks.build([root, close], NAMES, now_ms=NOW, **kw)

    typed = P("1", B, "👑 CANCEL – not needed", root="t", at=1)
    assert [r["status"] for r in rows(typed)] == ["open"]
    assert rows(typed, coordinators={"agent-b"}) == []
    assert rows(typed, coordinators={"agent-a"}) != []
    crowned = P("1", B, "👑 CANCEL", root="t", at=1, **_note(kind="cancel", crown=True))
    assert rows(crowned) == []  # crowned when it posted; the crown moved on since
    by_person = P("1", HUMAN, "CANCEL", root="t", at=1, **_note(kind="cancel", crown=True))
    assert [r["status"] for r in rows(by_person)] == ["open"]


def test_a_typed_crown_from_before_the_cutover_still_closes():
    """RT-6.14 step 3: an agent's 👑 CANCEL from before 0.46.0 (no crown props)
    keeps its task closed; a person's, or one after the cutover, does not."""
    cut = tasks.LEGACY_CROWN_BEFORE_MS
    root = P("t", LEIT, "TASK · x", create_at=cut - 10)

    def rows(uid, message, at):
        close = P("1", uid, message, root="t", create_at=at)
        return tasks.build([root, close], NAMES, now_ms=cut + 1000)

    assert rows(B, "👑 CANCEL – not needed", cut - 1) == []
    assert [r["status"] for r in rows(B, "👑 CANCEL", cut)] == ["open"]
    assert [r["status"] for r in rows(HUMAN, "👑 CANCEL", cut - 1)] == ["open"]


def test_annotate_names_pr_branch_and_crown():
    """RT-6.14 step 2: the server writes what the text names into props."""
    assert a2a.annotate("RESULT PR #57 on branch feat/a", crown=True) == {
        "v": 1, "kind": "result", "state": "input-required", "intent": "fyi",
        "pr": 57, "branch": "feat/a", "crown": True}
    assert a2a.annotate("see https://github.com/o/r/pull/9") == {"v": 1, "pr": 9}
    assert a2a.annotate("thanks", crown=False) is None


T26 = "t" * 26  # a valid post id: the server refuses others (#81)


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
    await server.reply_in_thread(root_id=T26, message="ACK – on it")
    await server.post_message(message="anyone seen #46?")
    assert c.posted[0]["props"] == {"a2a": {"v": 1, "kind": "ack", "state": "working",
                                            "intent": "fyi"}}
    assert c.posted[1]["props"] is None


@pytest.mark.asyncio
async def test_posts_carry_pr_branch_and_crown(bus):
    """RT-6.14 step 2: post_message and reply_in_thread write pr, branch and,
    while the author holds the crown, crown into props.a2a."""
    c = bus([], crown=True)
    await server.reply_in_thread(root_id=T26, message="RESULT PR #57 on branch feat/a")
    await server.post_message(message="CANCEL · not needed")
    assert c.posted[0]["props"]["a2a"] == {
        "v": 1, "kind": "result", "state": "input-required", "intent": "fyi",
        "pr": 57, "branch": "feat/a", "crown": True}
    assert c.posted[1]["props"]["a2a"]["crown"] is True


@pytest.mark.asyncio
async def test_coordinator_reads_tasks_and_asks_not_history(bus):
    """RT-6.14 step 5: one compact row per task instead of the channel."""
    bus(_leitstand_thread() + [P("q", A, "🙋 @dembaca — **Merge #57?**", at=40)])
    out = json.loads(await server.coordinator("tasks"))
    assert [r["task"] for r in out["tasks"]] == ["t1"] and out["asks"][0]["ask"] == "q"
    assert len(json.dumps(out)) < len(json.dumps([p.json() for p in _leitstand_thread()])) / 2


@pytest.mark.asyncio
async def test_coordinator_roster(bus, monkeypatch):
    """RT-6.14 step 4: lease rows from lease.roster (RT-6.6), cards from HELLO."""
    bus(_leitstand_thread())

    async def rows(_client):
        return [RosterEntry("agent-me", "working")]
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

    async def broken(*a, **kw):
        raise RuntimeError("HTTP 500")
    c.posts_after = broken
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


_SKILL = Path(server.__file__).resolve().parents[3] / "skills" / "mattermost-ai-agent-bus"
# Per-load budgets, about 10 % over the 0.57.0 size (in brackets): SKILL.md is
# the core every agent loads; the files next to it are read only when they apply.
_SKILL_BUDGET = {
    "SKILL.md": 16_500,                 # (16279) was 22,144 before the runtime split
    "runtime-claude-code.md": 4_850,    # (4417)
    "runtime-no-hooks.md": 2_600,       # (2381)
    "self-registration.md": 2_450,      # (2234)
}


@pytest.mark.parametrize("name", sorted(_SKILL_BUDGET))
def test_skill_file_budget(name):
    """RT-6.9, RT-6.14 acceptance: the per-load text does not grow. SKILL.md
    holds what every agent needs on every runtime; runtime-specific text lives
    in the file for that runtime, read on top, once."""
    assert len((_SKILL / name).read_text()) <= _SKILL_BUDGET[name]


def test_skill_pointers_name_existing_files():
    """RT-6.9: every file SKILL.md points to lies next to it, and both
    runtime files are pointed to, so an agent on any runtime finds its part."""
    import re

    text = (_SKILL / "SKILL.md").read_text()
    linked = set(re.findall(r"\]\(([\w.-]+\.md)\)", text))
    assert {"runtime-claude-code.md", "runtime-no-hooks.md", "coordinator.md", "self-registration.md"} <= linked
    assert [f for f in linked if not (_SKILL / f).is_file()] == []
    for f in ("runtime-claude-code.md", "runtime-no-hooks.md"):  # read on top of the core
        assert "[SKILL.md](SKILL.md)" in (_SKILL / f).read_text()


def test_static_budget():
    """RT-6.14 acceptance, RT-6.9: coordinator.md (the crown holder, per
    load) stays within its 0.30.0 size; the coordinator tool doc grows by its
    two new actions only. SKILL.md and its files have their budgets above; the
    per-turn text, the MCP instructions, has its own budget below."""
    skill = _SKILL
    assert len((skill / "coordinator.md").read_text()) <= 5_550  # + RT-6.16: posts not trusted; + RT-6.19: stall; + RT-6.21: departed; + RT-6.6: crown per home
    # cleandoc: Python 3.13 strips a docstring's indentation, older ones don't.
    assert len(inspect.cleandoc(server.coordinator.__doc__)) <= 425 + 90


# The MCP instructions per variant: (host-managed, hosted, unavailable) ×
# (no push, push, push + permission relay), each limit about 10 % over its
# 0.55.0 size (in brackets). The unavailable reason is at its longest, 300.
_INSTRUCTION_BUDGET = {
    "self-registering": (2050, 2350, 2500),   # (1860, 2128, 2267)
    "host-managed": (1850, 2150, 2300),       # (1680, 1948, 2087)
    "hosted": (2000, 2300, 2450),             # (1806, 2074, 2213)
    "unavailable": (2200, 2500, 2650),        # (1987, 2255, 2394)
}


@pytest.mark.parametrize("variant", sorted(_INSTRUCTION_BUDGET))
@pytest.mark.parametrize("mode", [0, 1, 2], ids=["no-push", "push", "push+relay"])
def test_instructions_budget(monkeypatch, variant, mode):
    """RT-6.9: the MCP instructions are the per-turn text, in the context of
    every request whether the skill is loaded or not: no variant grows past
    its budget without the budget changing here, with a reason."""
    monkeypatch.setattr(server, "HOST_MANAGED", variant in ("host-managed", "hosted"))
    monkeypatch.setattr(server, "HOSTED", variant == "hosted")
    monkeypatch.setattr(server, "UNAVAILABLE", "x" * 300 if variant == "unavailable" else "")
    monkeypatch.setattr(server, "PUSH", mode >= 1)
    monkeypatch.setattr(server, "RELAY", mode == 2)
    assert len(server._instructions()) <= _INSTRUCTION_BUDGET[variant][mode]


@pytest.mark.parametrize("message,kind,out", [
    ("on it", "ack", "ACK · on it"), ("ACK – on it", "ACK", "ACK – on it"),
    ("**DONE** #46 merged", "done", "**DONE** #46 merged"), ("PR #57", "RESULT", "PR #57"),
    ("PR #57 merged", "DONE", "DONE · PR #57 merged"), ("", "IDLE", "IDLE"),
    ("No worries", "ACK", "ACK · No worries"), ("on it", "", "on it"),
])
def test_kind_leads_the_text(message, kind, out):
    """RT-6.14 step 2: kind is put first unless the text starts with it, so
    text and props say the same; any case."""
    assert a2a.lead(message, kind) == out
    assert a2a.word(out) == (kind.upper() or None)


@pytest.mark.parametrize("message,kind,error", [
    ("on it", "OK", "kind must be one of TASK, DELEGATE, ACK"),
    ("DONE · merged", "ACK", "kind is ACK but the message starts with DONE"),
])
def test_kind_is_a_bus_word_and_agrees_with_the_text(message, kind, error):
    """RT-6.14 step 2: an unknown kind, or one the first word contradicts, is refused."""
    with pytest.raises(ValueError, match=error):
        a2a.lead(message, kind)


def test_intent_follows_the_kind():
    """RT-6.14 step 2: action, reply or fyi from the kind; nothing for a plain
    post. Read from props only, never from the text."""
    by = {w: a2a.intent(w) for w in a2a.WORDS}
    assert {w for w, i in by.items() if i == a2a.ACTION} == {"TASK", "DELEGATE", "HANDOVER", "CANCEL"}
    assert {w for w, i in by.items() if i == a2a.REPLY} == {"BLOCKED", "NO"}
    assert a2a.intent(a2a.ASK) == a2a.intent(a2a.PERMISSION) == a2a.REPLY
    assert a2a.annotate(kind=a2a.ASK) == {"v": 1, "kind": "ask", "state": "input-required",
                                          "intent": "reply"}
    assert a2a.intent("plain") is None and a2a.annotate("plain") is None
    assert a2a.intent_of({"props": {"a2a": {"intent": "fyi"}}}) == a2a.FYI
    for odd in ({"a2a": {"intent": "later"}}, {"a2a": "fyi"}, "fyi", None):
        assert a2a.intent_of({"message": "DONE", "props": odd}) is None


@pytest.mark.asyncio
async def test_kind_is_written_to_text_and_props(bus):
    """RT-6.14 step 2: post_message and reply_in_thread take kind; a refused
    kind posts nothing."""
    c = bus([])
    await server.reply_in_thread(root_id=T26, message="merged", kind="done")
    await server.post_message(message="relay · @agent-a", kind="TASK")
    assert [p["message"] for p in c.posted] == ["DONE · merged", "TASK · relay · @agent-a"]
    assert [p["props"]["a2a"]["intent"] for p in c.posted] == ["fyi", "action"]
    out = json.loads(await server.reply_in_thread(root_id=T26, message="DONE", kind="ACK"))
    assert "kind is ACK" in out["refused"] and "ACK" in out["kinds"]
    assert len(c.posted) == 2


@pytest.mark.asyncio
async def test_a_late_claim_learns_who_has_the_task(bus, monkeypatch):
    """RT-6.14 step 3: the first ACK, PROGRESS or RESULT in thread order takes
    the task; a later one gets taken_by back. Once the bot holds a task, its
    next claims there read no thread."""
    monkeypatch.setattr(server, "_my_tasks", set())
    root = P(T26, LEIT, "TASK · relay", at=0)
    c = bus([root, P("a1", A, "ACK", root=T26, at=10), P("m1", ME, "ACK", root=T26, at=20)])
    out = json.loads(await server.reply_in_thread(root_id=T26, message="on it", kind="ACK"))
    assert out["taken_by"] == "agent-a"
    out = json.loads(await server.reply_in_thread(root_id=T26, message="BLOCKED on #44"))
    assert "taken_by" not in out  # no claim: no thread read
    c = bus([root, P("m1", ME, "ACK", root=T26, at=10), P("a1", A, "ACK", root=T26, at=20)])
    reads = []
    orig = c.thread

    async def thread(r):
        reads.append(r)
        return await orig(r)
    c.thread = thread
    for _ in range(2):
        out = json.loads(await server.reply_in_thread(root_id=T26, message="PROGRESS · half"))
        assert "taken_by" not in out
    assert reads == [T26]


@pytest.mark.asyncio
async def test_an_fyi_reply_wakes_the_threads_starter(bus, monkeypatch):
    """RT-6.14, RT-6.12 step 3: the delegator waits for the RESULT in its TASK
    thread, so it is pushed; an agent that only posted in a thread gets it in
    the digest."""
    monkeypatch.setattr(server, "_my_roots", set())
    monkeypatch.setenv("MM_BOT_USER_ID", ME)
    bus([P("t", ME, "TASK · x"), P("a1", A, "ACK", root="t"),
         P("t2", LEIT, "TASK · y"), P("m1", ME, "ACK", root="t2")])

    def item(root):
        return {"kind": "watched_channel", "from_agent": True, "intent": "fyi",
                "post": {"id": "n", "root_id": root, "message": "RESULT · PR #5"}}
    assert await server._push_addressed(item("t"))
    assert not await server._push_addressed(item("t2"))
    assert server._my_roots == {"t"}


def test_an_ask_an_agent_resolved_is_not_open():
    """RT-6.11, RT-6.19: resolve_ask's ✅ closes the ask, also one of an earlier
    session of another bot; a ✅ by a human not asked or trusted does not."""
    posts = [P("q1", A, "🙋 @dembaca — **Merge #44?**", at=0,
               metadata={"reactions": [{"emoji_name": "white_check_mark", "user_id": B}]}),
             P("q2", A, "🙋 @dembaca — **Rename it?**", at=1,
               metadata={"reactions": [{"emoji_name": "white_check_mark", "user_id": "x" * 26}]})]
    assert [a["ask"] for a in tasks.open_asks(posts, {**NAMES, "x" * 26: "someone"})] == ["q2"]

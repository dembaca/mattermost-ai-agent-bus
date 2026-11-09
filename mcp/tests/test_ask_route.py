"""Ask routing modes (RT-6.25, ADR-005 phase 1): the server's class floor, a
mode per home that only a trusted human's post in the home switches, a
question to the home's 👑, the fallback to the human, and what never changes:
nothing an agent posts approves, and an approval never goes to an agent."""

import json
import stat

import pytest

from mattermost_ai_agent_bus import askroute, asks, channel, lease, remind, server, session_file, stall, tasks
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent, WebSocketInbox

from mattermost_ai_agent_bus.model import ChannelUser

from fakes import WORK, FakeChat

BOT = "b" * 26      # the asking agent
HUMAN = "h" * 26    # its trusted human, dembaca
OTHER = "o" * 26    # a human nobody trusts
COORD = "k" * 26    # the home's 👑
AGENT = "g" * 26    # another agent
ASK = "a" * 26
SWITCH = "s" * 26
WAIT = 15 * 60

MINE = "for @dembaca · trusts @dembaca · via agent-registrar"


class _Client(FakeChat):
    """FakeChat with the adapter's add_reaction the fallback calls."""

    def __init__(self, **kw):
        super().__init__(uid=BOT, username="agent-worker", trusted={HUMAN: "dembaca"},
                         names={HUMAN: "dembaca", OTHER: "visitor", COORD: "agent-coord",
                                AGENT: "agent-x", BOT: "agent-worker"},
                         agents={COORD, AGENT, BOT}, post_id=ASK, **kw)
        self.reacted = []
        self.descriptions |= {BOT: MINE, COORD: MINE}  # the 👑 is a peer (ADR-001)
        self.bots["agent-coord"] = ChannelUser(COORD, "agent-coord", create_at=1)

    async def add_reaction(self, post_id, emoji):
        self.reacted.append((post_id, emoji))


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for v in ("MM_RESPONSIBLE_HUMAN", "MM_AGENT_KIND", "MM_ASK_ROUTE", "MM_ASK_ROUTE_WAIT_MIN"):
        monkeypatch.delenv(v, raising=False)
    askroute._looked.clear()
    askroute._warned.clear()
    monkeypatch.setattr(lease, "crown_held", lambda uid: False)


def _switch(text="ask-mode coordinator", *, user=HUMAN, root="", channel_id=WORK, at=1000,
            pid=SWITCH, **raw):
    return post_of({"id": pid, "message": text, "user_id": user, "channel_id": channel_id,
                    "root_id": root, "create_at": at, **raw})


def _crown(name="agent-coord"):
    async def holder(client, channel_id):
        return name
    return holder


def _coordinator_mode(monkeypatch, c, holder="agent-coord"):
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setenv("MM_ASK_ROUTE", "coordinator")
    monkeypatch.setattr(lease, "coordinator_of", _crown(holder))


def title_of(posted):
    return posted["props"]["attachments"][0]["title"]


def _fields(posted):
    return {f["title"]: f["value"] for f in posted["props"]["attachments"][0]["fields"]}


# --- step 1: the floor ------------------------------------------------------------------

@pytest.mark.parametrize("text,named", [
    ("Merge PR #12 now?", "merge"),
    ("Deploy 02dadca to prod?", "deploy"),
    ("Roll out the fix on hogan?", "deploy"),
    ("Tag v0.67.0 and release it?", "release"),
    ("Install the new runner on hogan?", "install"),
    ("Delete the stale branch feat/x?", "delete"),
    ("Remove the old VM 399?", "delete"),
    ("Force-push feat/x?", "push"),
    ("Open an issue upstream for the flaky test?", "external write"),
    ("Comment on PR #12 with the benchmark?", "external write"),
    ("Send the customer the report by e-mail?", "external write"),
    ("Buy two more runners for 40 EUR a month?", "spending"),
    ("Soll ich den alten Branch löschen?", "delete"),
])
def test_the_floor_names_an_action_with_effect(text, named):
    """RT-6.25 step 1: the one list of actions with effect."""
    assert askroute.floor(text) == named


@pytest.mark.parametrize("text", [
    "Approach A or B for the cache?",
    "Which of the open PRs first?",
    "Who takes the review of PR #5?",
    "Write the tests for PR #12 first, or the docs?",
    "Is agent-x still on the lease bug?",
])
def test_a_question_of_content_names_none(text):
    assert askroute.floor(text) is None


def test_the_floor_overrides_a_question_and_unclear_is_an_approval():
    """RT-6.25 step 1: an approval declared as a question is lifted; a kind
    other than the two is an approval."""
    assert askroute.classify("question", "Merge PR #12?") == ("approval", "merge")
    assert askroute.classify("question", "Go ahead?", "Deploy 02dadca to prod") == ("approval", "deploy")
    assert askroute.classify("question", "Approach A or B?") == ("question", None)
    assert askroute.classify("approval", "Approach A or B?") == ("approval", None)
    assert askroute.classify("maybe", "Approach A or B?") == ("approval", None)
    assert askroute.classify("", "Approach A or B?") == ("approval", None)


# --- step 2: the mode -------------------------------------------------------------------

def test_the_default_mode_comes_from_the_environment(monkeypatch, caplog):
    assert askroute.env_mode() == "direct"
    monkeypatch.setenv("MM_ASK_ROUTE", "Coordinator")
    assert askroute.env_mode() == "coordinator"
    monkeypatch.setenv("MM_ASK_ROUTE", "delegated")
    with caplog.at_level("WARNING"):
        assert askroute.env_mode() == "direct"
    assert "delegated" in caplog.text
    assert askroute.wait_sec() == WAIT
    monkeypatch.setenv("MM_ASK_ROUTE_WAIT_MIN", "5")
    assert askroute.wait_sec() == 300


@pytest.mark.parametrize("text,mode", [
    ("ask-mode coordinator", "coordinator"), ("ASK-MODE Direct", "direct"),
    ("`ask-mode coordinator`", "coordinator"), ("ask-mode coordinator\nwe try it today", "coordinator"),
    ("ask-mode delegated", None), ("we could set ask-mode coordinator", None),
    ("ask-mode coordinator please", None), ("", None),
])
def test_the_command_form(text, mode):
    assert askroute.command(text) == mode


@pytest.mark.asyncio
@pytest.mark.parametrize("post", [
    _switch(user=AGENT), _switch(user=COORD), _switch(user=OTHER), _switch(root="r" * 26),
    _switch(channel_id="x" * 26), _switch(props={"from_webhook": "true"}),
], ids=["agent", "the crown", "not trusted", "a reply", "another channel", "a webhook"])
async def test_only_a_trusted_humans_top_level_post_in_the_home_switches(post):
    """RT-6.25 step 2, security: nobody else's post switches anything."""
    c = _Client()
    assert await askroute.observe(c, post, BOT) is False
    assert await askroute.mode(c, BOT) == ("direct", "env")


@pytest.mark.asyncio
async def test_an_agent_listed_as_trusted_still_switches_nothing():
    c = _Client()
    c.trusted[AGENT] = "agent-x"
    assert await askroute.observe(c, _switch(user=AGENT), BOT) is False


@pytest.mark.asyncio
async def test_a_trusted_switch_is_kept_and_the_crown_acknowledges_it_once(monkeypatch):
    c = _Client()
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    post = _switch()
    assert await askroute.observe(c, post, BOT) is True
    assert await askroute.mode(c, BOT) == ("coordinator", SWITCH)
    kept = session_file.state_dir() / f"askmode-{BOT}.env"
    assert stat.S_IMODE(kept.stat().st_mode) == 0o600
    assert f"POST={SWITCH}" in kept.read_text()
    ack = c.posted[0]
    assert len(c.posted) == 1 and ack["root_id"] == SWITCH
    assert ack["message"].startswith("\U0001F451 ask-mode coordinator, set by dembaca:")
    assert "@" not in ack["message"] and ack["props"]["a2a"]["kind"] == "ask_mode"
    assert ack["props"]["a2a"]["intent"] == "fyi"
    # The backfill and the live stream overlap: no second acknowledgement.
    assert await askroute.observe(c, post, BOT) is True and len(c.posted) == 1


@pytest.mark.asyncio
async def test_other_agents_stay_quiet_and_a_thread_with_an_acknowledgement_gets_none(monkeypatch):
    c = _Client()
    assert await askroute.observe(c, _switch(), BOT) is True
    assert c.posted == []  # no crown, no word
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    done = post_of({"id": "d" * 26, "root_id": SWITCH, "user_id": COORD, "message": "ask-mode …",
                    "props": {"a2a": {"kind": "ask_mode"}}})
    c.posts = [_switch(), done]
    assert await askroute.observe(c, _switch("ask-mode direct", at=2000, pid="n" * 26), BOT) is True
    assert len(c.posted) == 1  # its own thread had none
    assert await askroute.observe(c, _switch("ask-mode coordinator", at=3000), BOT) is True
    assert len(c.posted) == 1  # this thread has one already


@pytest.mark.asyncio
async def test_the_newest_switch_wins():
    c = _Client()
    await askroute.observe(c, _switch(at=2000, pid="n" * 26), BOT)
    await askroute.observe(c, _switch("ask-mode direct", at=1000), BOT)  # older, seen later
    assert await askroute.mode(c, BOT) == ("coordinator", "n" * 26)
    await askroute.observe(c, _switch("ask-mode direct", at=3000, pid="m" * 26), BOT)
    assert await askroute.mode(c, BOT) == ("direct", "m" * 26)


@pytest.mark.asyncio
async def test_without_a_kept_switch_the_home_is_read_once_for_the_newest():
    """RT-6.25 step 2: the start read, bounded; an untrusted post is skipped."""
    c = _Client(posts=[_switch("ask-mode direct", user=OTHER, at=3000, pid="o" * 26),
                       _switch(at=2000), _switch("ask-mode direct", at=1000, pid="d" * 26)])
    assert await askroute.mode(c, BOT) == ("coordinator", SWITCH)
    assert c.calls == [("posts", askroute.READ_POSTS, None)]
    c.posts = []
    assert await askroute.mode(c, BOT) == ("coordinator", SWITCH)
    assert len(c.calls) == 1  # kept: not read again


@pytest.mark.asyncio
async def test_an_unreadable_home_leaves_the_default(monkeypatch):
    c = _Client()

    async def down(*args, **kwargs):
        raise RuntimeError("down")
    monkeypatch.setattr(c, "channel_posts", down)
    monkeypatch.setenv("MM_ASK_ROUTE", "coordinator")
    assert await askroute.mode(c, BOT) == ("coordinator", "env")


def _posted(pid, user, text="ask-mode coordinator"):
    return {"event": "posted", "data": {"channel_type": "O", "post": json.dumps({
        "id": pid, "message": text, "user_id": user, "channel_id": WORK, "create_at": 1000})}}


@pytest.mark.asyncio
async def test_the_inbox_hands_a_switch_to_no_agent():
    """RT-6.25 step 2: the switch is the server's; anyone else's post with the
    same words goes on as any post."""
    c = _Client()
    inbox = WebSocketInbox(client=c, watched_channel_ids={WORK})
    inbox._bot_user_id, inbox._bot_username = BOT, "agent-worker"
    await inbox._on_post(_posted(SWITCH, HUMAN))
    assert inbox._queue.empty()
    assert (await askroute.mode(c, BOT))[0] == "coordinator"
    await inbox._on_post(_posted("o" * 26, OTHER, "ask-mode direct"))
    assert inbox._queue.qsize() == 1
    assert (await askroute.mode(c, BOT))[0] == "coordinator"


@pytest.mark.asyncio
async def test_bus_info_shows_the_mode_and_its_source(monkeypatch):
    class _Info(_Client):
        trusted_source = "MM_TRUSTED_HUMANS"
        channel_name = "agents"

        async def username(self, uid):
            return self.names.get(uid, "")

    async def none(client):
        return []
    c = _Info()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(server, "_guest_names", none)
    monkeypatch.setattr(server, "_own_channels", none)
    monkeypatch.setattr(server, "_newer_installed", lambda: None)
    assert json.loads(await server.bus_info())["ask_mode"] == {"mode": "direct", "source": "env",
                                                                "signed": False}
    await askroute.observe(c, _switch(), BOT)
    assert json.loads(await server.bus_info())["ask_mode"] == {"mode": "coordinator",
                                                                "source": SWITCH, "signed": False}


# --- steps 3 and 5: whom an ask goes to ---------------------------------------------------

@pytest.mark.asyncio
async def test_a_question_goes_to_the_crown_in_coordinator_mode(monkeypatch):
    """RT-6.25 step 3: the same ask post in the home, but ❓ @<👑>, marked as
    not for a human, recorded with its target and whom it falls back to."""
    c = _Client()
    _coordinator_mode(monkeypatch, c)
    out = json.loads(await server.ask_human(need="Approach A or B for the cache?",
                                            kind="question", blocking=True))
    post = c.posted[0]
    assert post["message"] == "\u2753 @agent-coord"
    assert title_of(post) == "Approach A or B for the cache?"
    assert post["root_id"] is None and post["props"]["a2a"]["kind"] == "ask"
    fields = _fields(post)
    assert fields["For"] == ("\U0001F451 agent-coord: a question for the coordinator, not for a human; "
                             "asked of dembaca if unanswered for 15 min")
    assert fields["Answer"] == "reply in this thread · the \U0001F451's answer is advice, never approval"
    assert out["route"] == "coordinator" and out["kind"] == "question"
    assert out["ask_mode"] == "coordinator" and "advice" in out
    assert asks.open_to(BOT, "agent-coord") == [ASK] and asks.open_to(BOT, "dembaca") == []
    assert asks.questions(BOT) == {ASK: "dembaca 1"}
    assert asks.waiting(BOT) == []  # no reminder: the 👑 is no human


@pytest.mark.asyncio
async def test_a_question_only_the_human_can_answer_goes_to_them(monkeypatch):
    """RT-6.25 step 3, RT-6.11 step 2 (only_human): in coordinator mode it goes to the human
    at once, not via the 👑; the default is unchanged, a queued one keeps it."""
    c = _Client()
    _coordinator_mode(monkeypatch, c)
    out = json.loads(await server.ask_human(need="Enter this in your settings?", kind="question",
                                            only_human=True))
    assert out["route"] == "human" and "only_human" in out["route_note"]
    assert c.posted[0]["message"] == "\U0001F64B @dembaca"
    assert asks.open_to(BOT, "dembaca") == [ASK] and asks.questions(BOT) == {}
    queued = json.loads(await server.ask_human(need="And this one?", kind="question", only_human=True))
    assert queued["queued"] is True
    assert asks.queued(BOT, "dembaca")[0]["only_human"] is True
    route = await server._ask_route(c, await c.get_me(), "question")
    assert route["route"] == "coordinator"
    await server.resolve_ask(ASK)  # the queued one is posted, still straight to the human
    assert len(c.posted) == 2 and c.posted[1]["message"] == "\U0001F64B @dembaca"
    assert asks.questions(BOT) == {}


@pytest.mark.asyncio
async def test_a_switch_post_in_the_home_routes_without_the_environment(monkeypatch):
    c = _Client(posts=[_switch()])
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(lease, "coordinator_of", _crown())
    out = json.loads(await server.ask_human(need="A or B?", kind="question"))
    assert out["route"] == "coordinator" and c.posted[0]["message"].startswith("\u2753 @agent-coord")


@pytest.mark.asyncio
async def test_an_approval_never_goes_to_an_agent(monkeypatch):
    """RT-6.25, security: approvals go to the human in every mode."""
    c = _Client()
    _coordinator_mode(monkeypatch, c)
    out = json.loads(await server.ask_human(need="Ship 02dadca to hogan?"))
    assert c.posted[0]["message"] == "\U0001F64B @dembaca"
    assert title_of(c.posted[0]) == "Ship 02dadca to hogan?"
    assert out["kind"] == "approval" and out["route"] == "human" and "advice" not in out
    assert asks.questions(BOT) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("need,decision,named", [
    ("Merge PR #12 now?", "", "merge"),
    ("Go ahead?", "Deploy 02dadca to prod", "deploy"),
])
async def test_an_approval_declared_as_a_question_is_lifted_by_the_floor(monkeypatch, need, decision, named):
    """RT-6.25 step 1, security: the floor overrides the worker."""
    c = _Client()
    _coordinator_mode(monkeypatch, c)
    out = json.loads(await server.ask_human(need=need, decision=decision, kind="question"))
    assert c.posted[0]["message"].startswith("\U0001F64B @dembaca")
    assert (out["kind"], out["kind_declared"], out["floor"]) == ("approval", "question", named)
    assert out["route"] == "human"


@pytest.mark.asyncio
@pytest.mark.parametrize("holder", ["", "agent-worker"], ids=["no crown", "only the asker"])
async def test_without_a_reachable_crown_the_human_is_asked_at_once(monkeypatch, holder):
    """RT-6.25 step 5."""
    c = _Client()
    _coordinator_mode(monkeypatch, c, holder)
    out = json.loads(await server.ask_human(need="A or B?", kind="question"))
    assert c.posted[0]["message"] == "\U0001F64B @dembaca"
    assert title_of(c.posted[0]) == "A or B?"
    assert out["route"] == "human" and "no \U0001F451 reachable" in out["route_note"]


@pytest.mark.asyncio
async def test_the_crowns_own_question_goes_to_the_human(monkeypatch):
    c = _Client()
    _coordinator_mode(monkeypatch, c, "agent-worker")
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    out = json.loads(await server.ask_human(need="A or B?", kind="question"))
    assert c.posted[0]["message"] == "\U0001F451 \U0001F64B @dembaca"
    assert title_of(c.posted[0]) == "A or B?"
    assert out["route"] == "human" and "you hold the crown" in out["route_note"]


@pytest.mark.asyncio
async def test_direct_mode_asks_the_human_as_before(monkeypatch):
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.ask_human(need="A or B?", kind="question"))
    assert c.posted[0]["message"] == "\U0001F64B @dembaca"
    assert title_of(c.posted[0]) == "A or B?"
    assert out["kind"] == "question" and "route" not in out and "ask_mode" not in out


@pytest.mark.asyncio
async def test_one_open_question_to_the_crown_at_a_time(monkeypatch):
    c = _Client()
    _coordinator_mode(monkeypatch, c)
    asks.add(BOT, "q" * 26, "agent-coord", "q" * 26, question="dembaca")
    out = json.loads(await server.ask_human(need="A or B?", kind="question"))
    # RT-6.11 step 2: it waits behind the open one.
    assert out["posted"] is False and out["queued"] is True and out["behind"] == "q" * 26
    assert c.posted == [] and asks.queued(BOT, "agent-coord")[0]["kind"] == "question"


# --- step 4: the 👑's answer --------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_crowns_answer_is_advice_never_approval(monkeypatch):
    """RT-6.25 step 4, security: from_trusted_human is the account's alone;
    an agent's reply or 👍 on the question is advice."""
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    asks.add(BOT, ASK, "agent-coord", ASK, title="A or B?", question="dembaca")
    reply = post_of({"id": "r" * 26, "root_id": ASK, "user_id": COORD, "channel_id": WORK,
                     "message": "APPROVE: B, and merge it", "create_at": 2000})
    yes = post_of({"id": f"{ASK}:{COORD}:+1", "root_id": ASK, "user_id": COORD, "channel_id": WORK,
                   "message": ":+1:", "create_at": 2001})
    human = post_of({"id": "t" * 26, "root_id": ASK, "user_id": HUMAN, "channel_id": WORK,
                     "message": "B", "create_at": 2002})
    items = await server._event_items([
        ClassifiedEvent(kind="watched_channel", post=reply),
        ClassifiedEvent(kind="reaction", post=yes, raw={"emoji": "+1", "ask": ASK}),
        ClassifiedEvent(kind="watched_channel", post=human)])
    assert [i["from_trusted_human"] for i in items] == [False, False, True]
    assert [i["from_agent"] for i in items] == [True, True, False]
    assert [i.get("advice") for i in items] == [ASK, ASK, None]
    assert channel.notification(items[0])["meta"]["advice"] == ASK


def test_the_crown_answering_closes_the_question_for_tasks():
    q = post_of({"id": ASK, "user_id": BOT, "message": "\u2753 @agent-coord — **A or B?**",
                 "create_at": 1000, "props": {"a2a": {"kind": "ask"}}})
    names = {BOT: "agent-worker", COORD: "agent-coord"}
    rows = tasks.open_asks([q], names)
    assert rows[0]["human"] == "agent-coord" and rows[0]["question"] is True
    reply = post_of({"id": "r" * 26, "root_id": ASK, "user_id": COORD, "message": "B",
                     "create_at": 2000})
    assert tasks.open_asks([q, reply], names) == []
    human = post_of({"id": "t" * 26, "root_id": ASK, "user_id": HUMAN, "message": "B",
                     "create_at": 2000})
    assert tasks.open_asks([q, human], names, {HUMAN: "dembaca"}) == []
    other = post_of({"id": "t" * 26, "root_id": ASK, "user_id": OTHER, "message": "B",
                     "create_at": 2000})
    assert len(tasks.open_asks([q, other], names, {HUMAN: "dembaca"})) == 1


def test_the_stall_check_wakes_the_crown_for_a_question():
    """RT-6.25 step 7: listed as a question for the 👑, not as a reminded ask."""
    now_ms = 10_000_000
    q = post_of({"id": ASK, "user_id": BOT, "message": "\u2753 @agent-coord — **A or B?**",
                 "create_at": now_ms - 60_000, "props": {"a2a": {"kind": "ask"}}})
    found = stall.items([q], {BOT: "agent-worker"}, ["agent"], [], {"agent-worker"}, now_ms,
                        me="agent-coord")
    assert [(i["what"], i["next"].split(" ")[0]) for i in found] == [("question", "agent-coord")]
    assert "reminded" not in found[0]


# --- step 6: no answer in time ----------------------------------------------------------

def _asked(c, *, title="A or B?", blocking=True):
    c.posts = [post_of({"id": ASK, "user_id": BOT, "channel_id": WORK, "create_at": 1000,
                        "message": f"❓ @agent-coord — **{title}**"})]
    asks.add(BOT, ASK, "agent-coord", ASK, title=title, question="dembaca", blocking=blocking)
    return asks.questions_open(BOT)[0][1]


def _fallback(monkeypatch):
    called = []

    async def ask(client, question, human, coord):
        called.append((question, human, coord))
        asks.resolve(BOT, question)
        return "f" * 26
    monkeypatch.setattr(askroute, "fallback", ask)
    return called


@pytest.mark.asyncio
async def test_an_unanswered_question_goes_to_the_human_once(monkeypatch):
    c = _Client()
    called = _fallback(monkeypatch)
    at = _asked(c)
    assert await askroute.tick(c, BOT, at + 60) == pytest.approx(WAIT - 60)
    assert called == []
    assert await askroute.tick(c, BOT, at + WAIT) is None
    assert called == [(ASK, "dembaca", "agent-coord")]
    assert await askroute.tick(c, BOT, at + WAIT + 60) is None and len(called) == 1


@pytest.mark.asyncio
async def test_the_reminders_heartbeat_passes_it_on(monkeypatch):
    """Under the reminders' lock, in their heartbeat (RT-6.19)."""
    c = _Client()
    called = _fallback(monkeypatch)
    at = _asked(c)
    await remind.tick(c, BOT, at + WAIT)
    assert called == [(ASK, "dembaca", "agent-coord")]


@pytest.mark.asyncio
@pytest.mark.parametrize("user,settled", [(COORD, True), (HUMAN, True), (OTHER, False), (AGENT, False)],
                         ids=["the crown", "a trusted human", "not trusted", "another agent"])
async def test_an_answer_in_time_settles_it(monkeypatch, user, settled):
    c = _Client()
    called = _fallback(monkeypatch)
    at = _asked(c)
    c.posts.append(post_of({"id": "r" * 26, "root_id": ASK, "user_id": user, "channel_id": WORK,
                            "message": "B", "create_at": 2000}))
    await askroute.tick(c, BOT, at + WAIT)
    assert (called == []) is settled
    assert (asks.questions_open(BOT) == []) is True  # settled, or passed on and closed


@pytest.mark.asyncio
async def test_it_waits_while_the_human_has_another_ask_open(monkeypatch):
    c = _Client()
    called = _fallback(monkeypatch)
    at = _asked(c)
    asks.add(BOT, "x" * 26, "dembaca", "x" * 26)
    await askroute.tick(c, BOT, at + WAIT)
    assert called == [] and len(asks.questions_open(BOT)) == 1
    asks.resolve(BOT, "x" * 26)
    await askroute.tick(c, BOT, at + WAIT + 60)
    assert called == [(ASK, "dembaca", "agent-coord")]


@pytest.mark.asyncio
async def test_the_fallback_asks_the_human_linked_and_closes_the_question(monkeypatch):
    """RT-6.25 step 6: one ask to the human, linked both ways; ✅ on the
    question; an ask_route event says what happened."""
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    inbox = WebSocketInbox(client=c)
    monkeypatch.setattr(server, "_inbox", inbox)
    q = "q" * 26
    asks.add(BOT, q, "agent-coord", q, title="A or B for the cache?", question="dembaca", blocking=True)
    c.post_id = "f" * 26
    aid = await server._ask_fallback(c, q, "dembaca", "agent-coord")
    ask, link = c.posted
    assert aid == "f" * 26
    assert ask["message"] == "\U0001F64B @dembaca"
    assert title_of(ask) == "A or B for the cache?"
    assert ask["root_id"] is None
    fields = _fields(ask)
    assert fields["Where"] == f"ask {q}" and fields["Blocking"] == "yes"
    assert "agent-coord" in fields["Context"] and "15 min" in fields["Context"]
    assert link == {"message": f"🙋 ask {aid}", "channel_id": None, "root_id": q,
                    "props": {"a2a": {"v": 1, "kind": "ask_link"}}}
    assert c.reacted == [(q, "white_check_mark")]
    assert asks.open_to(BOT, "dembaca") == [aid] and asks.open_to(BOT, "agent-coord") == []
    assert asks.waiting(BOT)[0][0] == aid  # reminded like any ask to a human
    assert inbox.waits(askroute.KIND)
    event = inbox._queue.get_nowait()
    assert aid in event.post.message and q in event.post.message
    item = (await server._event_items([event]))[0]
    assert item["kind"] == "ask_route" and item["from_agent"] is False
    assert item["from_trusted_human"] is False


# --- the 👑 is a peer of this session (RT-6.25 steps 3, 5, 6; ADR-001) -------------------

FOREIGN = "for @carol · trusts @carol · via agent-registrar"
UNBOUND = "Registered via agent-registrar"


@pytest.mark.asyncio
@pytest.mark.parametrize("description", [FOREIGN, UNBOUND, None],
                         ids=["foreign", "unbound", "not readable"])
async def test_a_question_never_goes_to_a_crown_that_is_no_peer(monkeypatch, description):
    """RT-6.25 step 3, security: a foreign, unbound or unreadable 👑 is no
    one to ask; the question goes to the human at once, and says why."""
    c = _Client()
    if description is None:
        del c.descriptions[COORD]
    else:
        c.descriptions[COORD] = description
    _coordinator_mode(monkeypatch, c)
    out = json.loads(await server.ask_human(need="A or B?", kind="question"))
    assert c.posted[0]["message"] == "\U0001F64B @dembaca"
    assert title_of(c.posted[0]) == "A or B?"
    assert out["route"] == "human" and "to" not in out
    assert out["route_note"] == ("the home's \U0001F451 agent-coord is not a peer of this session: "
                                 "asked the human at once")
    assert asks.open_to(BOT, "agent-coord") == [] and asks.questions(BOT) == {}


@pytest.mark.asyncio
async def test_a_crown_that_is_not_a_bot_found_is_no_peer(monkeypatch):
    """RT-6.25 step 3: the holder's account cannot be found: the human."""
    c = _Client()
    c.bots.clear()
    _coordinator_mode(monkeypatch, c)
    out = json.loads(await server.ask_human(need="A or B?", kind="question"))
    assert out["route"] == "human" and "not a peer" in out["route_note"]


@pytest.mark.asyncio
async def test_without_the_owner_split_no_crown_is_a_peer(monkeypatch):
    """RT-6.25 step 3: my own bot unbound (the split off): no 👑 can be shown
    to be a peer."""
    c = _Client()
    c.descriptions[BOT] = UNBOUND
    _coordinator_mode(monkeypatch, c)
    out = json.loads(await server.ask_human(need="A or B?", kind="question"))
    assert out["route"] == "human" and "not a peer" in out["route_note"]


@pytest.mark.asyncio
async def test_a_peer_crown_is_asked_as_before(monkeypatch):
    c = _Client()
    _coordinator_mode(monkeypatch, c)
    out = json.loads(await server.ask_human(need="A or B?", kind="question"))
    assert out["route"] == "coordinator"
    assert c.posted[0]["message"].startswith("\u2753 @agent-coord")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["reply", "reaction"])
async def test_a_foreign_crowns_answer_closes_nothing(monkeypatch, kind):
    """RT-6.25 step 6, security: the crown moved to a foreign agent after the
    question: its reply or 👍 does not settle it, the human is asked at the
    end of the wait; a trusted human's still does."""
    c = _Client()
    c.descriptions[COORD] = FOREIGN
    called = _fallback(monkeypatch)
    at = _asked(c)
    if kind == "reply":
        c.posts.append(post_of({"id": "r" * 26, "root_id": ASK, "user_id": COORD,
                                "channel_id": WORK, "message": "B", "create_at": 2000}))
    else:
        c.reacts = {ASK: [{"user_id": COORD, "post_id": ASK, "emoji_name": "+1"}]}
    await askroute.tick(c, BOT, at + WAIT)
    assert called == [(ASK, "dembaca", "agent-coord")]

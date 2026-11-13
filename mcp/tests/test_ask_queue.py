"""Asks to the same target are queued, not refused (RT-6.11 step 2): stored
with all fields, posted when the open one is answered or resolved, routed as
of then (RT-6.25), told to the agent, shown to the human and in `tasks`."""

import asyncio
import json
import time

import pytest

from mattermost_ai_agent_bus import askroute, asks, channel, lease, remind, server
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent

from mattermost_ai_agent_bus.model import ChannelUser

from fakes import WORK, FakeChat

BOT = "b" * 26
HUMAN = "h" * 26
COORD = "k" * 26

MINE = "for @dembaca · trusts @dembaca · via agent-registrar"


class _Bus(FakeChat):
    """FakeChat whose posts have their own ids and are in the channel."""

    def __init__(self):
        super().__init__(uid=BOT, username="agent-worker", trusted={HUMAN: "dembaca"},
                         names={HUMAN: "dembaca", COORD: "agent-coord", BOT: "agent-worker"},
                         agents={COORD, BOT}, channel="proj")
        self.reacted = []
        self.descriptions |= {BOT: MINE, COORD: MINE}  # the 👑 is a peer (ADR-001)
        self.bots["agent-coord"] = ChannelUser(COORD, "agent-coord", create_at=1)
        self.n = 0

    async def add_reaction(self, post_id, emoji):
        self.reacted.append((post_id, emoji))

    async def post(self, message, *, channel_id=None, root_id=None, props=None):
        self.n += 1
        self.post_id = f"p{self.n}".ljust(26, "0")
        got = await super().post(message, channel_id=channel_id, root_id=root_id, props=props)
        self.posts.append({"id": got.id, "message": message, "user_id": BOT, "channel_id": WORK,
                           "root_id": root_id or "", "create_at": int(time.time() * 1000) - 1000})
        return got

    def ids(self):
        return [f"p{i}".ljust(26, "0") for i in range(1, self.n + 1)]


class _Inbox:
    def __init__(self):
        self.sent = []

    def notify(self, kind, message):
        self.sent.append((kind, message))


@pytest.fixture
def bus(monkeypatch):
    for v in ("MM_RESPONSIBLE_HUMAN", "MM_AGENT_KIND", "MM_ASK_ROUTE", "MM_ASK_ROUTE_WAIT_MIN"):
        monkeypatch.delenv(v, raising=False)
    askroute._looked.clear()
    monkeypatch.setattr(lease, "crown_held", lambda uid: False)
    c = _Bus()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    inbox = _Inbox()
    monkeypatch.setattr(server, "_get_inbox", lambda: inbox)
    c.inbox = inbox
    return c


def title_of(posted):
    return posted["props"]["attachments"][0]["title"]


async def _ask(**kw):
    return json.loads(await server.ask_human(**kw))


@pytest.mark.asyncio
async def test_a_second_ask_is_queued_with_all_its_fields(bus):
    first = await _ask(need="Merge #61?")
    out = await _ask(need="Archive #60?", context="Nothing uses it.", recommendation="Yes.",
                     decision="Archive 60", where="#60", blocking=True, answers=["go", "no-go"],
                     root_id="t" * 26)
    assert out["posted"] is False and out["queued"] is True and out["position"] == 1
    assert out["behind"] == first["ask"] and len(bus.posted) == 1
    (e,) = asks.queued(BOT, "dembaca")
    assert (e["need"], e["context"], e["recommendation"], e["decision"], e["where"]) == (
        "Archive #60?", "Nothing uses it.", "Yes.", "Archive 60", "#60")
    assert e["blocking"] is True and e["link"] == "t" * 26
    third = await _ask(need="Keep VM 399?")
    assert third["position"] == 2


def test_the_queue_survives_multi_line_text_and_the_old_file_format(bus):
    asks.add(BOT, "a" * 26, "dembaca", "a" * 26)
    _, qid = asks.enqueue(BOT, "dembaca", {"need": "x?", "context": "line 1\nline 2 = ü \U0001F64B", "who": "dembaca"})
    text = asks._path(BOT).read_text()
    assert text.count("\n") == len(text.splitlines()) and "line 1" not in text
    assert asks.queued(BOT, "dembaca")[0]["context"] == "line 1\nline 2 = ü \U0001F64B"
    assert asks.open_asks(BOT) == ["a" * 26]  # the open ask is read as before
    asks.resolve(BOT, "a" * 26)
    assert asks.queued(BOT, "dembaca")  # resolve keeps the queue
    assert asks.claim(BOT, qid)["need"] == "x?" and asks.claim(BOT, qid) is None  # once
    assert not asks._path(BOT).exists()


@pytest.mark.asyncio
async def test_one_question_and_the_approval_floor_are_checked_when_queued(bus):
    await _ask(need="Merge #61?")
    out = await _ask(need="Three open points", context="1. Merge #62 now? 2. Keep VM 399?")
    assert out["posted"] is False and "one question per ask" in out["hint"] and "queued" not in out
    out = await _ask(need="Install 02dadca on hogan?", answers=["A", "B", "C"])
    assert out["posted"] is False and "go / no-go" in out["hint"] and "queued" not in out
    assert asks.queued(BOT) == []


@pytest.mark.asyncio
async def test_at_most_five_wait_per_target(bus):
    first = await _ask(need="Ask 0?")
    for i in range(5):
        assert (await _ask(need=f"Ask {i + 1}?"))["position"] == i + 1
    out = await _ask(need="Ask 6?")
    assert out["posted"] is False and out["queue_full"] is True and out["open_ask"] == first["ask"]
    assert len(asks.queued(BOT, "dembaca")) == 5
    other = await _ask(need="Another?", human="kollegin")  # its own queue
    assert other["ask"]


@pytest.mark.asyncio
async def test_resolve_ask_posts_the_next_at_once_and_tells_the_agent(bus):
    first = await _ask(need="Merge #61?")
    await _ask(need="Archive #60?", context="Nothing uses it.")
    await _ask(need="Keep VM 399?")
    await server.resolve_ask(first["ask"])
    assert len(bus.posted) == 2 and title_of(bus.posted[1]) == "Archive #60?"
    assert [e["need"] for e in asks.queued(BOT)] == ["Keep VM 399?"]
    kind, text = bus.inbox.sent[0]
    assert kind == askroute.POSTED == "ask_posted" and bus.ids()[1] in text and "1 more" in text
    # The second now holds the queue: resolving it posts the third.
    await server.resolve_ask(bus.ids()[1])
    assert len(bus.posted) == 3 and asks.queued(BOT) == []


@pytest.mark.asyncio
async def test_an_ask_queued_by_an_older_version_is_cleaned_when_posted(bus, monkeypatch):
    """RT-6.11 step 2: a queued ask's fields go through the mention check
    again when it is posted, so one stored before 0.86.2 notifies no one else."""
    async def humans_among(names):
        return {n for n in names if n.lower() in {"dembaca", "kollegin"}}
    monkeypatch.setattr(bus, "humans_among", humans_among, raising=False)
    first = await _ask(need="Merge #61?")
    asks.enqueue(BOT, "dembaca", _entry(need="Ask @kollegin?", context="@kollegin knows",
                                        answers=["go", "ask @kollegin"]))
    await server.resolve_ask(first["ask"])
    assert len(bus.posted) == 2 and "@kollegin" not in json.dumps(bus.posted[1])
    assert title_of(bus.posted[1]) == "Ask kollegin?"


@pytest.mark.asyncio
async def test_a_queued_ask_keeps_its_lines_like_a_direct_one(bus):
    """RT-6.11 step 1: the rest of need is the first paragraph, the lines stay."""
    first = await _ask(need="Merge #61?")
    await _ask(need="Archive #60? Nothing uses it.", context="- one\n\n\n- two")
    await server.resolve_ask(first["ask"])
    fields = {f["title"]: f["value"] for f in bus.posted[1]["props"]["attachments"][0]["fields"]}
    assert fields["Context"] == "Nothing uses it.\n\n- one\n\n- two"


@pytest.mark.asyncio
@pytest.mark.parametrize("by", ["reaction", "reply"])
async def test_an_answer_on_the_bus_posts_the_next_on_the_heartbeat(bus, by):
    first = await _ask(need="Merge #61?")
    await _ask(need="Archive #60?")
    await remind.tick(bus, BOT)
    assert len(bus.posted) == 1  # nobody answered yet
    if by == "reaction":
        bus.reacts[first["ask"]] = [{"user_id": HUMAN, "emoji_name": "+1"}]
    else:
        bus.posts.append({"id": "r" * 26, "root_id": first["ask"], "user_id": HUMAN,
                          "channel_id": WORK, "create_at": int(time.time() * 1000)})
    await remind.tick(bus, BOT)
    assert len(bus.posted) == 2 and asks.queued(BOT) == []
    assert bus.inbox.sent[0][0] == "ask_posted"
    await remind.tick(bus, BOT)  # the answered one holds nothing back, the new one does
    assert len(bus.posted) == 2


@pytest.mark.asyncio
async def test_a_reaction_by_someone_else_posts_nothing(bus):
    first = await _ask(need="Merge #61?")
    await _ask(need="Archive #60?")
    bus.reacts[first["ask"]] = [{"user_id": COORD, "emoji_name": "+1"},
                                {"user_id": HUMAN, "emoji_name": "eyes"}]
    await remind.tick(bus, BOT)
    assert len(bus.posted) == 1 and len(asks.queued(BOT)) == 1


@pytest.mark.asyncio
async def test_the_next_ask_is_routed_when_it_is_posted(bus, monkeypatch):
    """RT-6.25: a question queued in direct mode goes to the 👑 once the mode
    is ask mode coordinator; the queue is kept per target it waited behind."""
    async def holder(client, channel_id):
        return "agent-coord"
    monkeypatch.setattr(lease, "coordinator_of", holder)
    first = await _ask(need="Merge #61?")
    out = await _ask(need="Which order, A or B?", kind="question")
    assert out["queued"] is True and "route" not in out
    monkeypatch.setenv("MM_ASK_ROUTE", "coordinator")
    await server.resolve_ask(first["ask"])
    assert bus.posted[1]["message"].startswith("\u2753 @agent-coord")
    assert asks.questions(BOT)[bus.ids()[1]].startswith("dembaca ")
    # A question to the 👑 is open now: an approval to the human is not behind it.
    assert (await _ask(need="Merge #62?"))["ask"] and len(bus.posted) == 3


@pytest.mark.asyncio
async def test_a_question_queued_for_the_crown_goes_to_the_human_when_the_mode_is_direct(bus, monkeypatch):
    async def holder(client, channel_id):
        return "agent-coord"
    monkeypatch.setattr(lease, "coordinator_of", holder)
    monkeypatch.setenv("MM_ASK_ROUTE", "coordinator")
    first = await _ask(need="Which order, A or B?", kind="question")
    assert first["route"] == "coordinator"
    out = await _ask(need="Which name, A or B?", kind="question")
    assert out["queued"] is True and out["behind"] == first["ask"]
    monkeypatch.delenv("MM_ASK_ROUTE")
    await server.resolve_ask(first["ask"])
    assert bus.posted[1]["message"].startswith("\U0001F64B @dembaca")


@pytest.mark.asyncio
async def test_an_expired_open_ask_blocks_and_is_not_queued_behind(bus):
    first = await _ask(need="Merge #61?")
    asks._write(BOT, {first["ask"]: time.time() - asks.APPROVAL_SEC - 60}, set(),
                {first["ask"]: f"dembaca {first['ask']}"})
    out = await _ask(need="Archive #60?")
    assert out["posted"] is False and "more than 24 h old" in out["hint"] and "queued" not in out
    asks.enqueue(BOT, "dembaca", {
        "need": "q?", "context": "", "recommendation": "", "decision": "", "where": "",
        "blocking": False, "answers": "", "who": "dembaca", "link": "", "kind": "approval"})
    bus.reacts[first["ask"]] = [{"user_id": HUMAN, "emoji_name": "+1"}]
    await remind.tick(bus, BOT)
    assert len(bus.posted) == 1  # until the agent closes it
    await server.resolve_ask(first["ask"])
    assert len(bus.posted) == 2


def test_the_reminder_says_how_many_wait():
    assert "more waiting" not in remind.text("dembaca", "Merge #61?", 1300)
    assert remind.text("dembaca", "Merge #61?", 1300).startswith("\U0001F514 @dembaca — reminder: **Merge #61?**")
    assert remind.text("dembaca", "Merge #61?", 1300, 2).endswith("or reply here · 2 more waiting")


@pytest.mark.asyncio
async def test_the_sent_reminder_counts_the_queue(bus):
    first = await _ask(need="Merge #61?")
    await _ask(need="Archive #60?")
    await _ask(need="Keep VM 399?")
    await remind.tick(bus, BOT, time.time() + 3600)
    reminder = bus.posted[-1]
    assert reminder["root_id"] == first["ask"] and reminder["message"].endswith("· 2 more waiting")


@pytest.mark.asyncio
async def test_tasks_show_the_agents_own_queue_marked_queued(bus):
    first = await _ask(need="Merge #61?")
    await _ask(need="Archive #60?")
    out = json.loads(await server.coordinator("tasks"))
    (row,) = [a for a in out["asks"] if a.get("queued")]
    assert row["human"] == "dembaca" and row["need"] == "Archive #60?" and row["behind"] == first["ask"]
    assert row["position"] == 1 and row["by"] == "agent-worker"
    assert [a["ask"] for a in out["asks"] if not a.get("queued")] == [first["ask"]]
    rows = await server._overview(bus, "tasks")
    assert [a["queued"] for a in rows["asks_by_human"]["dembaca"] if a.get("queued")] == [True]


class _Named(_Bus):
    async def humans_among(self, names):
        return {n for n in names if n.lower() == "dembaca"}

    async def direct_channel_with_bot(self, name):
        return "dm"


@pytest.mark.asyncio
@pytest.mark.parametrize("word,warns", [("IDLE", True), ("RESULT", True), ("PROGRESS", False), ("", False)])
async def test_idle_or_result_naming_a_human_without_an_ask_warns(monkeypatch, word, warns):
    c = _Named()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.post_message(f"{word} · merge waits on dembaca".lstrip(" ·")))
    assert out["unasked"] == ["dembaca"] and "call ask_human now" in out["hint"]  # as before
    assert ("warning" in out) is warns
    assert len(c.posted) == 1  # never blocks


@pytest.mark.asyncio
async def test_a_queued_ask_counts_as_an_ask_for_the_warning(monkeypatch):
    c = _Named()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(lease, "crown_held", lambda uid: False)
    await server.ask_human(need="Merge #61?")
    await server.ask_human(need="Archive #60?")  # queued
    asks.resolve(BOT, c.ids()[0])  # no open ask now, one queued
    out = json.loads(await server.post_message("IDLE · open: go from dembaca"))
    assert "unasked" not in out and "warning" not in out


def _entry(**kw):
    return {"need": "q?", "context": "", "recommendation": "", "decision": "", "where": "",
            "blocking": False, "answers": "", "who": "dembaca", "link": "", "kind": "approval"} | kw


@pytest.mark.asyncio
async def test_the_heartbeat_and_resolve_ask_post_each_queued_ask_once(bus):
    """The race of the review: the heartbeat is posting the next ask when
    resolve_ask runs; each queued ask goes out once, none is lost."""
    first = await _ask(need="Merge #61?")
    await _ask(need="Archive #60?")
    await _ask(need="Keep VM 399?")
    bus.reacts[first["ask"]] = [{"user_id": HUMAN, "emoji_name": "+1"}]
    gate, entered = asyncio.Event(), asyncio.Event()
    orig, calls = bus.post, [0]

    async def slow(message, **kw):
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            await gate.wait()
        return await orig(message, **kw)

    bus.post = slow
    tick = asyncio.create_task(remind.tick(bus, BOT))
    await entered.wait()
    resolve = asyncio.create_task(server.resolve_ask(first["ask"]))  # the heartbeat's post is on its way
    await asyncio.sleep(0.05)
    assert not resolve.done()  # it waits its turn: one queued ask is posted at a time
    gate.set()
    await asyncio.gather(tick, resolve)
    sent = [title_of(p) for p in bus.posted]
    assert sum("Archive #60?" in m for m in sent) == 1
    assert sum("Keep VM 399?" in m for m in sent) <= 1
    left = [e["need"] for e in asks.queued(BOT)]
    assert ("Keep VM 399?" in left) == (not any("Keep VM 399?" in m for m in sent))
    assert len(asks.open_to(BOT, "dembaca")) == 1  # one open ask at a time


@pytest.mark.asyncio
async def test_a_failure_after_the_ask_is_out_spends_the_entry(bus):
    """RT-6.11 error row: the ask is posted, its link is not: not posted twice."""
    first = await _ask(need="Merge #61?")
    await _ask(need="Archive #60?", root_id="t" * 26)
    orig = bus.post

    async def flaky(message, **kw):
        if kw.get("root_id") == "t" * 26:
            raise OSError("network down")
        return await orig(message, **kw)

    bus.post = flaky
    await server.resolve_ask(first["ask"])
    assert asks.queued(BOT) == [] and len(bus.posted) == 2
    bus.post = orig
    await server.resolve_ask(bus.ids()[-1])
    assert len(bus.posted) == 2  # nothing posts a second time


@pytest.mark.asyncio
async def test_a_failure_before_the_ask_is_out_keeps_the_entry(bus):
    first = await _ask(need="Merge #61?")
    await _ask(need="Archive #60?")
    orig = bus.post

    async def down(message, **kw):
        raise OSError("network down")

    bus.post = down
    await server.resolve_ask(first["ask"])
    assert [e["need"] for e in asks.queued(BOT)] == ["Archive #60?"]
    bus.post = orig
    await remind.tick(bus, BOT)
    assert len(bus.posted) == 2 and asks.queued(BOT) == []


@pytest.mark.asyncio
async def test_a_queued_ask_has_an_id_and_is_withdrawn_by_it(bus):
    first = await _ask(need="Merge #61?")
    one = await _ask(need="Archive #60?")
    two = await _ask(need="Keep VM 399?")
    assert one["id"].startswith("q-") and two["id"] != one["id"]
    out = json.loads(await server.resolve_ask(one["id"]))
    assert out == {"ask": one["id"], "queued": True, "was_open": True}
    assert [e["id"] for e in asks.queued(BOT)] == [two["id"]]
    assert json.loads(await server.resolve_ask(one["id"]))["was_open"] is False
    assert bus.reacted == []  # no post to put a ✅ on
    await server.resolve_ask(first["ask"])
    assert title_of(bus.posted[1]) == "Keep VM 399?"


@pytest.mark.asyncio
async def test_queue_full_names_the_ids_to_withdraw(bus):
    await _ask(need="Merge #61?")
    ids = [(await _ask(need=f"Ask {i}?"))["id"] for i in range(asks.QUEUE_MAX)]
    out = await _ask(need="One more?")
    assert out["queue_full"] is True and all(i in out["hint"] for i in ids)


@pytest.mark.asyncio
async def test_a_queued_result_carries_the_floor_and_the_unmentioned(bus):
    await _ask(need="Merge #61?")
    out = await _ask(need="Which order, A or B?", kind="question", context="ping @all")
    assert out["queued"] is True and out["kind"] == "question"
    out = await _ask(need="Deploy it now?", kind="question")
    assert out["queued"] is True and out["kind"] == "approval" and out["kind_declared"] == "question"
    assert out["floor"]


@pytest.mark.asyncio
async def test_a_changed_route_on_the_heartbeat_still_waits_behind_an_open_ask(bus, monkeypatch):
    """The target the ask goes to now is judged by its own open asks, not
    by the one the heartbeat found answered."""
    async def holder(client, channel_id):
        return "agent-coord"
    monkeypatch.setattr(lease, "coordinator_of", holder)
    monkeypatch.setenv("MM_ASK_ROUTE", "coordinator")
    first = await _ask(need="Merge #61?")
    asks.enqueue(BOT, "dembaca", _entry(need="Which order, A or B?", kind="question"))
    crown = await _ask(need="Which name, A or B?", kind="question")  # open to the 👑
    assert crown["route"] == "coordinator"
    bus.reacts[first["ask"]] = [{"user_id": HUMAN, "emoji_name": "+1"}]
    await remind.tick(bus, BOT)
    assert len(bus.posted) == 2  # the question would go to the 👑, who has one open
    assert [e["need"] for e in asks.queued(BOT)] == ["Which order, A or B?"]


@pytest.mark.asyncio
async def test_ask_posted_and_ask_route_wake_nobody_but_a_stall_wakes_the_crown(bus, monkeypatch):
    monkeypatch.setattr(server, "_client", bus)
    for kind, wakes in (("ask_posted", False), ("ask_route", False), ("stall", True)):
        event = ClassifiedEvent(kind=kind, post=post_of({"id": "", "message": "x", "create_at": 1}))
        (item,) = await server._event_items([event])
        assert channel.wakes(item, coordinator=True, in_my_thread=False) is wakes, kind

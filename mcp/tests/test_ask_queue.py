"""Asks to the same target are queued, not refused (RT-6.11 step 2): stored
with all fields, posted when the open one is answered or resolved, routed as
of then (RT-6.25), told to the agent, shown to the human and in `tasks`."""

import json
import time

import pytest

from mattermost_ai_agent_bus import askroute, asks, lease, remind, server
from mattermost_ai_agent_bus.client import post_of

from fakes import WORK, FakeChat

BOT = "b" * 26
HUMAN = "h" * 26
COORD = "k" * 26


class _Bus(FakeChat):
    """FakeChat whose posts have their own ids and are in the channel."""

    def __init__(self):
        super().__init__(uid=BOT, username="agent-worker", trusted={HUMAN: "dembaca"},
                         names={HUMAN: "dembaca", COORD: "agent-coord", BOT: "agent-worker"},
                         agents={COORD, BOT}, channel="proj")
        self.reacted = []
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


async def _ask(**kw):
    return json.loads(await server.ask_human(**kw))


@pytest.mark.asyncio
async def test_a_second_ask_is_queued_with_all_its_fields(bus):
    first = await _ask(need="Merge #61?")
    out = await _ask(need="Archive #60?", context="Nothing uses it.", recommendation="Yes.",
                     decision="Archive 60", where="#60", blocking=True, answers="go / no-go",
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
    asks.enqueue(BOT, "dembaca", {"need": "x?", "context": "line 1\nline 2 = ü 🙋", "who": "dembaca"})
    text = asks._path(BOT).read_text()
    assert text.count("\n") == len(text.splitlines()) and "line 1" not in text
    assert asks.queued(BOT, "dembaca")[0]["context"] == "line 1\nline 2 = ü 🙋"
    assert asks.open_asks(BOT) == ["a" * 26]  # the open ask is read as before
    asks.resolve(BOT, "a" * 26)
    assert asks.queued(BOT, "dembaca")  # resolve keeps the queue
    asks.dequeue(BOT, "dembaca")
    assert not asks._path(BOT).exists()


@pytest.mark.asyncio
async def test_one_question_and_the_approval_floor_are_checked_when_queued(bus):
    await _ask(need="Merge #61?")
    out = await _ask(need="Three open points", context="1. Merge #62 now? 2. Keep VM 399?")
    assert out["posted"] is False and "one question per ask" in out["hint"] and "queued" not in out
    out = await _ask(need="Install 02dadca on hogan?", answers="A / B / C")
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
    assert len(bus.posted) == 2 and bus.posted[1]["message"].endswith("**Archive #60?**")
    assert [e["need"] for e in asks.queued(BOT)] == ["Keep VM 399?"]
    kind, text = bus.inbox.sent[0]
    assert kind == askroute.POSTED == "ask_posted" and bus.ids()[1] in text and "1 more" in text
    # The second now holds the queue: resolving it posts the third.
    await server.resolve_ask(bus.ids()[1])
    assert len(bus.posted) == 3 and asks.queued(BOT) == []


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
    assert bus.posted[1]["message"].startswith("❓ @agent-coord")
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
    assert bus.posted[1]["message"].startswith("🙋 @dembaca")


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
    assert remind.text("dembaca", "Merge #61?", 1300).startswith("🔔 @dembaca — reminder: **Merge #61?**")
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

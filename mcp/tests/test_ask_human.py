"""ask_human: one easy-to-spot post per ask, and reactions as answers (RT-6.11)."""

import json
import time

import pytest

from mattermost_ai_agent_bus import asks, channel, server
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent, WebSocketInbox

BOT = "b" * 26
HUMAN = "h" * 26
ASK = "a" * 26


class _Client:
    session = None

    def __init__(self):
        self.posted = []
        self.reacted = []
        self.found_reactions = []

    async def get_me(self):
        return {"id": BOT, "username": "agent-vtpm-hogan-1"}

    async def trusted_humans(self):
        return {HUMAN: "dembaca"}

    async def post(self, message, channel_id=None, root_id=None, props=None):
        self.posted.append({"message": message, "root_id": root_id, "props": props})
        return {"id": ASK, "message": message, "user_id": BOT, "channel_id": "c1"}

    async def add_reaction(self, post_id, emoji):
        self.reacted.append((post_id, emoji))

    async def reactions(self, post_id):
        return self.found_reactions

    async def usernames(self, ids):
        return {HUMAN: "dembaca"}

    async def get_channel(self, cid):
        return {"name": "proj-vtpm"}

    async def unread_channels(self):
        return []


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for v in ("MM_RESPONSIBLE_HUMAN", "MM_AGENT_KIND"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(server.lease, "crown_held", lambda uid: False)


@pytest.mark.asyncio
async def test_ask_is_one_post_with_the_mention_and_a_callout(monkeypatch):
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.ask_human(
        need="Go to install 02dadca on hogan", where="#50, hogan", blocking=True,
        answers="go / not now"))
    post = c.posted[0]
    assert post["message"] == "🙋 @dembaca — **Go to install 02dadca on hogan**"
    assert post["root_id"] is None  # stands on its own
    box = post["props"]["attachments"][0]
    fields = {f["title"]: f["value"] for f in box["fields"]}
    assert fields == {"from": "@agent-vtpm-hogan-1", "blocking": "yes",
                      "where": "#50, hogan",
                      "answer": "go / not now — 👍 = yes · 👎 = no · or reply in this thread"}
    assert box["color"] == "#d24b4e"
    assert out["ask"] == ASK and asks.open_asks(BOT) == [ASK]


@pytest.mark.asyncio
async def test_resolve_puts_a_check_mark_and_closes_the_ask(monkeypatch):
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    asks.add(BOT, ASK)
    out = json.loads(await server.resolve_ask(ASK, note="installed, thanks"))
    assert c.reacted == [(ASK, "white_check_mark")] and out["was_open"] is True
    assert asks.open_asks(BOT) == []
    assert c.posted[0]["root_id"] == ASK


def _inbox(client):
    inbox = WebSocketInbox(client=client, watched_channel_ids={"c1"})
    inbox._bot_user_id, inbox._bot_username = BOT, "agent-vtpm-hogan-1"

    class _Dead:
        async def close(self):
            pass
    inbox._ws = _Dead()
    return inbox


def _reaction_event(emoji, post_id=ASK, user=HUMAN, at=1):
    return {"event": "reaction_added", "broadcast": {"channel_id": "c1"},
            "data": {"reaction": json.dumps({"user_id": user, "post_id": post_id,
                                             "emoji_name": emoji, "create_at": at})}}


@pytest.mark.asyncio
async def test_a_thumbs_up_on_an_open_ask_reaches_the_agent_once(monkeypatch):
    """RT-6.11: a reaction raised no counter and reached nobody."""
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    inbox = _inbox(c)
    monkeypatch.setattr(server, "_get_inbox", lambda: inbox)
    asks.add(BOT, ASK)
    await inbox._on_reaction_event(_reaction_event("+1"))
    await inbox._on_reaction_event(_reaction_event("+1"))  # duplicate
    await inbox._on_reaction_event(_reaction_event("+1", post_id="o" * 26))  # not an ask
    await inbox._on_reaction_event(_reaction_event("+1", user=BOT))  # own
    items = json.loads(await server.wait_for_events(timeout_sec=1))
    assert len(items) == 1
    item = items[0]
    assert item["kind"] == "reaction" and item["answer"] == "yes" and item["ask"] == ASK
    assert item["from_trusted_human"] is True and item["trusted_human"] == "dembaca"
    assert asks.was_seen(BOT, asks.seen_key(ASK, HUMAN, "+1"))


@pytest.mark.asyncio
async def test_reactions_given_while_nobody_listened_are_backfilled():
    c = _Client()
    c.found_reactions = [{"user_id": HUMAN, "post_id": ASK, "emoji_name": "-1"}]
    inbox = _inbox(c)
    asks.add(BOT, ASK)
    await inbox._backfill()
    events = await inbox.wait_for_events(timeout_sec=1)
    assert [(e.kind, e.raw["emoji"]) for e in events] == [("reaction", "-1")]
    assert asks.answer_of("-1") == "no" and asks.answer_of("eyes") is None


class _NamingClient(_Client):
    """Knows which usernames are people."""

    PEOPLE = {"dembaca", "kollegin"}

    async def humans_among(self, names):
        return {n for n in names if n.lower() in self.PEOPLE}


@pytest.mark.asyncio
async def test_posts_never_mention_a_human(monkeypatch):
    """RT-6.11: only ask_human notifies a person; elsewhere a name stays a name."""
    c = _NamingClient()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.reply_in_thread(
        "r1", "@dembaca gave the coordination to @agent-mac-1; APPROVE from @Dembaca "
              "in thread, @kollegin too. @channel heads-up, mail a@b.de, `@dembaca` in code"))
    msg = c.posted[0]["message"]
    assert msg == ("dembaca gave the coordination to @agent-mac-1; APPROVE from Dembaca "
                   "in thread, kollegin too. @channel heads-up, mail a@b.de, `@dembaca` in code")
    assert out["unmentioned"] == ["Dembaca", "dembaca", "kollegin"] and "ask_human" in out["hint"]


@pytest.mark.asyncio
async def test_a_post_without_human_mentions_is_untouched(monkeypatch):
    c = _NamingClient()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.post_message("@agent-cursor-1 please HANDOVER, then release"))
    assert c.posted[0]["message"] == "@agent-cursor-1 please HANDOVER, then release"
    assert "unmentioned" not in out and "unasked" not in out


@pytest.mark.asyncio
async def test_naming_a_human_without_an_ask_gets_a_hint(monkeypatch):
    """RT-6.11: "merge waits on dembaca" reaches nobody; the result says to ask."""
    c = _NamingClient()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.reply_in_thread("r1", "review ok · merge: Dembaca decides"))
    assert c.posted[0]["message"] == "review ok · merge: Dembaca decides"  # posted all the same
    assert out["unasked"] == ["dembaca"] and "call ask_human now" in out["hint"]
    out = json.loads(await server.post_message("IDLE · open: go from dembaca for #190"))
    assert out["unasked"] == ["dembaca"]


@pytest.mark.asyncio
async def test_no_hint_with_an_open_ask_or_without_the_name(monkeypatch):
    c = _NamingClient()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    for text in ("merged #190", "branch dembaca-x, file a.dembaca, mail dembaca@x.de"):
        assert "unasked" not in json.loads(await server.post_message(text))
    asks.add(BOT, ASK, "dembaca")
    assert "unasked" not in json.loads(await server.post_message("merge waits on dembaca, ask " + ASK))


@pytest.mark.asyncio
async def test_ask_human_still_mentions(monkeypatch):
    c = _NamingClient()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    await server.ask_human(need="Merge #61")
    assert c.posted[0]["message"].startswith("🙋 @dembaca — ")


@pytest.mark.asyncio
async def test_humans_among_asks_once_and_skips_bots():
    import httpx

    from mattermost_ai_agent_bus.client import MattermostClient
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json=[{"username": "dembaca", "is_bot": False},
                                         {"username": "helper-bot", "is_bot": True}])

    c = MattermostClient(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await c.humans_among({"dembaca", "helper-bot", "nobody"}) == {"dembaca"}
    assert await c.humans_among({"Dembaca"}) == {"Dembaca"}
    assert calls == [["dembaca", "helper-bot", "nobody"]]  # second call from cache


@pytest.mark.asyncio
async def test_a_decision_comes_with_context_recommendation_and_wording(monkeypatch):
    """RT-6.11: the human answers in a word because the ask explains itself."""
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    await server.ask_human(
        need="Install 02dadca on hogan?",
        context="#50 throttles guest requests; lab 3.2 passed, the hogan part needs the package.",
        recommendation="Yes: the change is reviewed and the flood test is ready.",
        decision="Install 02dadca on hogan now; VM 399 stays up.")
    fields = [(f["title"], f["value"]) for f in c.posted[0]["props"]["attachments"][0]["fields"]]
    titles = [t for t, _ in fields]
    assert titles == ["from", "blocking", "where", "context", "recommendation", "decision", "answer"]
    assert dict(fields)["decision"] == "Install 02dadca on hogan now; VM 399 stays up."
    assert dict(fields)["answer"].startswith("👍 = yes to the decision")


@pytest.mark.asyncio
async def test_only_the_first_sentence_is_bold(monkeypatch):
    """RT-6.11: an explanation written into need goes to context, not into the bold title."""
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    await server.ask_human(
        need="**Merge #62** `reload` (new VMs within 5 s). Ready now: review passed.",
        context="Open only 1.1.")
    assert c.posted[0]["message"].endswith("— **Merge #62 `reload` (new VMs within 5 s).**")
    fields = dict((f["title"], f["value"]) for f in c.posted[0]["props"]["attachments"][0]["fields"])
    assert fields["context"] == "Ready now: review passed. Open only 1.1."


@pytest.mark.asyncio
async def test_one_open_ask_per_human_at_a_time(monkeypatch):
    """RT-6.11: several questions at once are asked one after the other."""
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    first = json.loads(await server.ask_human(need="Merge #61?"))
    second = json.loads(await server.ask_human(need="Archive #60?"))
    assert len(c.posted) == 1 and second["posted"] is False
    assert second["open_ask"] == first["ask"] and "resolve_ask" in second["hint"]
    other = json.loads(await server.ask_human(need="Hand on hogan?", human="kollegin"))
    assert other["ask"] and len(c.posted) == 2  # another human: own queue
    await server.resolve_ask(first["ask"])
    third = json.loads(await server.ask_human(need="Archive #60?"))
    assert third["ask"] and len(c.posted) == 3


@pytest.mark.asyncio
async def test_one_question_per_ask(monkeypatch):
    """RT-6.11 step 1: a list of questions in one ask is refused, so the human
    answers them one after the other."""
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.ask_human(
        need="Three open points",
        context="1. Merge #62 now? 2. Keep VM 399? 3. Who reviews #63?"))
    assert out["posted"] is False and "one question per ask" in out["hint"]
    assert c.posted == []
    await server.ask_human(need="Merge #62 now?", context="Review passed; CI is green.")
    assert len(c.posted) == 1


@pytest.mark.asyncio
async def test_an_answer_more_than_24_hours_after_the_ask_comes_expired(monkeypatch):
    """RT-6.11 step 5: a yes approves only within 24 h of the ask; a later
    reaction or reply reaches the agent marked expired, a timely one does not."""
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    inbox = _inbox(c)
    monkeypatch.setattr(server, "_get_inbox", lambda: inbox)
    now, fresh = time.time(), "f" * 26
    asks._write(BOT, {ASK: now - asks.APPROVAL_SEC - 60, fresh: now - 60}, set(),
                {ASK: "dembaca", fresh: "kollegin"})
    ms = int(now * 1000)
    await inbox._on_reaction_event(_reaction_event("+1", at=ms))
    await inbox._on_reaction_event(_reaction_event("+1", post_id=fresh, at=ms))
    await inbox._queue.put(ClassifiedEvent(kind="watched_channel", post={
        "id": "r" * 26, "root_id": ASK, "user_id": HUMAN, "channel_id": "c1",
        "create_at": ms, "message": "yes"}))
    # Someone not trusted approves nothing anyway: no flag on that late post.
    await inbox._queue.put(ClassifiedEvent(kind="watched_channel", post={
        "id": "s" * 26, "root_id": ASK, "user_id": "o" * 26, "channel_id": "c1",
        "create_at": ms, "message": "looks good"}))
    items = json.loads(await server.wait_for_events(timeout_sec=1))
    got = {(i["kind"], i["post"]["id"]): i.get("expired", False) for i in items}
    assert got == {("reaction", asks.seen_key(ASK, HUMAN, "+1")): True,
                   ("reaction", asks.seen_key(fresh, HUMAN, "+1")): False,
                   ("watched_channel", "r" * 26): True, ("watched_channel", "s" * 26): False}
    late = next(i for i in items if i["kind"] == "reaction" and i["ask"] == ASK)
    assert late["answer"] == "yes" and late["from_trusted_human"] is True  # what was meant
    assert channel.notification(late)["meta"]["expired"] == "true"
    out = json.loads(await server.ask_human(need="Merge #61?"))
    assert out["posted"] is False and "more than 24 h old" in out["hint"]


@pytest.mark.asyncio
async def test_a_reply_in_the_thread_of_an_ask_in_a_thread(monkeypatch):
    """RT-6.11 step 2: an ask posted in a thread is recorded with it, so a reply
    there counts as late only when every ask of this bot in it is."""
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    task, fresh, day = "t" * 26, "f" * 26, asks.APPROVAL_SEC
    await server.ask_human(need="Merge #61?", root_id=task)
    assert not asks.expired(BOT, task, thread=True)
    assert asks.expired(BOT, task, time.time() + day, thread=True)
    assert not asks.expired(BOT, task, time.time() + day)  # no ask by that id
    asks.add(BOT, fresh, "kollegin", task)
    assert not asks.expired(BOT, task, time.time() + day - 30, thread=True)
    assert asks.open_to(BOT, "dembaca") == [ASK] and asks.open_to(BOT, "kollegin") == [fresh]


def test_asks_written_before_the_thread_was_recorded_still_read():
    asks._path(BOT).parent.mkdir(parents=True, exist_ok=True)
    asks._path(BOT).write_text(f"ASK={ASK} {int(time.time())} dembaca\n")
    assert asks.open_to(BOT, "dembaca") == [ASK] and not asks.expired(BOT, ASK)


class _AskedClient(_NamingClient):
    """Remembers which names it was asked about."""

    asked: set = set()

    async def humans_among(self, names):
        self.asked = set(names)
        return await super().humans_among(names)


@pytest.mark.asyncio
async def test_agents_stay_mentioned_with_any_registrar_prefix(monkeypatch):
    """RT-6.11, arc42 §7.1: with prefix bot- a bot-… agent is never looked up
    as a person and keeps its mention; a human's name still turns plain."""
    monkeypatch.setenv("MM_AGENT_PREFIX", "bot-")
    c = _AskedClient()
    out, humans = await server._unmention(c, "@bot-mac-1 please review, @dembaca FYI")
    assert out == "@bot-mac-1 please review, dembaca FYI" and humans == ["dembaca"]
    assert c.asked == {"dembaca"}

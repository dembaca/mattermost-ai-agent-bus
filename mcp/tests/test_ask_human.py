"""ask_human: one easy-to-spot post per ask, and reactions as answers (RT-6.11).
Also what no other post of an agent may do: notify a person, or everyone."""

import json
import time

import pytest

from mattermost_ai_agent_bus import a2a, asks, channel, server
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import Reaction
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent, WebSocketInbox

from fakes import FakeChat

BOT = "b" * 26
HUMAN = "h" * 26
ASK = "a" * 26


class _Client(FakeChat):
    """FakeChat, plus the adapter's add_reaction the tools call."""

    def __init__(self):
        super().__init__(uid=BOT, username="agent-vtpm-hogan-1", trusted={HUMAN: "dembaca"},
                         names={HUMAN: "dembaca"}, channel="proj-vtpm", post_id=ASK)
        self.reacted = []

    async def add_reaction(self, post_id, emoji):
        self.reacted.append((post_id, emoji))


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
    c.reacts = {ASK: [Reaction(user_id=HUMAN, post_id=ASK, emoji_name="-1")]}
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
        "r1".ljust(26, "0"), "@dembaca gave the coordination to @agent-mac-1; APPROVE from @Dembaca "
              "in thread, @kollegin too. @channel heads-up, mail a@b.de, `@dembaca` in code"))
    msg = c.posted[0]["message"]
    assert msg == ("dembaca gave the coordination to @agent-mac-1; APPROVE from Dembaca "
                   "in thread, kollegin too. channel heads-up, mail a@b.de, `@dembaca` in code")
    assert out["unmentioned"] == ["Dembaca", "dembaca", "kollegin", "channel"]
    assert "ask_human" in out["hint"] and 'to="all"' in out["hint"]


EVERYONE = "@channel heads-up, @Here too, and @all. @@all, `@channel` in code, a@all.example, @all-hands"
PLAIN = "channel heads-up, Here too, and all. all, `@channel` in code, a@all.example, all-hands"


@pytest.mark.asyncio
async def test_posts_never_notify_everyone(monkeypatch):
    """RT-6.11 step 1: @channel, @here and @all become plain words in post_message,
    reply_in_thread and the note of resolve_ask, as a person's name does; the
    result says why and names the way to wake the agents."""
    c = _NamingClient()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.post_message(EVERYONE))
    assert c.posted[0]["message"] == PLAIN
    assert out["unmentioned"] == ["all", "channel", "here"]
    assert 'to="all"' in out["hint"] and "ask_human" not in out["hint"]
    await server.reply_in_thread("r1".ljust(26, "0"), EVERYONE)
    await server.resolve_ask(ASK, note=EVERYONE)
    assert [p["message"] for p in c.posted[1:]] == [PLAIN, PLAIN]


@pytest.mark.asyncio
async def test_everyone_is_rewritten_when_the_name_lookup_fails(monkeypatch):
    """RT-6.11 error handling: a failed lookup leaves names as written (fail
    open) but never lets @channel through."""
    class Down(_NamingClient):
        async def humans_among(self, names):
            raise RuntimeError("HTTP 500")

    c = Down()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    await server.post_message("@dembaca and @channel")
    assert c.posted[0]["message"] == "@dembaca and channel"


@pytest.mark.asyncio
async def test_an_ask_never_notifies_everyone(monkeypatch):
    """RT-6.11 step 1: the ask mentions its human only; @channel in any field
    of it is written plain, and the result says so."""
    c = _NamingClient()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.ask_human(
        need="Merge #61 @channel?", context="@here is waiting", recommendation="yes",
        decision="Merge @all", where="PR @channel", answers="go / @all"))
    text = json.dumps([c.posted[0]["message"], c.posted[0]["props"]])
    for word in ("@channel", "@here", "@all"):
        assert word not in text
    assert c.posted[0]["message"] == "🙋 @dembaca — **Merge #61 channel?**"
    assert out["unmentioned"] == ["all", "channel", "here"]


@pytest.mark.asyncio
async def test_to_all_wakes_every_agent_through_the_props(monkeypatch):
    """RT-6.11 step 1: to="all" writes props.a2a.to (with the kind a first word
    gives); the text needs no @channel. Anything else for a reply is refused."""
    c = _NamingClient()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.post_message("main is red, hold merges", to="all"))
    await server.post_message("IDLE · done: x", to="All")
    await server.reply_in_thread("r1".ljust(26, "0"), "freeze lifted", to="all")
    assert [p["props"] for p in c.posted] == [
        {"a2a": {"v": 1, "to": "all"}},
        {"a2a": {"v": 1, "kind": "idle", "intent": "fyi", "to": "all"}},
        {"a2a": {"v": 1, "to": "all"}}]
    assert "unmentioned" not in out
    await server.post_message("anyone seen #46?")
    assert c.posted[-1]["props"] is None
    assert "to must be" in json.loads(await server.reply_in_thread("r1".ljust(26, "0"), "hi", to="agent-x"))["refused"]


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
    out = json.loads(await server.reply_in_thread("r1".ljust(26, "0"), "review ok · merge: Dembaca decides"))
    assert c.posted[0]["message"] == "review ok · merge: Dembaca decides"  # posted all the same
    assert out["unasked"] == ["dembaca"] and "call ask_human now" in out["hint"]
    out = json.loads(await server.post_message("IDLE · open: go from dembaca for #190"))
    assert out["unasked"] == ["dembaca"]


@pytest.mark.asyncio
async def test_no_hint_with_an_open_ask_or_without_the_name(monkeypatch):
    c = _NamingClient()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    for text in (
        "merged #190",
        "branch dembaca-x, file a.dembaca, mail dembaca@x.de",
        "see https://github.com/dembaca/x/issues/69",
        "[#69](https://github.com/dembaca/x/issues/69)",
        "fixed dembaca/x#12",
        "branch dembaca/feature",
    ):
        assert "unasked" not in json.loads(await server.post_message(text))
    asks.add(BOT, ASK, "dembaca")
    assert "unasked" not in json.loads(await server.post_message("merge waits on dembaca, ask " + ASK))


@pytest.mark.asyncio
async def test_hint_when_the_name_stands_next_to_a_link(monkeypatch):
    c = _NamingClient()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    res = json.loads(await server.post_message("waits on dembaca: https://github.com/dembaca/x/pull/1"))
    assert "unasked" in res


@pytest.mark.asyncio
async def test_ask_human_still_mentions(monkeypatch):
    c = _NamingClient()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    await server.ask_human(need="Merge #61")
    assert c.posted[0]["message"].startswith("🙋 @dembaca — ")


@pytest.mark.asyncio
async def test_humans_among_asks_once_and_skips_bots():
    import httpx

    from mattermost_ai_agent_bus.client import MattermostAdapter
    calls = []

    def handler(req):
        calls.append(json.loads(req.content))
        return httpx.Response(200, json=[{"username": "dembaca", "is_bot": False},
                                         {"username": "helper-bot", "is_bot": True}])

    c = MattermostAdapter(base_url="https://mm.example", token="t")
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


def test_only_a_question_mark_that_ends_a_sentence_counts():
    """RT-6.11 step 1: two real questions still count two; a quoted example,
    a URL's query string and a code span hold no question of the ask's own."""
    q = server._questions
    assert q("Merge #62 now? Keep VM 399?") == 2
    assert q("1. Merge #62 now? 2. Keep VM 399? 3. Who reviews #63?") == 3
    assert q('The link reads "🙋 Merge PR #1?" now. Keep it?') == 1
    assert q("„Merge PR #1?“ und «Deploy?» stehen dort. Passt das?") == 1
    assert q("Isn't the headline 'Merge PR #1?' too short? Don't you think so") == 1
    assert q("The log is at https://ci.example/run?id=7&x=y. Rerun it?") == 1
    assert q("CI failed at https://ci.example/run?id=7? Rerun it?") == 2  # its own ? ends a sentence
    assert q("`git log --format=%s?` shows it. Push now?") == 1
    assert q("```\nwhy?\n```\nRun it?") == 1
    assert q("(Merge now?) Or wait?") == 2


@pytest.mark.asyncio
async def test_a_quoted_question_is_no_second_question(monkeypatch):
    """RT-6.11 step 1: an ask whose context quotes an example headline, a link
    with a query string or a code span with "?" is posted."""
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.ask_human(
        need="Shorten the ask link line?",
        context='It reads "🙋 Merge PR #1?" now (https://mm.example/pl/x?a=b, `ask?`).'))
    assert out["ask"] and len(c.posted) == 1


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
    await inbox._queue.put(ClassifiedEvent(kind="watched_channel", post=post_of({
        "id": "r" * 26, "root_id": ASK, "user_id": HUMAN, "channel_id": "c1",
        "create_at": ms, "message": "yes"})))
    # Someone not trusted approves nothing anyway: no flag on that late post.
    await inbox._queue.put(ClassifiedEvent(kind="watched_channel", post=post_of({
        "id": "s" * 26, "root_id": ASK, "user_id": "o" * 26, "channel_id": "c1",
        "create_at": ms, "message": "looks good"})))
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
async def test_an_ask_from_a_thread_stands_on_its_own_and_is_linked_there(monkeypatch):
    """RT-6.11 step 1/2: with root_id the ask is still a top-level post, its
    thread its own; the given thread gets a link to it without the mention, so
    the human is notified once. A later post in that thread answers nothing."""
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    task, day = "t" * 26, asks.APPROVAL_SEC
    out = json.loads(await server.ask_human(need="Merge #61?", root_id=task))
    ask, link = c.posted
    assert ask["root_id"] is None and ask["message"] == "🙋 @dembaca — **Merge #61?**"
    # A pointer, not an ask (RT-6.19): its own kind keeps it out of the open asks.
    # Humans read it as the ask's headline, linked (RT-6.15 step 2): asks-<uid>.env keeps it.
    assert link == {"message": f"🙋 ask {ASK}", "channel_id": None, "root_id": task,
                    "props": {"a2a": {"v": a2a.V, "kind": a2a.ASK_LINK}}}
    assert asks.titles(BOT) == {ASK: "Merge #61?"}
    assert out["ask"] == ASK and out["linked_in"] == task
    assert asks.expired(BOT, ASK, time.time() + day, thread=True)
    assert not asks.expired(BOT, task, time.time() + day, thread=True)  # no ask lives there
    assert asks.open_to(BOT, "dembaca") == [ASK]


@pytest.mark.asyncio
async def test_a_reply_in_a_thread_with_several_asks(monkeypatch):
    """RT-6.11 step 5: in a thread that holds several asks (recorded before
    0.56.0), a reply counts as late only when every ask of this bot in it is."""
    task, fresh, day = "t" * 26, "f" * 26, asks.APPROVAL_SEC
    asks.add(BOT, ASK, "dembaca", task)
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


def _answers():
    """The `answer` events the stats hold (RT-6.9)."""
    from mattermost_ai_agent_bus import stats
    f = stats._path()
    lines = f.read_text().splitlines() if f and f.exists() else []
    return [json.loads(x) for x in lines if '"k": "answer"' in x]


@pytest.fixture
def stats_state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", BOT)
    monkeypatch.delenv("MM_STATS_CHANNEL", raising=False)


@pytest.mark.asyncio
async def test_the_wait_for_an_ask_ends_at_the_first_trusted_answer(monkeypatch, stats_state):
    """RT-6.9: the wait is the answer's server time minus the ask's; only the
    first answer by a trusted human counts, whichever way it comes: a remark
    emoji, an untrusted human, a second reaction, a reply, or a restarted server
    changes nothing; an expired answer counts with its full wait."""
    from mattermost_ai_agent_bus import stats
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    inbox = _inbox(c)
    monkeypatch.setattr(server, "_get_inbox", lambda: inbox)
    await server.ask_human(need="Merge #62 now?", context="CI is green.")
    asked = asks._read(BOT)[0][ASK]
    later = int((asked + 90) * 1000)
    await inbox._on_reaction_event(_reaction_event("eyes", at=later))          # a remark
    await inbox._on_reaction_event(_reaction_event("+1", user="o" * 26, at=later))  # not trusted
    await inbox._on_reaction_event(_reaction_event("+1", at=later))
    await inbox._on_reaction_event(_reaction_event("-1", at=later + 60_000))   # second answer
    await inbox._queue.put(ClassifiedEvent(kind="watched_channel", post=post_of({
        "id": "r" * 26, "root_id": ASK, "user_id": HUMAN, "channel_id": "c1",
        "create_at": later + 120_000, "message": "yes, as said"})))
    assert len(json.loads(await server.wait_for_events(timeout_sec=1))) == 5
    assert [(a["id"], a["wait"]) for a in _answers()] == [(ASK, 90)]
    # The file remembers: a server that starts afresh counts the ask no more.
    assert asks.answered(BOT, ASK, asked + 500) is None
    s = stats.summary("0.36.0")
    assert (s["asks"], s["asks_answered"], s["ask_wait_sec"], s["asks_open"]) == (1, 1, 90, 0)


def test_an_answer_in_the_thread_counts_for_an_ask_in_a_thread_and_a_late_one_too(stats_state):
    """RT-6.9: the same matching as expired (RT-6.11); a permission prompt is no ask."""
    task, prompt = "t" * 26, "p" * 26
    asks.add(BOT, ASK, "dembaca", task)
    asks.add(BOT, prompt)                               # a relayed permission prompt (RT-6.13)
    day = asks.APPROVAL_SEC
    assert asks.answered(BOT, prompt) is None
    assert asks.answered(BOT, "z" * 26) is None
    pid, wait = asks.answered(BOT, task, time.time() + day + 60, thread=True)
    assert pid == ASK and day + 59 <= wait <= day + 61  # late: still an answer
    assert asks.answered(BOT, task, time.time(), thread=True) is None
    assert asks.is_open(BOT, ASK) and asks.open_to(BOT, "dembaca") == [ASK]


def test_old_ask_lines_still_parse_and_the_marker_goes_with_the_ask(stats_state):
    asks._path(BOT).parent.mkdir(parents=True, exist_ok=True)
    asks._path(BOT).write_text(f"ASK={ASK} {int(time.time()) - 30}\nASK={'f' * 26} {int(time.time())} kollegin\n")
    assert asks.answered(BOT, ASK) is None              # no human in the line: no ask_human ask
    assert asks.answered(BOT, "f" * 26)[0] == "f" * 26
    assert f"ANSWERED={'f' * 26}" in asks._path(BOT).read_text()
    asks.resolve(BOT, "f" * 26)
    assert "ANSWERED" not in asks._path(BOT).read_text()


class _Refuse(_Client):
    async def direct_channel_with_bot(self, name):
        raise AssertionError("refused before any lookup")


@pytest.mark.asyncio
@pytest.mark.parametrize("call,kinds", [
    (lambda: server.reply_in_thread(ASK, "PR #85 · x", kind="PR"), True),
    (lambda: server.post_message("on it", kind="PR"), True),
    (lambda: server.reply_in_thread(ASK, "DONE · x", kind="ACK"), True),
    (lambda: server.post_message("DONE · x", kind="ACK"), True),
    (lambda: server.reply_in_thread(ASK, "x", to="agent-other"), False),
    (lambda: server.announce("p", "t", "m", role="boss"), False),
    (lambda: server.coordinator("bogus"), False),
])
async def test_refused_input_is_a_result_and_posts_nothing(monkeypatch, call, kinds):
    """RT-6.14 step 2: a refusal is the tool's result with the reason, not an error."""
    c = _Refuse()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await call())
    assert out["posted"] is False and out["refused"]
    assert ("kinds" in out) is kinds
    if kinds:
        assert out["kinds"] == list(server.a2a.WORDS) and "PR" not in out["kinds"]
    assert c.posted == []


@pytest.mark.asyncio
async def test_ask_without_a_human_is_refused_as_a_result(monkeypatch):
    c = _Refuse()
    monkeypatch.setattr(server, "_get_client", lambda: c)

    async def nobody(client):
        return "", set()
    monkeypatch.setattr(server, "_responsible_and_trusted", nobody)
    out = json.loads(await server.ask_human(need="Go?"))
    assert out["posted"] is False and "no human to ask" in out["refused"]
    assert c.posted == []


def test_asks_go_on_the_bus_even_when_asked_in_the_terminal():
    """RT-6.11: a question for a human always goes out as ask_human, also when
    the agent asked in its own terminal or UI: the human may not watch it, the
    bus is where they see what waits. SKILL.md and the instructions say so."""
    from pathlib import Path

    from mattermost_ai_agent_bus import server

    skill = Path(server.__file__).resolve().parents[3] / "skills" / "mattermost-ai-agent-bus" / "SKILL.md"
    assert "even when you also asked in your terminal\nor UI" in skill.read_text()
    assert "even when you also asked in your terminal or UI" in server._instructions()

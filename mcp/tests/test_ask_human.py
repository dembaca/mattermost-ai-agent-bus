"""ask_human: one easy-to-spot post per ask, and reactions as answers (RT-6.11)."""

import json

import pytest

from mattermost_ai_agent_bus import asks, server
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

BOT = "b" * 26
HUMAN = "h" * 26
ASK = "a" * 26


class _Client:
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


def _reaction_event(emoji, post_id=ASK, user=HUMAN):
    return {"event": "reaction_added", "broadcast": {"channel_id": "c1"},
            "data": {"reaction": json.dumps({"user_id": user, "post_id": post_id,
                                             "emoji_name": emoji, "create_at": 1})}}


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
    assert "unmentioned" not in out


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

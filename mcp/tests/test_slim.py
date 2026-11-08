"""Lean tool results: slim posts, compact JSON, raw on request."""

import json

import pytest

from mattermost_ai_agent_bus import server, slim
from mattermost_ai_agent_bus.client import ask_headline, post_of

from fakes import FakeChat

POST = {
    "id": "p1", "root_id": "r1", "channel_id": "c1", "user_id": "u1",
    "create_at": 1792229058954, "message": "Listener-Test \U0001F442", "type": "",
    "props": {"from_bot": "true"}, "metadata": {"embeds": []}, "hashtags": "",
    "file_ids": [], "pending_post_id": "x", "reply_count": 0, "edit_at": 0,
}
SYSTEM = {"id": "s1", "channel_id": "c1", "user_id": "u2", "create_at": 1,
          "message": "agent-x added to the channel", "type": "system_add_to_channel"}


class _Client(FakeChat):
    """FakeChat, plus the adapter's list_recent the tool calls."""

    def __init__(self):
        super().__init__(names={"u1": "dembaca"})

    async def list_recent(self, **_):
        return [post_of(SYSTEM), post_of(POST)]


@pytest.mark.asyncio
async def test_slim_post_keeps_what_an_agent_needs():
    p = await slim.post(_Client(), post_of(POST))
    assert p == {"id": "p1", "root_id": "r1", "channel": "agents", "author": "dembaca",
                 "at": p["at"], "message": "Listener-Test \U0001F442"}
    assert len(json.dumps(p)) < len(json.dumps(POST)) / 2


@pytest.mark.asyncio
async def test_list_recent_drops_system_messages_and_is_compact(monkeypatch):
    monkeypatch.setattr(server, "_get_client", lambda: _Client())
    out = await server.list_recent()
    assert "added to the channel" not in out and "\U0001F442" in out and "\\u" not in out
    assert ": " not in out and "\n" not in out
    raw = json.loads(await server.list_recent(raw=True))
    assert raw == [SYSTEM, POST]


@pytest.mark.asyncio
async def test_names_are_optional(monkeypatch):
    class _Broken(_Client):
        async def usernames(self, ids):
            raise RuntimeError("down")

    p = await slim.post(_Broken(), post_of(POST))
    assert p["author"] == "u1" and p["message"] == "Listener-Test \U0001F442"


NEW_ASK = {"id": "k1", "channel_id": "c1", "user_id": "u1", "create_at": 1, "message": "\U0001F64B @dembaca",
           "props": {"a2a": {"v": 1, "kind": "ask"}, "attachments": [{"title": "Merge #4?"}]}}
OLD_ASK = {"id": "k2", "channel_id": "c1", "user_id": "u1", "create_at": 1,
           "message": "\U0001F64B @dembaca — **Merge #4?**", "props": {"a2a": {"v": 1, "kind": "ask"}}}


def test_post_of_reads_an_asks_title_from_its_card_or_its_old_text():
    """RT-6.11 step 1: the card's title, else the first bold text; no ask, no title."""
    assert post_of(NEW_ASK).title == "Merge #4?"
    assert post_of(OLD_ASK).title == "Merge #4?"
    assert post_of(POST).title == ""


def test_ask_headline_reads_the_card_title():
    """RT-6.15 step 2: a linked ask's headline is its card's title."""
    assert ask_headline(NEW_ASK) == "Merge #4?"
    assert ask_headline(OLD_ASK) == "Merge #4?"
    assert ask_headline(POST) == ""


@pytest.mark.asyncio
async def test_slim_puts_an_asks_title_after_the_mention_once():
    """RT-6.11 step 1: a new ask's question follows the mention as it did in the text; an old one is not doubled."""
    assert (await slim.post(_Client(), post_of(NEW_ASK)))["message"] == "\U0001F64B @dembaca — **Merge #4?**"
    assert (await slim.post(_Client(), post_of(OLD_ASK)))["message"] == "\U0001F64B @dembaca — **Merge #4?**"


def test_the_hook_api_and_the_reminder_read_the_question_from_the_card():
    """RT-6.11 step 1: the hook API's posts carry a new ask's question after the
    mention; the open asks name it (RT-6.19)."""
    from mattermost_ai_agent_bus import hookapi, tasks
    assert hookapi._slim(post_of(NEW_ASK), set(), set())["message"] == "\U0001F64B @dembaca — **Merge #4?**"
    assert [a["need"] for a in tasks.open_asks([post_of(NEW_ASK)], {"u1": "agent-x"})] == ["Merge #4?"]

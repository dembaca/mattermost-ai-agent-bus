"""Lean tool results: slim posts, compact JSON, raw on request."""

import json

import pytest

from mattermost_ai_agent_bus import server, slim
from mattermost_ai_agent_bus.client import post_of

POST = {
    "id": "p1", "root_id": "r1", "channel_id": "c1", "user_id": "u1",
    "create_at": 1792229058954, "message": "Listener-Test 👂", "type": "",
    "props": {"from_bot": "true"}, "metadata": {"embeds": []}, "hashtags": "",
    "file_ids": [], "pending_post_id": "x", "reply_count": 0, "edit_at": 0,
}
SYSTEM = {"id": "s1", "channel_id": "c1", "user_id": "u2", "create_at": 1,
          "message": "agent-x added to the channel", "type": "system_add_to_channel"}


class _Client:
    async def usernames(self, ids):
        return {"u1": "dembaca"}

    async def get_channel(self, cid):
        return {"name": "agents"}

    async def list_recent(self, **_):
        return [post_of(SYSTEM), post_of(POST)]


@pytest.mark.asyncio
async def test_slim_post_keeps_what_an_agent_needs():
    p = await slim.post(_Client(), post_of(POST))
    assert p == {"id": "p1", "root_id": "r1", "channel": "agents", "author": "dembaca",
                 "at": p["at"], "message": "Listener-Test 👂"}
    assert len(json.dumps(p)) < len(json.dumps(POST)) / 2


@pytest.mark.asyncio
async def test_list_recent_drops_system_messages_and_is_compact(monkeypatch):
    monkeypatch.setattr(server, "_get_client", lambda: _Client())
    out = await server.list_recent()
    assert "added to the channel" not in out and "👂" in out and "\\u" not in out
    assert ": " not in out and "\n" not in out
    raw = json.loads(await server.list_recent(raw=True))
    assert raw == [SYSTEM, POST]


@pytest.mark.asyncio
async def test_names_are_optional(monkeypatch):
    class _Broken(_Client):
        async def usernames(self, ids):
            raise RuntimeError("down")

    p = await slim.post(_Broken(), post_of(POST))
    assert p["author"] == "u1" and p["message"] == "Listener-Test 👂"

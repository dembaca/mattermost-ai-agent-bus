"""post_message(to=…): agents talk directly, people only through ask_human (RT-6.11)."""

import json

import httpx
import pytest

from mattermost_ai_agent_bus.client import MattermostClient

ME, PEER, PERSON, DM = "m" * 26, "p" * 26, "h" * 26, "d" * 26
USERS = {"agent-peer": {"id": PEER, "username": "agent-peer", "is_bot": True},
         "dembaca": {"id": PERSON, "username": "dembaca", "is_bot": False}}


def _client(calls):
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content or b"null")
        calls.append((req.url.path, body))
        if req.url.path == "/api/v4/users/me":
            return httpx.Response(200, json={"id": ME, "username": "agent-me"})
        if req.url.path == "/api/v4/users/usernames":
            return httpx.Response(200, json=[USERS[n] for n in body if n in USERS])
        if req.url.path == "/api/v4/channels/direct":
            return httpx.Response(201, json={"id": DM})
        return httpx.Response(404, json={})
    c = MattermostClient(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


@pytest.mark.asyncio
async def test_dm_to_another_agent():
    calls = []
    assert await _client(calls).direct_channel_with_bot("@Agent-Peer") == DM
    assert ("/api/v4/channels/direct", [ME, PEER]) in calls


@pytest.mark.asyncio
async def test_no_dm_to_a_person_or_unknown_name():
    calls = []
    c = _client(calls)
    with pytest.raises(ValueError, match="ask_human"):
        await c.direct_channel_with_bot("dembaca")
    with pytest.raises(ValueError, match="no user"):
        await c.direct_channel_with_bot("nobody")
    assert not any(path == "/api/v4/channels/direct" for path, _ in calls)

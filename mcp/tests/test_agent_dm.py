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
        if req.url.path == "/api/v4/posts/rootindm000000000000000000":
            return httpx.Response(200, json={"id": "rootindm000000000000000000", "channel_id": DM})
        if req.url.path == "/api/v4/posts":
            return httpx.Response(201, json={"id": "new", **body})
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


@pytest.mark.asyncio
async def test_reply_goes_where_its_root_is():
    """A reply to a DM stays in the DM: without channel_id the root decides,
    not the default channel (where Mattermost would refuse the root)."""
    calls = []
    post = await _client(calls).post("ok", root_id="rootindm000000000000000000")
    assert post.channel_id == DM and post.root_id == "rootindm000000000000000000"
    assert ("/api/v4/posts/rootindm000000000000000000", None) in calls


@pytest.mark.asyncio
async def test_own_post_leaves_no_unread_dm(monkeypatch):
    """Mattermost counts the bot's own post as unread for it; in a DM the Stop
    hook and the listener would then wake for it forever. The channel is
    marked read, unless the inbox holds — or, unconnected, may have missed —
    a message there still to be collected."""
    from mattermost_ai_agent_bus import server
    from mattermost_ai_agent_bus.client import post_of
    from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent, WebSocketInbox

    viewed = []

    class Client:
        async def view_channel(self, cid):
            viewed.append(cid)

    inbox = WebSocketInbox(client=Client())
    monkeypatch.setattr(server, "_inbox", inbox)
    await server._posted(Client(), post_of({"channel_id": DM}))
    assert viewed == []                       # not connected: may have missed one
    inbox._ws = object()
    await server._posted(Client(), post_of({"channel_id": DM}))
    assert viewed == [DM]
    inbox._queue.put_nowait(ClassifiedEvent(kind="dm", post=post_of({"channel_id": DM})))
    await server._posted(Client(), post_of({"channel_id": DM}))
    assert viewed == [DM]                     # a DM still to collect stays unread

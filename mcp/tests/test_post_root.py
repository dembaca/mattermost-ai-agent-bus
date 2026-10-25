"""Posting into a thread and reading the task window (RT-6.14): a reply's id
leads to its thread's root, Mattermost's refusal reaches the agent as text, and
the window pages back from the newest post."""

from __future__ import annotations

import json

import httpx
import pytest

from mattermost_ai_agent_bus import server
from mattermost_ai_agent_bus.client import MattermostClient, Refused

ME, ROOT, REPLY, CH = "m" * 26, "r" * 26, "q" * 26, "c" * 26


def _client(handler) -> MattermostClient:
    c = MattermostClient(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c._me = {"id": ME, "username": "agent-me"}
    return c


@pytest.mark.asyncio
async def test_a_reply_to_a_reply_goes_to_its_thread_root():
    """Mattermost refuses a reply's id as root_id: the post goes to the root."""
    sent = []

    def handler(req):
        if req.method == "GET" and req.url.path == f"/api/v4/posts/{REPLY}":
            return httpx.Response(200, json={"id": REPLY, "root_id": ROOT, "channel_id": CH})
        if req.method == "POST" and req.url.path == "/api/v4/posts":
            sent.append(json.loads(req.content))
            return httpx.Response(201, json={"id": "n" * 26, **sent[-1]})
        return httpx.Response(200, json={})

    await _client(handler).post("ACK", root_id=REPLY)
    assert (sent[0]["root_id"], sent[0]["channel_id"]) == (ROOT, CH)


@pytest.mark.asyncio
async def test_a_refused_post_says_why(monkeypatch):
    """The MCP SDK shows a raised error as a bare "Error executing tool": the
    tool returns Mattermost's reason instead."""
    def handler(req):
        if req.method == "GET":
            return httpx.Response(200, json={"id": ROOT, "channel_id": CH})
        return httpx.Response(400, json={"message": "Invalid RootId parameter."})

    c = _client(handler)
    with pytest.raises(Refused, match=r"\(400\): Invalid RootId"):
        await c.post("x", root_id=ROOT)
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = json.loads(await server.reply_in_thread(root_id=ROOT, message="ACK · on it"))
    assert out["posted"] is False and "Invalid RootId" in out["refused"]


@pytest.mark.asyncio
async def test_the_task_window_pages_back_from_the_newest():
    """With since, Mattermost answers at most 1000 posts in no set order: the
    window pages back from the newest until it is older than the window."""
    total, pages = 450, []

    def handler(req):
        page = int(req.url.params["page"])
        pages.append(page)
        assert "since" not in req.url.params
        ids = [f"{i:026d}" for i in range(total - 1 - page * 200, max(-1, total - 1 - (page + 1) * 200), -1)]
        posts = {i: {"id": i, "create_at": int(i), "channel_id": CH} for i in ids}
        posts["0" * 25 + "r"] = {"id": "0" * 25 + "r", "create_at": 0, "channel_id": CH}  # an old thread root
        return httpx.Response(200, json={"order": ids, "posts": posts})

    got = await _client(handler).posts_after(100, channel_id=CH)
    assert [p["create_at"] for p in got] == list(range(100, total))
    assert pages == [0, 1]  # page 1 reaches past the window

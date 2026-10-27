"""Ids and names an agent or a hook passes never reach a Mattermost API path
unchecked (#81): a bad one is refused before any request."""

from __future__ import annotations

import json

import httpx
import pytest

from mattermost_ai_agent_bus import hookapi, server
from mattermost_ai_agent_bus.client import InvalidId, MattermostAdapter, mm_id, mm_name

ME = "m" * 26
BAD = ["../users/me", "abc", "%2e%2e", "a" * 12 + "/" + "b" * 13, "A" * 26, "a" * 25 + "\n", ""]


def _client(seen: list[str]) -> MattermostAdapter:
    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(200, json={})

    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    c._me = {"id": ME, "username": "agent-me"}
    return c


def test_ids_and_names():
    assert mm_id("a1" * 13, "post") == "a1" * 13
    for bad in BAD:
        with pytest.raises(InvalidId, match="invalid post id"):
            mm_id(bad, "post")
    for good in ("ai-agents", "agents-stats", "agent.x_1", f"{ME}__{'n' * 26}", "Town-Square"):
        assert mm_name(good, "channel") == good
    for bad in ("..", "a/b", "a..b", "a?b", "a#b", "a%2f", "a b", "-x", "", "x" * 65):
        with pytest.raises(InvalidId, match="invalid channel name"):
            mm_name(bad, "channel")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD[:-1])
async def test_the_client_refuses_before_any_request(bad):
    seen: list[str] = []
    c = _client(seen)
    calls = [lambda: c.thread(bad), lambda: c.reactions(bad), lambda: c.add_reaction(bad, "x"),
             lambda: c.list_recent(channel_id=bad), lambda: c.channel_posts(bad),
             lambda: c.post("hi", root_id=bad), lambda: c.post("hi", channel_id=bad),
             lambda: c.get_channel(bad), lambda: c.username(bad), lambda: c.view_channel(bad),
             lambda: c.set_presence(bad, "online"), lambda: c.direct_channel_with(bad)]
    for call in calls:
        with pytest.raises(InvalidId):
            await call()
    with pytest.raises(InvalidId):
        await c.get_channel_id("../x")
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", BAD[:4])
async def test_the_tools_refuse_a_bad_id(bad, monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(server, "_get_client", lambda: _client(seen))
    for out in (await server.get_thread(bad), await server.reply_in_thread(bad, "ACK · ok"),
                await server.list_recent(channel_id=bad), await server.resolve_ask(bad),
                await server.post_message("hi", channel_id=bad),
                await server.post_message("hi", root_id=bad),
                await server.post_message("hi", to="../agent-x"),
                await server.ask_human("ok?", human="dembaca", root_id=bad)):
        assert json.loads(out)["refused"].startswith("invalid ")
    assert seen == []


@pytest.mark.asyncio
async def test_the_hook_api_answers_400_before_any_request(monkeypatch):
    seen: list[str] = []
    client = _client(seen)
    monkeypatch.setenv("MM_PROJECT_CHANNEL_ID", "../users/me")

    async def get():
        return client

    api = hookapi.HookAPI(get)
    await api.start(write_file=False)
    try:
        async with httpx.AsyncClient(base_url=api.endpoint,
                                     headers={"Authorization": f"Bearer {api.key}"}) as h:
            for route in ("/v1/posts", "/v1/last-post"):
                r = await h.get(route)
                assert (r.status_code, r.json()) == (400, {"error": "invalid channel id"})
    finally:
        await api.close()
    assert seen == []

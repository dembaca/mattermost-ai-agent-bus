"""The whole MCP server on stand-ins (arc42 §5.2, the acceptance of phase 2 of
the NATS move, ADR-014): no Mattermost, no registrar, no network. Only the
composition root is patched (compose.client_from_env hands out a FakeBus,
compose.event_source a FakeEvents); the rest runs through the server's own
entry points: its tools, the WebSocket inbox it builds, the hook API and the
exit. A second chat adapter is chosen in compose.py and leaves all of this
as it is.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fakes import BOT, WORK, FakeBus, FakeEvents, FakeRegistrar

from mattermost_ai_agent_bus import asks, compose, hookapi, ownpost, server, stats
from mattermost_ai_agent_bus.events import ReactionEvent
from mattermost_ai_agent_bus.model import Reaction

HUMAN = "h" * 26
MENTION = "m" * 26


@pytest.fixture
async def wired(monkeypatch):
    """The server's module state as at its start, and the composition root
    handing out the stand-ins: (FakeBus, FakeEvents, what it built)."""
    bus = FakeBus(FakeRegistrar(), trusted={HUMAN: "dembaca"}, names={HUMAN: "dembaca"})
    source = FakeEvents(connected=False, held=True)  # open until closed, as a socket
    built: list[str] = []

    def client_from_env():
        built.append("client")
        return bus

    def event_source(client):
        assert client is bus
        built.append("events")
        return source

    monkeypatch.setattr(compose, "client_from_env", client_from_env)
    monkeypatch.setattr(compose, "event_source", event_source)
    # As a fresh server: no client, no inbox; the client named itself and
    # its roots are known, so the first _get_client looks for a kept bot (RT-6.10).
    for name, value in (("STALE", ""), ("_client", None), ("_inbox", None), ("_client_named", True),
                        ("_adopt_tried", False), ("_roots_pending", False), ("_exit_handled", False),
                        ("_confirmed", None), ("_hook_api", None)):
        monkeypatch.setattr(server, name, value)
    monkeypatch.setattr(server, "corral_sandbox", lambda: None)
    monkeypatch.setattr(stats, "_uid", "")
    monkeypatch.delenv("MM_RESPONSIBLE_HUMAN", raising=False)
    yield bus, source, built
    if server._inbox is not None:
        await server._inbox.close()


async def test_the_whole_server_runs_on_stand_ins(wired):
    """Phase 2 acceptance: session start, posts, a trusted human's mention, an
    ask and its answer, the hook API and the exit, all on the stand-ins."""
    bus, source, built = wired

    # 1. Session start (RT-6.1): no kept bot to take over (RT-6.10), so a new one
    # is registered and set up; the stats and the inbox know it.
    out = json.loads(await server.session_start("x"))
    assert out["user_id"] == BOT and out["username"] == "agent-agent-x" and "reused" not in out
    assert bus.registrar.calls == ["adopt_sync", "session_start x", "finish_session_setup"]
    assert built == ["client"] and stats._uid == BOT

    # 2. Posting (RT-6.14): the bus word first, the props as the server writes them.
    hello = json.loads(await server.post_message("starting on the parser", kind="HELLO"))
    assert bus.posted[-1] == {"message": "`HELLO` · starting on the parser", "channel_id": None,
                              "root_id": None, "props": {"a2a": {"v": 1, "kind": "hello", "intent": "fyi"}}}
    ack = json.loads(await server.reply_in_thread(hello["id"], "on it", kind="ACK"))
    assert ack["root_id"] == hello["id"]
    assert bus.posted[-1]["message"] == "`ACK` · on it" and bus.posted[-1]["root_id"] == hello["id"]
    assert bus.posted[-1]["props"]["a2a"]["kind"] == "ack"

    # 3. A trusted human's mention (RT-6.16) comes from the event source the
    # inbox got from the composition root, and is delivered as a mention.
    source.push(bus.arrive({"id": MENTION, "user_id": HUMAN,
                            "message": "@agent-agent-x please review #12"}))
    items = json.loads(await server.wait_for_events(timeout_sec=2))
    assert [(i["kind"], i["post"]["id"], i["from_trusted_human"], i["from_agent"], i["trusted_human"])
            for i in items] == [("mention", MENTION, True, False, "dembaca")]
    assert built == ["client", "events"] and source.opens == 1
    assert server._inbox is not None and server._inbox.client is bus

    # 4. An ask (RT-6.11) and its answer, that human's thumbs-up on the open
    # stream, delivered as the answer; resolve_ask closes the ask.
    ask = json.loads(await server.ask_human(need="Merge #12?", answers=["go", "no-go"]))
    assert bus.posted[-1]["message"] == "\U0001F64B @dembaca"
    assert bus.posted[-1]["props"]["a2a"]["kind"] == "ask" and asks.open_asks(BOT) == [ask["ask"]]
    source.push(ReactionEvent(Reaction(user_id=HUMAN, post_id=ask["ask"], emoji_name="+1",
                                       create_at=bus.clock + 1), WORK))
    items = json.loads(await server.wait_for_events(timeout_sec=2))
    assert [(i["kind"], i["ask"], i["answer"], i["chosen"], i["from_trusted_human"]) for i in items] == [
        ("reaction", ask["ask"], "yes", "go", True)]
    assert source.opens == 1 and source.connected
    assert json.loads(await server.resolve_ask(ask["ask"], note="merging")) == {
        "ask": ask["ask"], "was_open": True}
    assert [r.emoji_name for r in await bus.reactions(ask["ask"])] == ["white_check_mark"]
    assert asks.open_asks(BOT) == [] and bus.posted[-1]["root_id"] == ask["ask"]

    # 5. The hook API (RT-6.17) answers from the same stand-ins, through the
    # server's own client getter: a mention that came while nobody waited
    # counts, the last own post is the note, the handoff names the posts.
    bus.arrive({"id": "n" * 26, "user_id": HUMAN, "message": "@agent-agent-x and #13?"})
    api = hookapi.HookAPI(server._bus_client)
    await api.start(write_file=False)
    try:
        async with httpx.AsyncClient(base_url=api.endpoint,
                                     headers={"Authorization": f"Bearer {api.key}"}) as h:
            assert (await h.get("/v1/pending")).json() == {"count": 1}
            last = (await h.get("/v1/last-post")).json()
            assert last == {"at": bus.posts[1].create_at, "message": "merging", "signed_off": False}
            context = (await h.get("/v1/handoff", params={"runtime": "claude-code"})).json()["context"]
            assert "`ACK` · on it" in context and f"root {hello['id']}" in context
    finally:
        await api.close()

    # 6. The exit (RT-6.10): BYE in the HELLO thread, the stats, then the bot
    # unregistered through the registration port.
    server._cleanup_on_exit()
    bye, stats_post = bus.sent_sync
    assert bye["message"].startswith("`BYE`") and bye["root_id"] == hello["id"]
    assert bye["channel_id"] == WORK and bye["props"]["a2a"]["kind"] == "bye"
    assert stats_post["message"].startswith("STATS ")
    assert bus.registrar.calls[-1] == "shutdown_sync" and bus.session is None
    assert ownpost.read(BOT)["MSG"] == bye["message"]

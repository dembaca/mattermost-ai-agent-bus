"""RT-6.12: bus events pushed into the session as a Claude Code channel."""

import asyncio

import anyio
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp_types import INVALID_REQUEST, JSONRPCNotification, JSONRPCRequest
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from mattermost_ai_agent_bus import channel

MENTION = {
    "kind": "mention",
    "from_trusted_human": True,
    "trusted_human": "dembaca",
    "post": {"id": "p1", "root_id": "r1", "channel": "agents", "author": "dembaca",
             "at": "2026-10-20 09:00", "message": "@agent please look at #46"},
}


def test_enabled_only_when_the_human_says_so(monkeypatch):
    monkeypatch.delenv("MM_CHANNEL_PUSH", raising=False)
    assert not channel.enabled()
    monkeypatch.setenv("MM_CHANNEL_PUSH", "1")
    assert channel.enabled()
    monkeypatch.setenv("MM_CHANNEL_PUSH", "0")
    assert not channel.enabled()


def test_notification_carries_message_and_routing():
    params = channel.notification(MENTION)
    assert params["content"] == "@agent please look at #46"
    meta = params["meta"]
    assert meta == {"kind": "mention", "post_id": "p1", "root_id": "r1", "channel": "agents",
                    "author": "dembaca", "at": "2026-10-20 09:00",
                    "from_trusted_human": "true", "trusted_human": "dembaca"}
    # Claude Code drops attributes whose key is not an identifier, and wants strings.
    assert all(k.isidentifier() and isinstance(v, str) for k, v in meta.items())


def test_reaction_keeps_its_answer():
    item = {"kind": "reaction", "from_trusted_human": False, "ask": "a1", "emoji": "+1",
            "answer": "yes", "post": {"id": "a1:u:+1", "message": ":+1:"}}
    meta = channel.notification(item)["meta"]
    assert meta["from_trusted_human"] == "false"
    assert (meta["ask"], meta["emoji"], meta["answer"]) == ("a1", "+1", "yes")


async def _send(stream, msg):
    await stream.send(SessionMessage(message=msg))


async def _recv(stream):
    with anyio.fail_after(5):
        while True:
            item = await stream.receive()
            if isinstance(item, SessionMessage):
                return item.message.model_dump(by_alias=True, exclude_none=True)


@pytest.mark.anyio
async def test_handshake_era_channel_pushes_events():
    """A 2026-07-28 probe is refused, so the client falls back to initialize —
    the only era Claude Code registers a channel on — and events then arrive
    as notifications/claude/channel."""
    low = MCPServer("t")._lowlevel_server
    opts = low.create_initialization_options(experimental_capabilities={channel.CAPABILITY: {}})
    pusher = channel.Pusher()
    queued = [MENTION]

    async def wait():
        if queued:
            return [queued.pop()]
        await asyncio.sleep(3600)
        return []

    async with create_client_server_memory_streams() as ((c_read, c_write), (s_read, s_write)):
        async with anyio.create_task_group() as tg:
            async def serve():
                await channel.serve_handshake_only(
                    low, s_read, s_write, lifespan_state={}, init_options=opts,
                    pusher=pusher, on_connected=lambda: pusher.run(wait))
            tg.start_soon(serve)

            await _send(c_write, JSONRPCRequest(jsonrpc="2.0", id=1, method="server/discover", params={
                "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}))
            refused = await _recv(c_read)
            assert refused["error"]["code"] == INVALID_REQUEST

            await _send(c_write, JSONRPCRequest(jsonrpc="2.0", id=2, method="initialize", params={
                "protocolVersion": HANDSHAKE_PROTOCOL_VERSIONS[-1], "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": "test"}}))
            init = (await _recv(c_read))["result"]
            assert init["protocolVersion"] in HANDSHAKE_PROTOCOL_VERSIONS
            assert init["capabilities"]["experimental"] == {channel.CAPABILITY: {}}

            # Nothing is pushed before the client says it is ready.
            await _send(c_write, JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized"))
            pushed = await _recv(c_read)
            assert pushed["method"] == channel.METHOD
            assert pushed["params"] == channel.notification(MENTION)
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_pusher_waits_for_the_bus():
    """No bus yet (before session_start, or a dropped socket): retry, don't die."""
    pusher = channel.Pusher()
    sent = []

    class Conn:
        initialized = anyio.Event()

        async def notify(self, method, params):
            sent.append((method, params))

    pusher.connection = Conn()
    Conn.initialized.set()
    calls = 0

    async def wait():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("no bot token")
        if calls == 2:
            return [MENTION]
        await asyncio.sleep(3600)

    with anyio.move_on_after(0.5):
        await pusher.run(wait, retry_sec=0.01)
    assert calls >= 2
    assert sent == [(channel.METHOD, channel.notification(MENTION))]

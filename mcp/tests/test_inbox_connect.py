"""RT-6.17 step 7 (ADR-009 phase 2): the inbox's socket opens one connect at a
time (wait_for_events, the channel push and the wake long poll may all ask),
and a connect that is cancelled or fails half way leaves nothing that reads as
connected."""

import asyncio

import pytest

from mattermost_ai_agent_bus import websocket_inbox
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

from fakes import HookClient


class Socket:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.fixture
def inbox(monkeypatch):
    client = HookClient(uid="u" * 26)
    client.token = "t"
    client.base_url = "https://mm.example"
    box = WebSocketInbox(client=client)
    sockets = []

    async def ws_connect(url):
        sockets.append(Socket())
        return sockets[-1]

    async def identity():
        return None

    monkeypatch.setattr(websocket_inbox, "ws_connect", ws_connect)
    monkeypatch.setattr(box, "ensure_identity", identity)
    box.sockets = sockets
    return box


async def test_concurrent_callers_open_one_socket(inbox, monkeypatch):
    opened = asyncio.Event()

    async def slow_open():
        await opened.wait()

    monkeypatch.setattr(inbox, "_open", slow_open)
    tasks = [asyncio.create_task(inbox.ensure_connected()) for _ in range(3)]
    await asyncio.sleep(0.05)
    assert len(inbox.sockets) == 1
    opened.set()
    await asyncio.gather(*tasks)
    assert len(inbox.sockets) == 1 and inbox.connected


async def test_a_cancelled_connect_leaves_nothing_half_open(inbox, monkeypatch):
    """A long poll's deadline cancels its check, and the connect with it."""
    never = asyncio.Event()

    async def hang():
        await never.wait()

    monkeypatch.setattr(inbox, "_open", hang)
    task = asyncio.create_task(inbox.ensure_connected())
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not inbox.connected and inbox.sockets[0].closed

    async def fine():
        return None

    monkeypatch.setattr(inbox, "_open", fine)
    await inbox.ensure_connected()  # the next call connects afresh
    assert inbox.connected and len(inbox.sockets) == 2


async def test_a_failed_connect_leaves_it_unconnected(inbox, monkeypatch):
    async def boom():
        raise OSError("down")

    monkeypatch.setattr(inbox, "_open", boom)
    with pytest.raises(OSError):
        await inbox.ensure_connected()
    assert not inbox.connected and inbox.sockets[0].closed


async def test_wait_for_events_waits_for_a_connect_under_way(inbox, monkeypatch):
    """A long poll's connect sets the socket early; wait_for_events must not
    take that for a finished connect, or a connect its deadline cancels leaves
    it waiting on a queue nobody fills."""
    release = asyncio.Event()

    async def slow_open():
        await release.wait()

    monkeypatch.setattr(inbox, "_open", slow_open)
    connecting = asyncio.create_task(inbox.ensure_connected())
    await asyncio.sleep(0.05)
    assert inbox._ws is not None and not connecting.done()
    waiter = asyncio.create_task(inbox.wait_for_events(timeout_sec=0.2))
    await asyncio.sleep(0.05)
    connecting.cancel()  # the long poll's deadline
    await asyncio.gather(connecting, return_exceptions=True)
    release.set()
    await waiter
    # It connected afresh itself (two sockets in all) rather than wait on a dead one.
    assert len(inbox.sockets) == 2 and inbox.connected

"""RT-6.17 step 7 (ADR-009 phase 2): the inbox's socket opens one connect at a
time (wait_for_events, the channel push and the wake long poll may all ask),
and a connect that is cancelled or fails half way leaves nothing that reads as
connected."""

import asyncio

import pytest

from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

from fakes import FakeEvents, HookClient


@pytest.fixture
def inbox(monkeypatch):
    """An inbox over an event source that is not connected: each open() is
    one connection (events.opens), close() closes it (events.closed)."""
    client = HookClient(uid="u" * 26)
    client.token = "t"
    events = FakeEvents(connected=False)
    box = WebSocketInbox(client=client, source=events)

    async def identity():
        return None

    monkeypatch.setattr(box, "ensure_identity", identity)
    box.events = events
    return box


async def test_concurrent_callers_open_one_socket(inbox, monkeypatch):
    opened = asyncio.Event()

    async def slow_open():
        await opened.wait()

    monkeypatch.setattr(inbox, "_open", slow_open)
    tasks = [asyncio.create_task(inbox.ensure_connected()) for _ in range(3)]
    await asyncio.sleep(0.05)
    assert inbox.events.opens == 1
    opened.set()
    await asyncio.gather(*tasks)
    assert inbox.events.opens == 1 and inbox.connected


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
    assert not inbox.connected and inbox.events.closed

    async def fine():
        return None

    monkeypatch.setattr(inbox, "_open", fine)
    await inbox.ensure_connected()  # the next call connects afresh
    assert inbox.connected and inbox.events.opens == 2


async def test_a_failed_connect_leaves_it_unconnected(inbox, monkeypatch):
    async def boom():
        raise OSError("down")

    monkeypatch.setattr(inbox, "_open", boom)
    with pytest.raises(OSError):
        await inbox.ensure_connected()
    assert not inbox.connected and inbox.events.closed


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
    assert inbox.connected and not connecting.done()
    waiter = asyncio.create_task(inbox.wait_for_events(timeout_sec=0.2))
    await asyncio.sleep(0.05)
    connecting.cancel()  # the long poll's deadline
    await asyncio.gather(connecting, return_exceptions=True)
    release.set()
    await waiter
    # It connected afresh itself (two connections in all) rather than wait on a dead one.
    assert inbox.events.opens == 2 and inbox.connected


async def test_a_reader_that_closes_slowly_leaves_the_next_readers_handle(inbox, monkeypatch):
    """A stream whose close waits (the socket's close handshake): the next
    read connects meanwhile, and the old reader, ending after it, does not
    clear the new connection's reader, which close() then still stops."""
    release = asyncio.Event()
    streams: list[str] = []

    class Stream:
        def __init__(self, first: bool) -> None:
            self.first = first

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.first:   # the first connection ends at once
                inbox.events.connected = False
                raise StopAsyncIteration
            await asyncio.Event().wait()   # the second stays open

        async def aclose(self) -> None:
            if self.first:
                await release.wait()

    def events():
        streams.append("s")
        return Stream(len(streams) == 1)

    async def no_backfill():
        return None

    monkeypatch.setattr(inbox.events, "events", events)
    monkeypatch.setattr(inbox, "_backfill", no_backfill)
    await inbox.ensure_connected()
    for _ in range(20):
        await asyncio.sleep(0)
    assert not inbox.connected   # the first stream ended; its close still waits
    await inbox.ensure_connected()
    reader = inbox._reader
    for _ in range(20):
        await asyncio.sleep(0)
    assert reader is not None and len(streams) == 2
    release.set()
    for _ in range(20):
        await asyncio.sleep(0)
    assert inbox._reader is reader and not reader.done()
    await inbox.close()
    assert reader.done()

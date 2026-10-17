"""RT-6.5 typing indicator while a mention is answered (docs/arc42/06-runtime-view.md)."""

import asyncio
import json

import pytest

from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent, WebSocketInbox


class _FakeWs:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def typing(self) -> list[dict]:
        return [m["data"] for m in self.sent if m["action"] == "user_typing"]


class _FakeClient:
    async def view_channel(self, cid):
        return None


def _inbox(**kw) -> tuple[WebSocketInbox, _FakeWs]:
    inbox = WebSocketInbox(client=_FakeClient(), typing_interval=0.01, **kw)
    ws = _FakeWs()
    inbox._ws = ws
    return inbox, ws


async def _deliver(inbox, kind, post):
    await inbox._queue.put(ClassifiedEvent(kind=kind, post=post))
    return await inbox.wait_for_events(timeout_sec=1)


@pytest.mark.asyncio
async def test_mention_shows_typing_in_its_thread_until_next_wait():
    """RT-6.5 steps 1-3: typing in the thread, re-sent, stopped by the next wait."""
    inbox, ws = _inbox()
    await _deliver(inbox, "mention", {"id": "p1", "channel_id": "c1", "root_id": "r1"})
    await asyncio.sleep(0.05)
    sent = ws.typing()
    assert len(sent) >= 2
    assert sent[0] == {"channel_id": "c1", "parent_id": "r1"}
    assert await inbox.wait_for_events(timeout_sec=0.01) == []
    n = len(ws.typing())
    await asyncio.sleep(0.05)
    assert len(ws.typing()) == n


@pytest.mark.asyncio
async def test_top_level_post_uses_itself_as_thread():
    inbox, ws = _inbox()
    await _deliver(inbox, "dm", {"id": "p2", "channel_id": "d1"})
    await asyncio.sleep(0)
    assert ws.typing()[0] == {"channel_id": "d1", "parent_id": "p2"}
    inbox.stop_typing()


@pytest.mark.asyncio
async def test_watched_channel_post_promises_nothing():
    """RT-6.5: a post that may need no answer does not show typing."""
    inbox, ws = _inbox()
    await _deliver(inbox, "watched_channel", {"id": "p3", "channel_id": "c1"})
    await asyncio.sleep(0.03)
    assert ws.typing() == []


@pytest.mark.asyncio
async def test_typing_ends_on_its_own_and_can_be_off():
    """RT-6.5 error handling: no answer → the indicator stops after typing_sec."""
    inbox, ws = _inbox(typing_sec=0.03)
    await _deliver(inbox, "mention", {"id": "p4", "channel_id": "c1"})
    await asyncio.sleep(0.08)
    n = len(ws.typing())
    await asyncio.sleep(0.05)
    assert n > 0 and len(ws.typing()) == n

    off, ws_off = _inbox(typing_sec=0)
    await _deliver(off, "mention", {"id": "p5", "channel_id": "c1"})
    await asyncio.sleep(0.03)
    assert ws_off.typing() == []


@pytest.mark.asyncio
async def test_post_stops_typing(monkeypatch):
    """RT-6.5 step 3: the answer itself ends the indicator."""
    from mattermost_ai_agent_bus import server

    inbox, ws = _inbox()

    class _Client:
        async def post(self, message, channel_id=None, root_id=None):
            return {"id": "new"}

    monkeypatch.setattr(server, "_inbox", inbox)
    monkeypatch.setattr(server, "_get_client", lambda: _Client())
    await _deliver(inbox, "mention", {"id": "p6", "channel_id": "c1"})
    await server.reply_in_thread(root_id="p6", message="ACK")
    n = len(ws.typing())
    await asyncio.sleep(0.05)
    assert len(ws.typing()) == n

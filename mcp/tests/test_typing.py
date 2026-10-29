"""RT-6.5 typing indicator while a mention is answered (docs/arc42/06-runtime-view.md)."""

import asyncio
import json

import pytest

from fakes import WORK, FakeChat
from mattermost_ai_agent_bus.client import post_of
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
    await inbox._queue.put(ClassifiedEvent(kind=kind, post=post_of(post)))
    return await inbox.wait_for_events(timeout_sec=1)


@pytest.mark.asyncio
async def test_mention_shows_typing_in_its_thread_until_next_wait():
    """RT-6.5 steps 1-3: typing in the thread, re-sent, stopped by the next wait."""
    inbox, ws = _inbox()
    await _deliver(inbox, "mention", {"id": "p1", "channel_id": "c1", "root_id": "r1"})
    await asyncio.sleep(0.05)
    sent = ws.typing()
    assert len(sent) >= 4
    # Thread view and channel view each show only their own key.
    assert sent[:2] == [
        {"channel_id": "c1", "parent_id": "r1"},
        {"channel_id": "c1", "parent_id": ""},
    ]
    assert await inbox.wait_for_events(timeout_sec=0.01) == []
    n = len(ws.typing())
    await asyncio.sleep(0.05)
    assert len(ws.typing()) == n


@pytest.mark.asyncio
async def test_top_level_post_uses_itself_as_thread():
    inbox, ws = _inbox()
    await _deliver(inbox, "dm", {"id": "p2", "channel_id": "d1"})
    await asyncio.sleep(0)
    assert ws.typing()[:2] == [
        {"channel_id": "d1", "parent_id": "p2"},
        {"channel_id": "d1", "parent_id": ""},
    ]
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
        async def post(self, message, channel_id=None, root_id=None, props=None):
            return post_of({"id": "new"})

    monkeypatch.setattr(server, "_inbox", inbox)
    monkeypatch.setattr(server, "_get_client", lambda: _Client())
    await _deliver(inbox, "mention", {"id": "p6", "channel_id": "c1"})
    await server.reply_in_thread(root_id="p6", message="ACK")
    n = len(ws.typing())
    await asyncio.sleep(0.05)
    assert len(ws.typing()) == n


# --- RT-6.5: the 👑 shows typing for a human's plain post (0.52.0) ----------------------

def _item(**kw) -> dict:
    return {"kind": "watched_channel", "from_trusted_human": True, "from_agent": False} | kw


@pytest.fixture
def crowned(monkeypatch):
    """A server holding the crown, its inbox connected to a fake socket."""
    from mattermost_ai_agent_bus import lease, server

    inbox, ws = _inbox()
    crown = {"held": True}
    monkeypatch.setattr(server, "_get_inbox", lambda: inbox)
    monkeypatch.setattr(server, "_get_client", lambda: FakeChat())

    async def my_id(_client):
        return "me"

    monkeypatch.setattr(server, "_my_id", my_id)
    monkeypatch.setattr(lease, "crown_held", lambda uid: crown["held"])
    yield server, inbox, ws, crown
    inbox.stop_typing()


@pytest.mark.parametrize("path", ["wait_for_events", "push"])
@pytest.mark.asyncio
async def test_a_humans_plain_post_shows_the_crown_typing_in_both_paths(crowned, monkeypatch, path):
    """RT-6.5 / RT-6.16: a human's unmentioned post wakes only the 👑 as its
    task, so it shows typing in that thread, from wait_for_events and from the
    channel push (RT-6.12)."""
    server, inbox, ws, _ = crowned

    async def items(events, raw=False):
        return [_item() for _ in events]

    async def nothing(payload):
        return None

    monkeypatch.setattr(server, "_event_items", items)
    monkeypatch.setattr(server, "_compact_bystander", nothing)
    await inbox._queue.put(ClassifiedEvent(
        kind="watched_channel", post=post_of({"id": "p7", "channel_id": WORK, "root_id": "r7"})))
    if path == "push":
        await server._push_wait(1)
    else:
        await server.wait_for_events(timeout_sec=1)
    await asyncio.sleep(0)
    assert ws.typing()[:2] == [{"channel_id": WORK, "parent_id": "r7"}, {"channel_id": WORK, "parent_id": ""}]


@pytest.mark.asyncio
async def test_a_pushed_mention_shows_typing():
    """RT-6.5 / RT-6.12: the pusher's read (agent=False) starts it like the agent's."""
    inbox, ws = _inbox()
    await inbox._queue.put(ClassifiedEvent(kind="mention", post=post_of({"id": "p8", "channel_id": "c1"})))
    await inbox.wait_for_events(timeout_sec=1, agent=False)
    await asyncio.sleep(0)
    assert ws.typing()[:1] == [{"channel_id": "c1", "parent_id": "p8"}]
    inbox.stop_typing()


@pytest.mark.parametrize(("item", "post", "held"), [
    (_item(), {}, False),                                   # no crown: not its task
    (_item(gated="from visitor, not trusted by me"), {}, True),
    (_item(from_agent=True, from_trusted_human=False), {}, True),
    (_item(from_agent=True, intent="fyi"), {}, True),
    (_item(), {"message": "@agent-bob can you look?"}, True),  # bob's: the 👑 only routes it
    (_item(), {"message": "@channel deploy at 5"}, True),
    (_item(), {"type": "system_join_channel"}, True),
    (_item(kind="mention"), {}, True),                      # the inbox's own rule
])
@pytest.mark.asyncio
async def test_no_typing_for_what_is_no_task_of_the_crown(crowned, monkeypatch, item, post, held):
    """RT-6.5: gated, fyi, an agent's, naming an agent or everyone, a system
    post, or no crown: no indicator from this rule."""
    server, _, ws, crown = crowned
    monkeypatch.setenv("MM_AGENT_PREFIX", "agent-")
    crown["held"] = held
    event = ClassifiedEvent(kind=item["kind"], gated=item.get("gated", ""),
                            post=post_of({"id": "p9", "channel_id": "c1", "message": "hi"} | post))
    await server._typing_for_task([event], [item])
    await asyncio.sleep(0)
    assert ws.typing() == []

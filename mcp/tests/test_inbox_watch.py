"""Which channels the inbox watches (no live Mattermost)."""

import pytest

from mattermost_ai_agent_bus.model import UnreadChannel
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox


class _FakeClient:
    """Minimal stand-in for MattermostClient.ensure_identity's needs."""

    def __init__(self, channel_name="agents", project_channel_name="", missing=()):
        self.channel_name = channel_name
        self.project_channel_name = project_channel_name
        self.missing = set(missing)
        self.resolved: list[str] = []

    async def get_me(self):
        return {"id": "bot1", "username": "agent-test"}

    async def get_channel_id(self, name=None):
        name = name or (self.project_channel_name or self.channel_name)
        self.resolved.append(name)
        if name in self.missing:
            raise RuntimeError(f"channel {name!r} does not exist")
        return f"cid-{name}"


@pytest.mark.asyncio
async def test_watches_default_channel_only():
    client = _FakeClient()
    inbox = WebSocketInbox(client=client)
    await inbox.ensure_identity()
    assert inbox.watched_channel_ids == {"cid-agents"}


@pytest.mark.asyncio
async def test_watches_default_and_project_channel():
    """Work happens in the project channel; the agent stays addressable in both."""
    client = _FakeClient(project_channel_name="proj-bus")
    inbox = WebSocketInbox(client=client)
    await inbox.ensure_identity()
    assert inbox.watched_channel_ids == {"cid-agents", "cid-proj-bus"}
    assert client.resolved == ["agents", "proj-bus"]


@pytest.mark.asyncio
async def test_missing_project_channel_does_not_lose_the_default():
    """A bad MM_PROJECT_CHANNEL degrades to the default channel, not to nothing."""
    client = _FakeClient(project_channel_name="typo", missing={"typo"})
    inbox = WebSocketInbox(client=client)
    await inbox.ensure_identity()
    assert inbox.watched_channel_ids == {"cid-agents"}


@pytest.mark.asyncio
async def test_explicit_watch_list_is_not_overridden():
    client = _FakeClient(project_channel_name="proj-bus")
    inbox = WebSocketInbox(client=client, watched_channel_ids={"preset"})
    await inbox.ensure_identity()
    assert inbox.watched_channel_ids == {"preset"}
    assert client.resolved == []


class _DeadSocket:
    """A socket whose read loop ends, as on a dropped connection."""

    def __init__(self):
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_dead_socket_is_dropped_so_the_next_wait_reconnects():
    """Regression: a dropped WebSocket left _ws set, so the inbox went silently
    deaf — every later wait_for_events timed out with nothing feeding the queue."""
    import asyncio

    inbox = WebSocketInbox(client=_FakeClient(), watched_channel_ids={"c"})
    inbox._ws = _DeadSocket()
    await inbox._read_loop()
    assert inbox._ws is None, "dead socket must be cleared to allow a reconnect"
    assert inbox._reader is None
    del asyncio


@pytest.mark.asyncio
async def test_close_still_closes_the_socket():
    """The reconnect fix must not stop close() from closing a live socket."""
    inbox = WebSocketInbox(client=_FakeClient())
    sock = _DeadSocket()
    inbox._ws = sock
    await inbox.close()
    assert sock.closed is True
    assert inbox._ws is None


class _BackfillClient(_FakeClient):
    """Client with history the WebSocket never saw."""

    def __init__(self, posts, **kw):
        super().__init__(**kw)
        self.posts = posts
        self.viewed: list[str] = []

    async def unread_channels(self):
        return [UnreadChannel("dm1", "D", since=100)]

    async def list_recent(self, channel_id=None, per_page=20, since_ms=None):
        return [p for p in self.posts if p["channel_id"] == channel_id]

    async def view_channel(self, channel_id):
        self.viewed.append(channel_id)


def _post(pid, msg="hi", user="human1", channel="dm1"):
    return {"id": pid, "user_id": user, "channel_id": channel, "message": msg}


@pytest.mark.asyncio
async def test_messages_sent_before_connect_are_still_delivered():
    """Regression: the socket streams from connect time, so anything sent
    earlier in the session was invisible to the queue while the REST counters
    still reported it — the Stop hook blocked on an undeliverable message."""
    client = _BackfillClient([_post("p1", "sent while nobody was listening")])
    inbox = WebSocketInbox(client=client, watched_channel_ids={"c"})
    inbox._bot_user_id, inbox._bot_username = "bot1", "agent-test"
    inbox._ws = _DeadSocket()  # already "connected": do not dial out
    await inbox._backfill()
    events = await inbox.wait_for_events(timeout_sec=1)
    assert [e.post["id"] for e in events] == ["p1"]
    assert events[0].kind == "dm"


@pytest.mark.asyncio
async def test_backfill_does_not_deliver_a_post_twice():
    """Backfill and the live stream overlap at connect time."""
    client = _BackfillClient([_post("p1")])
    inbox = WebSocketInbox(client=client, watched_channel_ids={"c"})
    inbox._bot_user_id, inbox._bot_username = "bot1", "agent-test"
    inbox._ws = _DeadSocket()  # already "connected": do not dial out
    await inbox._backfill()
    await inbox._backfill()
    events = await inbox.wait_for_events(timeout_sec=1)
    assert len(events) == 1


@pytest.mark.asyncio
async def test_backfill_ignores_own_posts():
    """A bot's own welcome DM counts as unread for it but can never be for it."""
    client = _BackfillClient([_post("p1", user="bot1")])
    inbox = WebSocketInbox(client=client, watched_channel_ids={"c"})
    inbox._bot_user_id, inbox._bot_username = "bot1", "agent-test"
    inbox._ws = _DeadSocket()  # already "connected": do not dial out
    await inbox._backfill()
    assert await inbox.wait_for_events(timeout_sec=0.2) == []


@pytest.mark.asyncio
async def test_newer_wait_supersedes_pending_one():
    """RT-6.2 step 4: a backgrounded listener must not swallow the next event."""
    import asyncio

    from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent

    inbox = WebSocketInbox(client=_FakeClient())
    inbox._ws = object()  # pretend connected
    stale = asyncio.create_task(inbox.wait_for_events(timeout_sec=30))
    await asyncio.sleep(0)
    fresh = asyncio.create_task(inbox.wait_for_events(timeout_sec=30))
    await asyncio.sleep(0)
    assert await stale == []
    await inbox._queue.put(ClassifiedEvent(kind="mention", post={"id": "p1"}))
    events = await fresh
    assert [e.post["id"] for e in events] == ["p1"]


@pytest.mark.asyncio
async def test_wait_times_out_empty():
    inbox = WebSocketInbox(client=_FakeClient())
    inbox._ws = object()
    assert await inbox.wait_for_events(timeout_sec=0.01) == []
    assert inbox._queue.qsize() == 0


class _FreshBotClient(_BackfillClient):
    """A bot that just joined: read from its creation (client.unread_channels)."""

    def __init__(self, posts, **kw):
        super().__init__(posts, **kw)
        self.since: list = []

    async def unread_channels(self):
        return [UnreadChannel("proj", since=5000)]  # the bot's create_at

    async def list_recent(self, channel_id=None, per_page=20, since_ms=None):
        self.since.append(since_ms)
        return [p for p in self.posts if p["channel_id"] == channel_id
                and p.get("create_at", 0) >= (since_ms or 0)]


@pytest.mark.asyncio
async def test_backfill_never_reads_before_the_bot_existed():
    """RT-6.2: a fresh bot was handed the project channel's last 50 posts of
    history, oldest first, and the mention that woke it was cut off."""
    old = [dict(_post(f"old{i}", channel="proj"), create_at=1000 + i) for i in range(20)]
    new = dict(_post("ask", "@agent-test hast du das gesehen?", channel="proj"), create_at=9000)
    client = _FreshBotClient(old + [new])
    inbox = WebSocketInbox(client=client, watched_channel_ids={"proj"})
    inbox._bot_user_id, inbox._bot_username = "bot1", "agent-test"
    inbox._ws = _DeadSocket()
    await inbox._backfill()
    assert client.since == [5000]
    events = await inbox.wait_for_events(timeout_sec=1)
    assert [e.post["id"] for e in events] == ["ask"] and events[0].kind == "mention"


@pytest.mark.asyncio
async def test_mentions_and_dms_come_first_and_the_rest_stays_unread():
    """RT-6.2: max_events must not cut off the message that wants an answer, and
    a channel with posts still queued must not be marked read."""
    from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent
    client = _BackfillClient([])
    inbox = WebSocketInbox(client=client, watched_channel_ids={"proj"})
    inbox._ws = _DeadSocket()
    for i in range(12):
        inbox._queue.put_nowait(ClassifiedEvent(
            kind="watched_channel", post=_post(f"c{i}", channel="proj")))
    inbox._queue.put_nowait(ClassifiedEvent(kind="dm", post=_post("dm", channel="dm1")))
    events = await inbox.wait_for_events(timeout_sec=1, max_events=10)
    assert events[0].post["id"] == "dm" and len(events) == 10
    assert inbox.pending == 3
    assert client.viewed == ["dm1"]  # proj still has queued posts: stays unread
    rest = await inbox.wait_for_events(timeout_sec=1, max_events=10)
    assert [e.post["id"] for e in rest] == ["c9", "c10", "c11"] and inbox.pending == 0
    assert client.viewed == ["dm1", "proj"]

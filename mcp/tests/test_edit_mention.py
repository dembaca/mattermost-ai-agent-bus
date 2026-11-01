"""RT-6.12 step 3: an edit that makes a post name the agent wakes it once."""

import asyncio
import json
import time

import pytest
from test_gate import GATED, HUMAN, ME, PROJ, STRANGER, Bus, _Socket

from mattermost_ai_agent_bus import channel, hookapi, server, unread, watermark
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import UnreadChannel

P1, P2 = "p1000000000000000000000000", "p2000000000000000000000000"
HOUR_MS = 3_600_000


@pytest.fixture
def bus(monkeypatch):
    for var in ("MM_TRUSTED_HUMANS", "MM_TRUSTED_HUMAN_USER_IDS", "MM_OWNER", "MM_OWNER_USER_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("MM_TRUSTED_HUMAN_USER_IDS", HUMAN)
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "dembaca")
    monkeypatch.setenv("MM_BOT_USER_ID", ME)
    b = Bus(monkeypatch)
    monkeypatch.setattr(server, "_get_client", lambda: b.client)
    monkeypatch.setattr(server, "_get_inbox", lambda: b.inbox)
    return b


def _post(pid, message, *, user=HUMAN, age_ms=60_000, edited=False):
    now = int(time.time() * 1000)
    return {"id": pid, "user_id": user, "channel_id": PROJ, "root_id": "", "message": message,
            "create_at": now - age_ms, "edit_at": now if edited else 0}


def _posted(pid, message, **kw):
    return json.dumps({"event": "posted", "data": {"channel_type": "O",
                                                   "post": json.dumps(_post(pid, message, **kw))}})


def _edited(pid, message, **kw):
    return json.dumps({"event": "post_edited", "data": {
        "post": json.dumps(_post(pid, message, edited=True, **kw))}})


async def _stream(bus, *messages):
    """The reader takes these WebSocket messages; the queue afterwards."""
    bus.inbox._ws = _Socket(*messages)
    await bus.inbox._read_loop()
    await asyncio.gather(*list(bus.inbox._gate_tasks))
    bus.inbox._ws = _Socket()
    return list(bus.inbox._queue._queue)


@pytest.mark.asyncio
async def test_an_edit_that_adds_a_mention_wakes_once(bus):
    """RT-6.12 step 3: a plain post queued as a channel post, then edited to
    name the agent, wakes it as a mention, marked edited, typing as for a new one."""
    queued = await _stream(bus, _posted(P1, "deploy is done"), _edited(P1, "@agent-me deploy is done"))
    assert [(e.kind, e.edited) for e in queued] == [("watched_channel", False), ("mention", True)]
    assert not queued[1].gated
    items = json.loads(await server.wait_for_events(timeout_sec=1))
    mention = next(i for i in items if i["kind"] == "mention")
    assert mention["post"]["edited"] is True and mention["post"]["message"] == "@agent-me deploy is done"
    assert channel.notification(mention)["meta"]["edited"] == "true"
    assert bus.inbox._typing is not None
    bus.inbox.stop_typing()


@pytest.mark.asyncio
async def test_a_second_edit_of_the_same_post_does_not_wake_again(bus):
    """RT-6.12 step 3: once per post."""
    queued = await _stream(bus, _edited(P1, "@agent-me deploy"), _edited(P1, "@agent-me deploy now"))
    assert [(e.kind, e.post.message) for e in queued] == [("mention", "@agent-me deploy")]


@pytest.mark.asyncio
async def test_an_edit_of_a_post_that_named_the_agent_wakes_nobody(bus):
    """RT-6.12 step 3: a typo fix in a post queued as a mention is no new mention."""
    queued = await _stream(bus, _posted(P1, "@agent-me deplyo"), _edited(P1, "@agent-me deploy"))
    assert [(e.kind, e.edited) for e in queued] == [("mention", False)]


@pytest.mark.asyncio
async def test_an_edited_plain_post_wakes_nobody(bus):
    """RT-6.12 step 3: an edit that names nobody new wakes nobody, whether the
    post was queued before or not."""
    queued = await _stream(bus, _posted(P1, "hello"), _edited(P1, "hello all"), _edited(P2, "fixed"))
    assert [(e.kind, e.post.id) for e in queued] == [("watched_channel", P1)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("hours", "wakes"), [(23, True), (25, False)])
async def test_an_edit_of_a_post_older_than_a_day_wakes_nobody(bus, hours, wakes):
    """RT-6.12 step 3: what a server delivered is kept in memory only, so an
    edit wakes only within a day of the post."""
    queued = await _stream(bus, _edited(P1, "@agent-me look", age_ms=hours * HOUR_MS))
    assert bool(queued) is wakes


@pytest.mark.asyncio
async def test_an_edit_by_someone_not_trusted_is_gated_as_a_new_post(bus):
    """RT-6.12 step 3, RT-6.16: the edit is gated and answered once in its
    thread, exactly as the same mention in a new post."""
    queued = await _stream(bus, _posted(P1, "@agent-me deploy", user=STRANGER),
                           _posted(P2, "deploy", user=STRANGER),
                           _edited(P2, "@agent-me deploy", user=STRANGER))
    assert [(e.post.id, e.kind, e.edited, e.gated) for e in queued] == [
        (P1, "mention", False, GATED), (P2, "watched_channel", False, ""), (P2, "mention", True, GATED)]
    assert [p["root_id"] for p in bus.posted] == [P1, P2]
    assert bus.inbox.edits() == []  # gated: data only, it keeps no count up


@pytest.mark.asyncio
async def test_the_hooks_count_an_edit_and_leave_the_mark(bus, monkeypatch):
    """RT-6.12 step 3, RT-6.2 step 1: the edited post is behind the read
    watermark. It does not pull the mark back (what was handed over after it
    would count again), and the hooks' count adds it, each post once."""
    now = int(time.time() * 1000)
    watermark.advance(ME, PROJ, "q" * 26, now)
    await _stream(bus, _edited(P1, "@agent-me deploy"))
    assert watermark.at(ME, PROJ) == now

    found: list[unread.Pending] = []

    async def channels(*_, **__):
        return list(found)

    async def nothing(*_):
        return set()

    monkeypatch.setattr(unread, "channels", channels)
    monkeypatch.setattr(hookapi, "guest_ids", nothing)
    monkeypatch.setattr(hookapi, "watched", nothing)
    monkeypatch.setattr(hookapi, "edits", bus.inbox.edits)
    rows = await hookapi.counted(bus.client)
    assert [(r.channel.channel_id, r.channel.channel_type, r.waiting, [p.id for p in r.posts])
            for r in rows] == [(PROJ, "O", 1, [P1])]
    found.append(unread.Pending(UnreadChannel(PROJ), 1, [post_of(_post(P1, "@agent-me deploy"))]))
    assert sum(r.waiting for r in await hookapi.counted(bus.client)) == 1

    await server.wait_for_events(timeout_sec=1)
    bus.inbox.stop_typing()
    assert bus.inbox.edits() == [] and watermark.at(ME, PROJ) == now

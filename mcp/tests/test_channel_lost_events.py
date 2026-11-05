"""RT-6.12 step 3: an event counts as delivered only once the pusher dealt with
it on a live connection; what it did not deal with when the connection ends or
a push fails goes back into the inbox, unhandled. Nothing is lost, and nothing
pushed is pushed twice."""

import asyncio

import anyio
import pytest

from mattermost_ai_agent_bus import channel
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent

from fakes import FakeChat
from test_typing import _inbox


def _event(n: int, kind: str = "mention") -> ClassifiedEvent:
    return ClassifiedEvent(kind=kind, post=post_of({"id": f"p{n}", "channel_id": "c1", "create_at": n}))


def _item(n: int) -> dict:
    return {"kind": "mention", "from_trusted_human": True,
            "post": {"id": f"p{n}", "root_id": "", "author": "x", "at": "09:00", "message": f"m{n}"}}


class _Conn:
    """A connection whose notify can fail or hang from the n-th push on."""

    def __init__(self, fail_at: int = 0, hang_at: int = 0):
        self.initialized = asyncio.Event()
        self.initialized.set()
        self.sent: list = []
        self.calls = 0
        self.fail_at, self.hang_at = fail_at, hang_at

    async def notify(self, method, params):
        self.calls += 1
        n = self.calls
        if self.hang_at == n:
            await asyncio.sleep(3600)
        if self.fail_at == n:
            raise BrokenPipeError("the relay is gone")
        self.sent.append(params)


def _pusher(conn):
    pusher = channel.Pusher()
    pusher.connection = conn
    pusher.confirm(pusher.code)
    return pusher


async def _answered(pusher, conn, *args, **kw):
    """run() with the session confirming the check at once (every connection checks)."""
    async def confirm():
        while not conn.sent:
            await asyncio.sleep(0.005)
        pusher.confirm(pusher.code)

    async with anyio.create_task_group() as tg:
        tg.start_soon(confirm)
        await pusher.run(*args, check_after=(0.01,), **kw)


def _recorder():
    done, back = [], []

    async def delivered(item):
        done.append(item["post"]["id"])

    def give_back(items):
        back.append([i["post"]["id"] for i in items])

    return done, back, delivered, give_back


@pytest.mark.anyio
async def test_a_push_that_fails_mid_batch_gives_the_rest_back():
    """The connection dies at the second item: the first is delivered, the second
    and third go back, none is dropped (before: handed over at the read, then lost)."""
    conn = _Conn(fail_at=3)  # push 1 is the check, push 2 the first item, push 3 the second
    pusher = channel.Pusher()
    pusher.connection = conn
    done, back, delivered, give_back = _recorder()
    batches = [[_item(1), _item(2), _item(3)]]

    async def wait(timeout):
        if batches:
            return batches.pop(0)
        await asyncio.sleep(3600)

    with anyio.move_on_after(0.4):
        await _answered(pusher, conn, wait, delivered=delivered, give_back=give_back, retry_sec=0.05)
    assert done == ["p1"] and back == [["p2", "p3"]]
    assert [p["meta"].get("kind") for p in conn.sent][:2] == ["channel_check", "mention"]


@pytest.mark.anyio
async def test_a_run_cancelled_mid_batch_gives_the_rest_back():
    """The relay ends while the second push is out: the run is cancelled, the
    second (may be on its way: at-least-once) and the third go back."""
    conn = _Conn(hang_at=3)
    pusher = channel.Pusher()
    pusher.connection = conn
    done, back, delivered, give_back = _recorder()
    batches = [[_item(1), _item(2), _item(3)]]

    async def wait(timeout):
        if batches:
            return batches.pop(0)
        await asyncio.sleep(3600)

    with anyio.move_on_after(0.3):
        await _answered(pusher, conn, wait, delivered=delivered, give_back=give_back)
    assert done == ["p1"] and back == [["p2", "p3"]]


@pytest.mark.anyio
async def test_without_a_live_connection_nothing_is_pushed_and_nothing_delivered():
    conn = _Conn()
    pusher = channel.Pusher()
    pusher.connection = conn
    done, back, delivered, give_back = _recorder()
    batches = [[_item(1)]]

    async def wait(timeout):
        if batches:
            pusher.connection = None  # the connection is gone when the batch is pushed
            return batches.pop(0)
        await asyncio.sleep(3600)

    with anyio.move_on_after(0.3):
        await _answered(pusher, conn, wait, delivered=delivered, give_back=give_back, retry_sec=0.05)
    assert done == [] and back and back[0] == ["p1"] and [p["meta"].get("kind") for p in conn.sent] == ["channel_check"]


@pytest.mark.anyio
async def test_dropped_and_held_items_count_as_delivered():
    """A system post, a guest post and a post held for the digest are dealt with:
    handed over at once, as before (the digest lives in the pusher)."""
    conn = _Conn()
    pusher = channel.Pusher()
    pusher.connection = conn
    done, back, delivered, give_back = _recorder()
    joined = {**_item(1), "post": {**_item(1)["post"], "type": "system_join_channel"}}
    guest = {**_item(2), "guest": True}
    held = _item(3)
    batches = [[joined, guest, held]]

    async def wait(timeout):
        if batches:
            return batches.pop(0)
        await asyncio.sleep(3600)

    async def addressed(item):
        return item is not held and item is not guest

    with anyio.move_on_after(0.3):
        await _answered(pusher, conn, wait, addressed=addressed, delivered=delivered, give_back=give_back)
    assert done == ["p1", "p2", "p3"] and back == [] and pusher.held == [held]
    assert [p["meta"].get("kind") for p in conn.sent] == ["channel_check"]


@pytest.mark.anyio
async def test_a_digest_that_cannot_be_sent_is_held_again_and_goes_out_later():
    conn = _Conn(fail_at=2)  # push 1 is the check, push 2 the digest
    pusher = channel.Pusher(digest_sec=0.0)
    pusher.connection = conn
    pusher.held, pusher.held_since = [_item(1)], 0.0

    async def wait(timeout):
        await asyncio.sleep(0.01)
        return []

    with anyio.move_on_after(0.4):
        await _answered(pusher, conn, wait, retry_sec=0.05)
    assert any(p["meta"].get("kind") == "digest" for p in conn.sent) and pusher.held == []


@pytest.mark.anyio
async def test_a_held_post_stays_held_when_the_crown_push_cannot_go_out():
    pusher = channel.Pusher()
    pusher.confirm(pusher.code)
    pusher.held = [_item(2)]
    pusher.connection = None
    assert await pusher.send({"x": 1}) is False

    async def wakes(item):
        return True

    held = list(pusher.held)
    assert await pusher.recheck(wakes) == 0 and pusher.held == held


@pytest.mark.asyncio
async def test_events_taken_without_delivery_stay_unhandled_and_can_be_put_back():
    """The inbox half: taken with deliver=False the read watermark does not move;
    requeue puts them back in front, wait_for_events hands over what it returns."""
    inbox, _ = _inbox()
    handled: list[str] = []
    inbox.handled = lambda post: handled.append(post.id)  # type: ignore[method-assign]
    for n in (1, 2, 3):
        await inbox._queue.put(_event(n))
    taken = await inbox.wait_for_events(timeout_sec=1, max_events=10, agent=False, deliver=False)
    assert [e.post.id for e in taken] == ["p1", "p2", "p3"] and handled == []
    await inbox.deliver(taken[:1])  # the first was pushed
    assert handled == ["p1"]
    inbox.requeue(taken[1:])  # the connection ended before the others
    assert inbox.pending == 2
    again = await inbox.wait_for_events(timeout_sec=1)
    assert [e.post.id for e in again] == ["p2", "p3"] and handled == ["p1", "p2", "p3"]
    inbox.stop_typing()


@pytest.mark.asyncio
async def test_the_server_wires_delivery_and_give_back(monkeypatch):
    """The server's half: _push_wait takes without handing over, _push_delivered hands over
    one item, _push_give_back puts the rest back."""
    from mattermost_ai_agent_bus import server

    inbox, _ = _inbox()
    handled: list[str] = []
    inbox.handled = lambda post: handled.append(post.id)  # type: ignore[method-assign]
    monkeypatch.setattr(server, "_get_inbox", lambda: inbox)
    monkeypatch.setattr(server, "_get_client", lambda: FakeChat())

    async def my_id(_client):
        return "me"

    async def items(events, raw=False):
        return [_item(int(e.post.id[1:])) for e in events]

    monkeypatch.setattr(server, "_my_id", my_id)
    monkeypatch.setattr(server, "_event_items", items)
    for n in (1, 2):
        await inbox._queue.put(_event(n, kind="watched_channel"))
    got = await server._push_wait(1)
    assert [i["post"]["id"] for i in got] == ["p1", "p2"] and handled == [] and len(server._taken) == 2
    await server._push_delivered(got[0])
    assert handled == ["p1"] and len(server._taken) == 1
    server._push_give_back(got[1:])
    assert server._taken == {} and inbox.pending == 1
    back = await inbox.wait_for_events(timeout_sec=1)
    assert [e.post.id for e in back] == ["p2"]
    inbox.stop_typing()

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

    def __init__(self, fail_at: int = 0, hang_at: int = 0, fail_from: int = 0):
        self.initialized = asyncio.Event()
        self.initialized.set()
        self.sent: list = []
        self.calls = 0
        self.fail_at, self.hang_at, self.fail_from = fail_at, hang_at, fail_from

    async def notify(self, method, params):
        self.calls += 1
        n = self.calls
        if self.hang_at == n:
            await asyncio.sleep(3600)
        if self.fail_at == n or (self.fail_from and n >= self.fail_from):
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


def _bus_inbox(monkeypatch, tmp_path):
    """An inbox with a bot id and a real read watermark in a temporary state directory."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    inbox, _ = _inbox()
    inbox._bot_user_id = "u" * 26
    return inbox


def _channel_post(n: int, kind: str = "watched_channel") -> ClassifiedEvent:
    return ClassifiedEvent(kind=kind, post=post_of({"id": f"p{n}".ljust(26, "0"), "channel_id": "c" * 26,
                                                    "create_at": n * 1000}))


@pytest.mark.asyncio
async def test_taken_events_are_unhandled_until_acknowledged_and_can_be_given_back():
    """The inbox half: take leaves the events unhandled and counted as waiting; ack
    hands over; nack puts the rest back in front, in order."""
    inbox, _ = _inbox()
    handled: list[str] = []
    inbox.handled = lambda post: handled.append(post.id)  # type: ignore[method-assign]
    for n in (1, 2, 3):
        await inbox._queue.put(_event(n))
    taken = await inbox.take(timeout_sec=1, max_events=10)
    assert [e.post.id for e in taken] == ["p1", "p2", "p3"] and handled == []
    assert inbox.waits("mention") and inbox.holds("c1") is True  # still counted
    await inbox.ack(taken[:1])
    assert handled == ["p1"]
    inbox.nack(taken[1:])
    assert inbox.pending == 2 and inbox._out == []
    again = await inbox.wait_for_events(timeout_sec=1)
    assert [e.post.id for e in again] == ["p2", "p3"] and handled == ["p1", "p2", "p3"]
    inbox.stop_typing()


@pytest.mark.asyncio
async def test_the_watermark_does_not_pass_events_given_back(monkeypatch, tmp_path):
    """The review's scenario: c1 (t1), c2 (t2) and a mention m (t3) in one channel, m comes first.
    m is pushed and acknowledged, then the relay ends and c1, c2 come back: the watermark must
    stay before them (they were out, not yet handled), so the Stop hook and a backfill still see them."""
    from mattermost_ai_agent_bus import watermark

    inbox = _bus_inbox(monkeypatch, tmp_path)
    for n, kind in ((1, "watched_channel"), (2, "watched_channel"), (3, "mention")):
        await inbox._queue.put(_channel_post(n, kind))
    taken = await inbox.take(timeout_sec=1, max_events=10)
    assert [e.post.create_at for e in taken] == [3000, 1000, 2000]  # the mention first
    await inbox.ack(taken[:1])  # m pushed
    assert watermark.at("u" * 26, "c" * 26) < 1000  # c1 and c2 are out: the mark stays before them
    inbox.nack(taken[1:])
    assert watermark.at("u" * 26, "c" * 26) < 1000
    got = await inbox.wait_for_events(timeout_sec=1)
    assert [e.post.create_at for e in got] == [1000, 2000]
    # Handed over, now it passes them and m, handed over before them (RT-6.2 step 1, 0.86.4).
    assert watermark.at("u" * 26, "c" * 26) == 3000
    inbox.stop_typing()


@pytest.mark.asyncio
async def test_the_server_wires_ack_and_give_back(monkeypatch):
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
    assert [i["post"]["id"] for i in got] == ["p1", "p2"] and handled == [] and len(inbox._out) == 2
    await server._push_delivered(got[0])
    assert handled == ["p1"] and len(inbox._out) == 1
    server._push_give_back(got[1:])
    assert inbox._out == [] and inbox.pending == 1
    back = await inbox.wait_for_events(timeout_sec=1)
    assert [e.post.id for e in back] == ["p2"]
    inbox.stop_typing()


@pytest.mark.asyncio
async def test_bookkeeping_after_the_hand_over_never_ends_the_run(monkeypatch, caplog):
    """An exception in typing, the sign-off or the stats must not abort the pusher after the item was acknowledged."""
    from mattermost_ai_agent_bus import server

    inbox, _ = _inbox()
    handled: list[str] = []
    inbox.handled = lambda post: handled.append(post.id)  # type: ignore[method-assign]
    monkeypatch.setattr(server, "_get_inbox", lambda: inbox)

    async def boom(events, items):
        raise RuntimeError("network")

    async def items(events, raw=False):
        return [_item(int(e.post.id[1:])) for e in events]

    monkeypatch.setattr(server, "_event_items", items)
    monkeypatch.setattr(server, "_note_delivered", boom)
    monkeypatch.setattr(server, "_typing_for_task", boom)
    await inbox._queue.put(_event(1, kind="watched_channel"))
    got = await server._push_wait(1)
    await server._push_delivered(got[0])  # does not raise
    assert handled == ["p1"] and inbox._out == []


@pytest.mark.asyncio
async def test_a_batch_shows_typing_for_its_first_mention_only(monkeypatch):
    from mattermost_ai_agent_bus import server

    inbox, _ = _inbox()
    started: list[str] = []
    inbox.start_typing = lambda cid, thread="": started.append(thread or cid)  # type: ignore[method-assign]
    monkeypatch.setattr(server, "_get_inbox", lambda: inbox)
    monkeypatch.setattr(server, "_get_client", lambda: FakeChat())

    async def my_id(_client):
        return "me"

    async def items(events, raw=False):
        return [_item(int(e.post.id[1:])) for e in events]

    monkeypatch.setattr(server, "_my_id", my_id)
    monkeypatch.setattr(server, "_event_items", items)
    for n, root in ((1, "r1"), (2, "r2")):
        await inbox._queue.put(ClassifiedEvent(kind="mention", post=post_of(
            {"id": f"p{n}", "channel_id": "c1", "root_id": root, "create_at": n})))
    got = await server._push_wait(1)
    for item in got:
        await server._push_delivered(item)
    assert started == ["r1"]  # the first mention of the batch, as before the per-item hand-over
    inbox.stop_typing()


@pytest.mark.anyio
async def test_a_digest_that_cannot_be_sent_waits_before_the_next_try():
    """No tight loop: held_since is old, so the wait timeout is 0; after a failed digest the run sleeps."""
    conn = _Conn(fail_from=2)  # every push after the check fails
    pusher = channel.Pusher(digest_sec=0.0)
    pusher.connection = conn
    pusher.held, pusher.held_since = [_item(1)], 0.0
    reads = 0

    async def wait(timeout):
        nonlocal reads
        reads += 1
        await asyncio.sleep(timeout)  # as the real read: a timeout of 0 (a digest due) returns at once
        return []

    with anyio.move_on_after(0.3):
        await _answered(pusher, conn, wait, retry_sec=0.1, wait_cap=0.05)
    assert reads <= 6 and [h["post"]["id"] for h in pusher.held] == ["p1"]  # the failed digest slept


@pytest.mark.anyio
async def test_recheck_keeps_the_order_of_the_held_posts_when_a_push_fails():
    pusher = channel.Pusher()
    pusher.confirm(pusher.code)
    conn = _Conn(fail_at=2)
    pusher.connection = conn
    a, b, c = _item(1), _item(2), _item(3)
    pusher.held = [a, b, c]

    async def wakes(item):
        return item is not c

    assert await pusher.recheck(wakes) == 1  # a goes out, b fails
    assert pusher.held == [b, c]  # b stays before c


@pytest.mark.anyio
async def test_recheck_puts_a_failed_post_back_into_the_held_list_as_it_is_now():
    """The run may append to the held list while recheck awaits: what it added stays, and the
    post that could not go out is back at its place (before: the old snapshot replaced the list)."""
    pusher = channel.Pusher()
    pusher.confirm(pusher.code)
    pusher.connection = _Conn(fail_at=2)
    a, b, late = _item(1), _item(2), _item(3)
    pusher.held = [a, b]

    async def wakes(item):
        if item is b:
            pusher.held.append(late)  # the run held another post meanwhile
        return True

    assert await pusher.recheck(wakes) == 1  # a goes out, b fails
    assert pusher.held == [b, late]


@pytest.mark.asyncio
async def test_a_cancel_as_the_getter_finishes_does_not_lose_the_event():
    """The pusher's run ends with its connection while take waits: if the queue's getter
    had just finished, its event goes back to the front, not lost."""
    inbox, _ = _inbox()
    task = asyncio.ensure_future(inbox.take(timeout_sec=5))
    await asyncio.sleep(0.01)  # take waits on its getter
    inbox._queue.put_nowait(_event(1))  # the getter finishes at the next loop turn
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert inbox._out == []
    again = await inbox.wait_for_events(timeout_sec=0.5)
    assert [e.post.id for e in again] == ["p1"]
    inbox.stop_typing()


@pytest.mark.asyncio
async def test_a_failing_hand_over_is_logged_never_raised_and_loses_nothing(monkeypatch, caplog):
    inbox, _ = _inbox()
    await inbox._queue.put(_event(1))
    taken = await inbox.take(timeout_sec=1)

    async def boom(events):
        raise RuntimeError("view failed")

    monkeypatch.setattr(inbox, "_hand_over", boom)
    await inbox.ack(taken)  # does not raise
    assert inbox._out == [] and "hand-over of 1 event(s) failed" in caplog.text
    inbox.stop_typing()


@pytest.mark.anyio
async def test_an_agents_context_line_is_dropped_not_held():
    """RT-6.6 step 9: an item marked quiet (another agent's context line) is neither
    pushed nor held for the digest: no turn for it. It counts as delivered."""
    conn = _Conn()
    pusher = channel.Pusher()
    pusher.connection = conn
    done, back, delivered, give_back = _recorder()
    quiet = {**_item(1), "quiet": True, "intent": "fyi"}
    batches = [[quiet]]

    async def wait(timeout):
        if batches:
            return batches.pop(0)
        await asyncio.sleep(3600)

    async def addressed(item):
        return False

    with anyio.move_on_after(0.3):
        await _answered(pusher, conn, wait, addressed=addressed, delivered=delivered, give_back=give_back)
    assert done == ["p1"] and pusher.held == []
    assert [p["meta"].get("kind") for p in conn.sent] == ["channel_check"]

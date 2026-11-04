"""RT-6.17 step 7 (ADR-009 phase 1): GET /v1/wake?wait=S is a long poll. It
answers at once when something wakes the agent by the rules it has without
wait (RT-6.2 step 4), empty at the end of S (a hard deadline), never for a
gated post or past a BYE; a second request replaces the first, which answers
reason "replaced" and gives up its check and heavy slot; a client that goes
and the server's exit end it; the session's checks keep MIN_GAP_SEC apart;
without wait it answers as before; S is capped at 55."""

import asyncio
import time
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from mattermost_ai_agent_bus import hookapi, session_file, stall, unread
from mattermost_ai_agent_bus.model import UnreadChannel
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

from fakes import HookClient

UID = "u" * 26
OTHER = "o" * 26
EMPTY = {"wake": False, "reason": "", "count": 0, "text": ""}
REPLACED = {**EMPTY, "reason": "replaced"}


def post(pid, message, user=OTHER, at=1000):
    return {"id": pid, "user_id": user, "message": message, "create_at": at,
            "root_id": "", "type": "", "props": {}}


@pytest.fixture
async def bus(monkeypatch):
    """A hook API whose agent said HELLO in its project channel; rows are the
    unread counts (RT-6.2 step 1), checks when each wake check started, and
    holds["last"] / holds["own"] an Event a check waits for at its first /
    a late step (unread counted by then)."""
    hookapi._own_cache.clear()
    rows, checks, holds = [], [], {}

    async def channels(client, watched):
        return [unread.Pending(UnreadChannel(cid), n, []) for cid, n in rows]

    last, own = hookapi._last, hookapi._own_posts

    async def counted_last(client, query, body):
        checks.append(time.monotonic())
        if holds.get("last") is not None:
            await holds["last"].wait()
        return await last(client, query, body)

    async def held_own(client, uid, body):
        if holds.get("own") is not None:
            await holds["own"].wait()
        return await own(client, uid, body)

    monkeypatch.setattr(unread, "channels", channels)
    monkeypatch.setattr(hookapi, "_last", counted_last)
    monkeypatch.setattr(hookapi, "_own_posts", held_own)
    monkeypatch.setattr(hookapi, "MIN_GAP_SEC", 0.0)
    monkeypatch.setattr(stall, "waiting", lambda: False)
    now = int(time.time() * 1000)
    hello = post("h", "HELLO · agent-me", user=UID, at=now - 60_000)
    client = HookClient(uid=UID, posts=[hello])
    client.project_channel_name = "proj"

    async def get():
        return client

    api = hookapi.HookAPI(get)
    await api.start(write_file=False)
    http = httpx.AsyncClient(base_url=api.endpoint, timeout=30,
                             headers={"Authorization": f"Bearer {api.key}"})
    yield SimpleNamespace(api=api, http=http, client=client, rows=rows, checks=checks,
                          holds=holds, hello=hello, now=now, q=f"since={now - 30_000}")
    for h in holds.values():
        h.set()
    await http.aclose()
    await api.close()


async def until(cond, sec=3.0):
    for _ in range(int(sec * 100)):
        if cond():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("never came")


async def waiting(bus, task, checks=1):
    """Until the long poll has checked `checks` times in all and waits."""
    await until(lambda: len(bus.checks) >= checks and bus.api._poll is not None)
    await asyncio.sleep(0.05)
    assert not task.done()


def free_slots(bus):
    return bus.api._heavy._value


async def test_a_wake_during_the_wait_answers_at_once(bus, monkeypatch):
    """RT-6.17 step 7: a DM that arrives while the request waits answers it
    at once, as /v1/wake without wait would; the inbox rings when it queues,
    so a stall event for the 👑 (RT-6.19) does the same."""
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await waiting(bus, task)
    t0 = time.monotonic()
    bus.rows[:] = [("dm", 1)]
    hookapi.ring()
    got = (await asyncio.wait_for(task, 5)).json()
    assert got["wake"] is True and got["reason"] == "dm" and "wait_for_events" in got["text"]
    assert time.monotonic() - t0 < 2
    bus.rows.clear()
    inbox = WebSocketInbox(client=bus.client, on_queue=hookapi.ring)
    monkeypatch.setattr(stall, "waiting", lambda: inbox.waits(stall.KIND))
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await waiting(bus, task, checks=len(bus.checks) + 1)
    inbox.notify(stall.KIND, "the bus has stalled")
    got = (await asyncio.wait_for(task, 5)).json()
    assert got["reason"] == "stall"
    assert inbox.waits(stall.KIND)  # only looked: the event waits for wait_for_events


async def test_nothing_within_the_wait_answers_empty_at_its_end(bus, monkeypatch):
    """RT-6.17 step 7: nothing wakes the agent, so the answer is empty once
    `wait` is over; without a ring it checks again every RECHECK_SEC (an
    inbox not connected before the first wait_for_events rings nothing)."""
    t0 = time.monotonic()
    got = await bus.http.get(f"/v1/wake?{bus.q}&wait=1")
    assert got.status_code == 200 and got.json() == EMPTY
    assert 0.9 <= time.monotonic() - t0 < 3
    monkeypatch.setattr(hookapi, "RECHECK_SEC", 0.2)
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await waiting(bus, task, checks=len(bus.checks) + 1)
    bus.rows[:] = [("dm", 2)]
    assert (await asyncio.wait_for(task, 3)).json()["count"] == 2


async def test_a_gated_post_or_a_bye_does_not_end_the_wait(bus):
    """RT-6.17 step 7 with RT-6.16: a post that names the agent but was gated
    rings, is checked and wakes nobody; the next one, not gated, wakes it at
    once. Past a BYE a DM wakes nobody; once the agent posts again it does.
    The waits are long: only the rings end them, never the deadline."""
    d = session_file.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"gate-{UID}.env").write_text(f"GATED=n {int(time.time())}\n")
    gated = post("n", "@agent-me. please look", at=bus.now - 1000)
    bus.client.posts = [gated, bus.hello]
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await waiting(bus, task)
    hookapi.ring()
    await waiting(bus, task, checks=2)
    bus.client.posts = [post("m", "@agent-me. and this", at=bus.now - 500), gated, bus.hello]
    hookapi.ring()
    assert (await asyncio.wait_for(task, 3)).json()["reason"] == "addressed"
    bus.client.posts = [post("b", "BYE · done", user=UID, at=bus.now - 1000)]
    bus.rows[:] = [("dm", 1)]
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    seen = len(bus.checks)
    await waiting(bus, task, checks=seen + 1)
    hookapi.ring()
    await waiting(bus, task, checks=seen + 2)
    bus.client.posts = [post("b2", "HELLO · back", user=UID, at=bus.now - 500)]
    hookapi.ring()
    assert (await asyncio.wait_for(task, 3)).json()["reason"] == "dm"


async def test_a_ring_during_a_running_check_is_not_lost(bus):
    """RT-6.17 step 7: a DM queued while a check runs, past the point where it
    counted, rings; the poll checks again when that check ends."""
    bus.holds["own"] = asyncio.Event()
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await until(lambda: len(bus.checks) == 1)
    await asyncio.sleep(0.05)  # the check counted nothing and waits in its last steps
    bus.rows[:] = [("dm", 1)]
    hookapi.ring()
    bus.holds["own"].set()
    assert (await asyncio.wait_for(task, 3)).json()["reason"] == "dm"
    assert len(bus.checks) == 2


async def test_checks_of_a_session_keep_their_gap(bus, monkeypatch):
    """RT-6.17 step 7: rings within MIN_GAP_SEC of the end of the session's
    last check make one check, once the gap is over; a new request of the
    session keeps the gap too."""
    monkeypatch.setattr(hookapi, "MIN_GAP_SEC", 0.5)
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await waiting(bus, task)
    for _ in range(5):
        hookapi.ring()
        await asyncio.sleep(0.02)
    await until(lambda: len(bus.checks) == 2)
    assert bus.checks[1] - bus.checks[0] >= 0.45
    await asyncio.sleep(0.7)
    assert len(bus.checks) == 2 and not task.done()
    bus.rows[:] = [("dm", 1)]
    hookapi.ring()
    assert (await asyncio.wait_for(task, 3)).json()["reason"] == "dm"
    got = (await bus.http.get(f"/v1/wake?{bus.q}&wait=30")).json()
    assert got["reason"] == "dm" and bus.checks[-1] - bus.checks[-2] >= 0.45


async def test_a_second_request_replaces_the_first(bus):
    """RT-6.17 step 7: one waiting request per session; the older one answers
    at once, reason replaced, so a plugin that restarts leaks none and a
    second poller can tell it lost and back off."""
    first = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await waiting(bus, first)
    t0 = time.monotonic()
    second = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=1"))
    assert (await asyncio.wait_for(first, 2)).json() == REPLACED
    assert time.monotonic() - t0 < 1 and not second.done()
    assert (await asyncio.wait_for(second, 3)).json() == EMPTY
    assert bus.api._poll is None and not hookapi._bells


async def test_a_replaced_request_gives_up_its_check_and_slot(bus):
    """RT-6.17 steps 1 and 7: two pollers of one session never hold both
    heavy slots; the replaced one's running check is cancelled."""
    bus.holds["last"] = asyncio.Event()  # every check hangs, holding its slot
    a = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await until(lambda: len(bus.checks) == 1)
    assert free_slots(bus) == hookapi.MAX_HEAVY_RUNS - 1
    b = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    assert (await asyncio.wait_for(a, 2)).json() == REPLACED
    await until(lambda: len(bus.checks) == 2)
    assert free_slots(bus) == hookapi.MAX_HEAVY_RUNS - 1
    assert (await bus.http.get("/v1/pending")).status_code == 200  # a slot is free
    c = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    assert (await asyncio.wait_for(b, 2)).json() == REPLACED
    await until(lambda: len(bus.checks) == 3)
    assert free_slots(bus) == hookapi.MAX_HEAVY_RUNS - 1
    bus.rows[:] = [("dm", 1)]
    bus.holds["last"].set()
    assert (await asyncio.wait_for(c, 3)).json()["reason"] == "dm"
    assert free_slots(bus) == hookapi.MAX_HEAVY_RUNS


async def test_wait_is_a_hard_deadline(bus):
    """RT-6.17 step 7: a check that outlasts `wait` is given up and the
    answer is empty at the end, never the route timeout's 502."""
    bus.holds["last"] = asyncio.Event()
    t0 = time.monotonic()
    got = await bus.http.get(f"/v1/wake?{bus.q}&wait=1")
    assert got.status_code == 200 and got.json() == EMPTY
    assert time.monotonic() - t0 < 2.5
    assert free_slots(bus) == hookapi.MAX_HEAVY_RUNS


async def test_without_wait_it_answers_as_before(bus):
    """RT-6.17 step 3: without wait, since and until are needed and the answer
    comes at once (the OpenCode plugin's timer, RT-6.20); a long poll may
    leave until out (no listen window) and gets a 400 for a bad wait;
    wait=0 asks once."""
    until_s = int(time.time()) + 600
    assert (await bus.http.get(f"/v1/wake?{bus.q}")).status_code == 400
    t0 = time.monotonic()
    assert (await bus.http.get(f"/v1/wake?{bus.q}&until={until_s}")).json() == EMPTY
    bus.rows[:] = [("dm", 1)]
    assert (await bus.http.get(f"/v1/wake?{bus.q}&until={until_s}")).json()["reason"] == "dm"
    assert (await bus.http.get(f"/v1/wake?{bus.q}&until={int(time.time()) - 1}")).json() == EMPTY
    assert time.monotonic() - t0 < 2 and bus.api._poll is None
    assert (await bus.http.get(f"/v1/wake?{bus.q}&wait=0")).json()["reason"] == "dm"
    assert (await bus.http.get(f"/v1/wake?{bus.q}&until={until_s}&wait=5")).json()["reason"] == "dm"
    for bad in ("wait=x", "wait=-1", f"{bus.q}&wait=1.5", f"{bus.q}&wait=x", "wait=5",
                f"{bus.q}&until=x&wait=5"):
        assert (await bus.http.get(f"/v1/wake?{bad}")).status_code == 400


async def test_wait_is_capped_at_55_seconds(bus, monkeypatch):
    """RT-6.17 step 7: a longer wait is held MAX_WAIT_SEC (55) at most."""
    assert hookapi.MAX_WAIT_SEC == 55
    assert hookapi.wait_sec({"wait": "600"}) == 55 and hookapi.wait_sec({"wait": "3"}) == 3
    monkeypatch.setattr(hookapi, "MAX_WAIT_SEC", 1)
    t0 = time.monotonic()
    assert (await bus.http.get(f"/v1/wake?{bus.q}&wait=600")).json() == EMPTY
    assert time.monotonic() - t0 < 3


async def test_a_client_that_goes_or_the_servers_exit_ends_the_wait(bus):
    """RT-6.17 step 7: a client that closes its connection ends its wait; the
    server's exit answers a waiting request empty (not replaced)."""
    url = urlsplit(bus.api.endpoint)
    reader, writer = await asyncio.open_connection(url.hostname, url.port)
    writer.write(f"GET /v1/wake?{bus.q}&wait=30 HTTP/1.1\r\nHost: x\r\n"
                 f"Authorization: Bearer {bus.api.key}\r\n\r\n".encode())
    await writer.drain()
    await until(lambda: bus.api._poll is not None and bus.checks)
    writer.close()
    await until(lambda: bus.api._poll is None and not hookapi._bells)
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await waiting(bus, task, checks=len(bus.checks) + 1)
    await bus.api.close()
    assert (await asyncio.wait_for(task, 3)).json() == EMPTY

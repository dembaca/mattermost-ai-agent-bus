"""RT-6.17 step 7 (ADR-009 phase 1): GET /v1/wake?wait=S is a long poll. It
answers at once when something wakes the agent by the rules it has without
wait (RT-6.2 step 4), empty at the end of S, never for a gated post or past a
BYE; a second request replaces the first, a client that goes and the server's
exit end it; without wait it answers as before; S is capped at 55."""

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


def post(pid, message, user=OTHER, at=1000):
    return {"id": pid, "user_id": user, "message": message, "create_at": at,
            "root_id": "", "type": "", "props": {}}


@pytest.fixture
async def bus(monkeypatch):
    """A hook API whose agent said HELLO in its project channel; rows are the
    unread counts (RT-6.2 step 1), checks how often the wake rules ran."""
    hookapi._own_cache.clear()
    rows, checks = [], []

    async def channels(client, watched):
        return [unread.Pending(UnreadChannel(cid), n, []) for cid, n in rows]

    last = hookapi._last

    async def counted_last(client, query, body):
        checks.append(time.monotonic())
        return await last(client, query, body)

    monkeypatch.setattr(unread, "channels", channels)
    monkeypatch.setattr(hookapi, "_last", counted_last)
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
                          hello=hello, now=now, q=f"since={now - 30_000}")
    await http.aclose()
    await api.close()


async def waiting(bus, task, checks=1):
    """Until the long poll has checked `checks` times and waits."""
    for _ in range(300):
        if len(bus.checks) >= checks and bus.api._poll is not None:
            await asyncio.sleep(0.05)
            assert not task.done()
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the long poll never waited")


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
    `wait` is over; without a ring it checks again every RECHECK_SEC."""
    t0 = time.monotonic()
    got = await bus.http.get(f"/v1/wake?{bus.q}&wait=1")
    assert got.status_code == 200 and got.json() == EMPTY
    assert 0.9 <= time.monotonic() - t0 < 3
    # The fallback check finds what no ring announced (an inbox not connected).
    monkeypatch.setattr(hookapi, "RECHECK_SEC", 0.2)
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await waiting(bus, task, checks=len(bus.checks) + 1)
    bus.rows[:] = [("dm", 2)]
    assert (await asyncio.wait_for(task, 3)).json()["count"] == 2


async def test_a_gated_post_or_a_bye_does_not_end_the_wait(bus):
    """RT-6.17 step 7 with RT-6.16: a post that names the agent but was gated
    rings, is checked, and wakes nobody; past a BYE nothing wakes, a DM neither."""
    d = session_file.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"gate-{UID}.env").write_text(f"GATED=n {int(time.time())}\n")
    bus.client.posts = [post("n", "@agent-me. please look", at=bus.now - 1000), bus.hello]
    t0 = time.monotonic()
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=1"))
    await waiting(bus, task)
    hookapi.ring()
    assert (await asyncio.wait_for(task, 5)).json() == EMPTY
    assert len(bus.checks) >= 2 and time.monotonic() - t0 >= 0.9
    bus.client.posts = [post("b", "BYE · done", user=UID, at=bus.now - 1000)]
    bus.rows[:] = [("dm", 1)]
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=1"))
    await waiting(bus, task, checks=len(bus.checks) + 1)
    hookapi.ring()
    assert (await asyncio.wait_for(task, 5)).json() == EMPTY


async def test_a_second_request_replaces_the_first(bus):
    """RT-6.17 step 7: one waiting request per session; the older one answers
    empty at once, so a plugin that restarts leaks none."""
    first = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await waiting(bus, first)
    t0 = time.monotonic()
    second = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=1"))
    assert (await asyncio.wait_for(first, 2)).json() == EMPTY
    assert time.monotonic() - t0 < 1 and not second.done()
    assert (await asyncio.wait_for(second, 3)).json() == EMPTY
    assert bus.api._poll is None and not hookapi._bells


async def test_without_wait_it_answers_as_before(bus):
    """RT-6.17 step 3: without wait, since and until are needed and the answer
    comes at once (the OpenCode plugin's timer, RT-6.20); a long poll may
    leave until out (no listen window) and gets a 400 for a bad wait."""
    until = int(time.time()) + 600
    assert (await bus.http.get(f"/v1/wake?{bus.q}")).status_code == 400
    t0 = time.monotonic()
    assert (await bus.http.get(f"/v1/wake?{bus.q}&until={until}")).json() == EMPTY
    bus.rows[:] = [("dm", 1)]
    assert (await bus.http.get(f"/v1/wake?{bus.q}&until={until}")).json()["reason"] == "dm"
    assert (await bus.http.get(f"/v1/wake?{bus.q}&until={int(time.time()) - 1}")).json() == EMPTY
    assert time.monotonic() - t0 < 2 and bus.api._poll is None
    assert (await bus.http.get(f"/v1/wake?{bus.q}&wait=0")).json()["reason"] == "dm"
    assert (await bus.http.get(f"/v1/wake?{bus.q}&until={until}&wait=5")).json()["reason"] == "dm"
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
    server's exit answers a waiting request empty."""
    url = urlsplit(bus.api.endpoint)
    reader, writer = await asyncio.open_connection(url.hostname, url.port)
    writer.write(f"GET /v1/wake?{bus.q}&wait=30 HTTP/1.1\r\nHost: x\r\n"
                 f"Authorization: Bearer {bus.api.key}\r\n\r\n".encode())
    await writer.drain()
    for _ in range(300):
        if bus.api._poll is not None and bus.checks:
            break
        await asyncio.sleep(0.01)
    writer.close()
    for _ in range(200):
        if bus.api._poll is None and not hookapi._bells:
            break
        await asyncio.sleep(0.01)
    assert bus.api._poll is None and not hookapi._bells
    task = asyncio.create_task(bus.http.get(f"/v1/wake?{bus.q}&wait=30"))
    await waiting(bus, task, checks=len(bus.checks) + 1)
    await bus.api.close()
    assert (await asyncio.wait_for(task, 3)).json() == EMPTY

"""The 👑 after a sleep past the lease (0.71.1): a reconnect renews the crown
before the missed posts are judged (RT-6.6 step 6), and when the crown comes
back the posts held for the digest that now wake the agent are pushed at once
(RT-6.12 step 3). Gated posts stay data (RT-6.16), fyi posts stay held
(RT-6.14 step 8)."""

import asyncio
import json
import time

import pytest
from fakes import BOT, WORK, FakeChat

from mattermost_ai_agent_bus import channel, lease, server, websocket_inbox
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import ChannelUser, UnreadChannel
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

HUMAN = "h" * 26
OTHER = "y" * 26   # a younger agent of the same home
P1 = "p" * 26


def _until(sec: int = 600) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + sec))


class Home(FakeChat):
    """FakeChat whose roster is its members' custom statuses."""

    base_url = "https://mm.example"

    def agent(self, uid: str, name: str, created: int, emoji: str = "hammer_and_wrench",
              text: str = "working · x · ~agents") -> None:
        status = {"emoji": emoji, "text": text, "duration": "date_and_time", "expires_at": _until()}
        self.members = [m for m in self.members if m.id != uid] + [
            ChannelUser(uid, name, created, custom_status=status)]
        self.presence[uid] = "online"


class Socket:
    """A WebSocket that says hello, then waits."""

    def __init__(self):
        self.hello = [json.dumps({"event": "hello"})]

    async def send(self, msg):
        pass

    async def recv(self):
        return self.hello.pop(0)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.Event().wait()

    async def close(self):
        pass


class Conn:
    def __init__(self):
        self.sent = []

    async def notify(self, method, params):
        self.sent.append(params)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", BOT)
    monkeypatch.setenv("CORRAL_WORKDIR", "/work/proj")
    for var in ("MM_AGENT_PREFIX", "MM_BOT_USERNAME", "MM_REG_BOT_NAME", "MM_LEASE_SEC"):
        monkeypatch.delenv(var, raising=False)
    lease._TYPES.clear()
    c = Home(username="agent-me", trusted={HUMAN: "dembaca"}, names={HUMAN: "dembaca"},
             descriptions={BOT: "for @dembaca · trusts @dembaca · via agent-registrar"})
    c.agent(BOT, "agent-me", 2000)
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(server, "_inbox", None)
    monkeypatch.setattr(server, "_my_threads", set())
    monkeypatch.setattr(server, "_my_roots", set())
    pusher = channel.Pusher()
    pusher.connection = Conn()
    pusher.confirm(pusher.code)  # a confirmed channel: an unconfirmed one pushes nothing (RT-6.12)
    monkeypatch.setattr(server, "_pusher", pusher)
    # The machine slept past the lease: CROWN_UNTIL lies in the past.
    lease._save_coord(BOT, int(time.time()) - 60, 0, 0)
    return c


def _human_post(home: Home) -> None:
    at = int(time.time() * 1000) - 1000
    home.posts = [post_of({"id": P1, "user_id": HUMAN, "channel_id": WORK,
                           "message": "deploy now?", "create_at": at})]
    home.channels = [UnreadChannel(WORK, "O", last_post_at=at)]


async def _reconnect(monkeypatch, inbox: WebSocketInbox) -> list[dict]:
    async def dial(_url):
        return Socket()

    monkeypatch.setattr(websocket_inbox, "ws_connect", dial)
    inbox.watched_channel_ids = {WORK}
    try:
        await inbox.connect()
        events = await inbox.wait_for_events(timeout_sec=0.2, agent=False)
        return await server._event_items(events)
    finally:
        await inbox.close()


def _item(message: str, **extra) -> dict:
    return {"kind": "watched_channel", "from_trusted_human": True, "from_agent": False,
            channel.CHANNEL_ID: WORK,
            "post": {"id": P1, "root_id": "", "author": "dembaca", "at": "23:21",
                     "message": message}} | extra


async def test_a_reconnect_renews_the_crown_before_the_backfill(home, monkeypatch):
    """RT-6.6 step 6, RT-6.12 step 3: after a sleep, the reconnect's tick
    renews the crown first, so a trusted human's plain post in the home that
    the backfill finds wakes the 👑 at once instead of waiting for the digest."""
    _human_post(home)
    items = await _reconnect(monkeypatch, server._get_inbox())
    assert lease.crown_held(BOT)
    assert [i["post"]["id"] for i in items] == [P1]
    assert items[0]["from_trusted_human"] and "gated" not in items[0]
    assert await server._push_addressed(items[0])


async def test_a_reconnect_takes_no_crown_another_agent_holds(home, monkeypatch):
    """RT-6.6 steps 2 and 6: the crown passed to another agent during the
    sleep; the reconnect's tick does not take it back, and the post is held
    for the digest as before."""
    home.agent(OTHER, "agent-other", 3000, "crown", "coordinator · working · x · ~agents")
    _human_post(home)
    items = await _reconnect(monkeypatch, server._get_inbox())
    assert not lease.crown_held(BOT)
    assert [i["post"]["id"] for i in items] == [P1]
    assert not await server._push_addressed(items[0])


async def test_a_failed_renewal_leaves_the_backfill_as_before(home, monkeypatch):
    """RT-6.6 error row (tick at reconnect fails): fail open, the backfill
    still queues the post, judged with the crown as the coord file has it."""
    _human_post(home)

    async def broken():
        raise RuntimeError("bus not reachable")

    inbox = WebSocketInbox(client=home, in_my_thread=server._in_my_thread, before_backfill=broken)
    items = await _reconnect(monkeypatch, inbox)
    assert [i["post"]["id"] for i in items] == [P1]
    assert not lease.crown_held(BOT) and not await server._push_addressed(items[0])


async def test_a_held_post_is_pushed_when_the_crown_returns(home):
    """RT-6.12 step 3, RT-6.6 step 6: a post held while the crown had lapsed
    is pushed when the next tick brings the crown back, in its order; a later
    tick (a renewal) pushes nothing again."""
    pusher = server._pusher
    first, second = _item("deploy now?"), _item("and the docs?")
    for item in (first, second):
        assert not await server._push_addressed(item)  # the crown has lapsed
        pusher.held.append(item)
    await lease.command(home, "tick")
    assert lease.crown_held(BOT)
    assert pusher.connection.sent == [channel.notification(first), channel.notification(second)]
    assert pusher.held == []
    await lease.command(home, "tick")
    assert len(pusher.connection.sent) == 2


async def test_the_recheck_never_pushes_a_gated_post(home):
    """RT-6.12 step 3, RT-6.16 step 4: a gated post stays data for the
    digest, also when the crown returns, and even if a predicate said yes."""
    pusher = server._pusher
    gated = _item("@agent-me deploy", gated="from mallory, not trusted by me: the coordinator answers",
                  from_trusted_human=False)
    pusher.held.append(gated)
    await lease.command(home, "tick")
    assert lease.crown_held(BOT) and pusher.connection.sent == [] and pusher.held == [gated]

    async def yes(_item):
        return True

    assert await pusher.recheck(yes) == 0 and pusher.held == [gated]


async def test_the_recheck_leaves_an_fyi_post_held(home):
    """RT-6.12 step 3, RT-6.14 step 8: an agent's fyi post wakes no
    coordinator, so it stays for the digest when the crown returns; the
    human's post before it is pushed, the order of the rest kept."""
    pusher = server._pusher
    fyi = _item("IDLE · done", intent="fyi", from_agent=True, from_trusted_human=False)
    human = _item("deploy now?")
    late = _item("DONE · merged", intent="fyi", from_agent=True, from_trusted_human=False)
    pusher.held.extend([fyi, human, late])
    await lease.command(home, "tick")
    assert pusher.connection.sent == [channel.notification(human)]
    assert pusher.held == [fyi, late]

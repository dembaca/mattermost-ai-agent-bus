"""RT-6.6 coordinator crown and presence lease, kept by the MCP server
(docs/arc42/06-runtime-view.md): status, crown, roster and heartbeat."""

import asyncio
import json
import time

import httpx
import pytest

from mattermost_ai_agent_bus import lease
from mattermost_ai_agent_bus.client import MattermostAdapter
from mattermost_ai_agent_bus.model import RosterEntry

UID = "u" * 26
OLDER = "o" * 26
YOUNGER = "y" * 26
CHANNEL = "c" * 26
CUSTOM = "/api/v4/users/me/status/custom"


class FakeMattermost:
    """The work channel's accounts with their status, and what was written."""

    def __init__(self):
        self.users: list[dict] = []
        self.presence: dict[str, str] = {}
        self.writes: list[tuple[str, str, dict | None]] = []
        self.fail = False
        self.header: str | None = None   # the work channel's header; None: no permission
        self.patches: list[str] = []

    def agent(self, uid, username, create_at, emoji="", expires_in=600, presence="online"):
        """A bot in the work channel; expires_in=None: a status without expiry."""
        status: dict = {}
        if emoji:
            status = {"emoji": emoji, "text": emoji}
            if expires_in is not None:
                status |= {"duration": "date_and_time", "expires_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in))}
        self.users = [u for u in self.users if u["id"] != uid] + [{
            "id": uid, "username": username, "create_at": create_at, "delete_at": 0,
            "props": {"customStatus": json.dumps(status)} if status else {}}]
        self.presence[uid] = presence

    def custom_status(self) -> dict | None:
        for method, path, body in reversed(self.writes):
            if path == CUSTOM:
                return body if method == "PUT" else None
        return None

    def handler(self, req: httpx.Request) -> httpx.Response:
        if self.fail:
            return httpx.Response(500, json={})
        p = req.url.path
        body = json.loads(req.content) if req.content else None
        if req.method == "GET" and p == "/api/v4/users":
            assert req.url.params["in_channel"] == CHANNEL
            return httpx.Response(200, json=self.users)
        if p == "/api/v4/users/status/ids":
            return httpx.Response(200, json=[{"user_id": u, "status": self.presence.get(u, "offline")}
                                             for u in body])
        if p == f"/api/v4/channels/{CHANNEL}" and req.method == "GET":
            return httpx.Response(200, json={"id": CHANNEL, "name": "agents", "header": self.header or ""})
        if p == f"/api/v4/channels/{CHANNEL}/patch" and req.method == "PUT":
            if self.header is None:
                return httpx.Response(403, json={"message": "no permission"})
            self.header = body["header"]
            self.patches.append(self.header)
            return httpx.Response(200, json={"id": CHANNEL, "name": "agents", "header": self.header})
        if req.method in ("PUT", "DELETE"):
            self.writes.append((req.method, p, body))
            return httpx.Response(200, json={})
        return httpx.Response(404, json={})


@pytest.fixture
def mm(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    monkeypatch.setenv("MM_CHANNEL_ID", CHANNEL)
    monkeypatch.setenv("CORRAL_WORKDIR", "/work/proj")
    for var in ("MM_PROJECT_CHANNEL_ID", "MM_AGENT_PREFIX", "MM_BOT_USERNAME", "MM_REG_BOT_NAME",
                "MM_LEASE_SEC"):
        monkeypatch.delenv(var, raising=False)
    return FakeMattermost()


@pytest.fixture
def client(mm):
    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(mm.handler))
    return c


def _dir(tmp_path):
    return tmp_path / "mm-agent-bus"


def coord(tmp_path) -> dict[str, int]:
    f = _dir(tmp_path) / f"coord-{UID}.env"
    return {k: int(v) for k, v in (ln.split("=") for ln in f.read_text().split())} if f.exists() else {}


def holds() -> bool:
    return lease.crown_held(UID)


def me(mm, emoji="hammer_and_wrench", **kw):
    mm.agent(UID, "agent-me", 2000, emoji, **kw)


def _coord_file(tmp_path, monkeypatch, **values):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    d = _dir(tmp_path)
    d.mkdir(exist_ok=True)
    (d / f"coord-{UID}.env").write_text("".join(f"{k}={v}\n" for k, v in values.items()))


# --- the status ---------------------------------------------------------------------

async def test_every_status_is_a_lease(mm, client, tmp_path):
    """RT-6.6 step 1: a status always carries an expiry, so a dead session's
    runs out; presence online, the project as label, the state kept locally."""
    await lease.set_status(client, "working")
    s = mm.custom_status()
    assert s["emoji"] == "hammer_and_wrench" and s["text"] == "working · proj"
    assert s["duration"] == "date_and_time" and s["expires_at"].endswith("Z")
    assert ("PUT", f"/api/v4/users/{UID}/status", {"user_id": UID, "status": "online"}) in mm.writes
    assert (_dir(tmp_path) / f"presence-{UID}.env").read_text() == "STATE=working\nLABEL=proj\n"
    with pytest.raises(ValueError):
        await lease.set_status(client, "napping")


async def test_listening_ends_with_its_window(mm, client, tmp_path):
    """RT-6.6, RT-6.2 step 3: 👂 until the window's end at the latest; a renewal
    after a window that ran out without a Stop hook shows 💤."""
    until = int(time.time()) + 120
    await lease.set_status(client, "listening", str(until))
    s = mm.custom_status()
    assert s["emoji"] == "ear" and s["text"].startswith("listening · until ")
    assert s["expires_at"] == time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
    await lease.set_status(client, "renew")
    assert mm.custom_status()["emoji"] == "ear"
    (_dir(tmp_path) / f"presence-{UID}.env").write_text("STATE=listening\nLABEL=1\n")
    await lease.set_status(client, "renew")
    assert mm.custom_status()["emoji"] == "zzz"
    with pytest.raises(ValueError):
        await lease.set_status(client, "listening", "soon")


async def test_renew_without_a_report_is_on_the_bus(mm, client):
    """RT-6.6 step 1: no turn hooks (Cursor, OpenCode) report a state: 📡."""
    await lease.set_status(client, "renew")
    assert mm.custom_status()["text"] == "on the bus · proj"


async def test_offline_ends_status_and_lease(mm, client, tmp_path, monkeypatch):
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=int(time.time()) + 600)
    await lease.set_status(client, "working")
    await lease.set_status(client, "offline")
    assert mm.custom_status() is None and mm.writes[-1][0] == "DELETE"
    assert ("PUT", f"/api/v4/users/{UID}/status", {"user_id": UID, "status": "offline"}) in mm.writes
    assert not list(_dir(tmp_path).glob("*.env"))


async def test_status_fails_open(mm, client):
    """RT-6.6: Mattermost failing never surfaces; without a token nothing is sent."""
    mm.fail = True
    await lease.set_status(client, "working")
    client.token = None
    await lease.set_status(client, "working")
    assert await lease.command(client, "who") == ""


# --- the crown -----------------------------------------------------------------------

async def test_oldest_reachable_takes_the_free_crown(mm, client):
    """RT-6.6 step 2: no crown anywhere → the oldest reachable agent takes it."""
    me(mm)
    mm.agent(YOUNGER, "agent-young", 3000, "hammer_and_wrench")
    mm.agent(OLDER, "agent-old-idle", 1000, "zzz")           # idle: not reachable
    mm.agent("s" * 26, "agent-stale", 500, "hammer_and_wrench", expires_in=None)
    mm.agent("f" * 26, "agent-off", 400, "hammer_and_wrench", presence="offline")
    await lease.set_status(client, "working")
    assert await lease.command(client, "tick") == ""
    assert holds()
    s = mm.custom_status()
    assert s["emoji"] == "crown" and s["text"].startswith("coordinator · working")


async def test_younger_agent_does_not_claim(mm, client):
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")
    await lease.command(client, "tick")
    assert not holds()


async def test_take_waits_for_the_holder_then_takes(mm, client, tmp_path):
    """RT-6.6 step 4: on ACK the new coordinator's take waits for the release."""
    me(mm)
    mm.agent(OLDER, "agent-old", 1000, "crown")
    out = await lease.command(client, "take")
    assert out.startswith("pending: @agent-old still holds the crown")
    assert not holds() and coord(tmp_path)["PENDING"] == 1 and lease.take_pending(UID)
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")   # released
    await lease.command(client, "tick")
    assert holds() and coord(tmp_path)["PENDING"] == 0


async def test_release_leaves_room_for_the_successor(mm, client, tmp_path):
    """RT-6.6 step 4: after a release the oldest agent does not grab it back."""
    me(mm)
    assert await lease.command(client, "take") == "taken"
    assert holds()
    assert await lease.command(client, "release") == "released"
    assert coord(tmp_path)["YIELD_UNTIL"] > time.time() + 1700
    await lease.command(client, "tick")
    assert not holds()
    assert mm.custom_status()["emoji"] != "crown"


async def test_a_release_during_a_tick_stays_released(mm, client, tmp_path):
    """RT-6.6 step 4: a release that arrives while a tick renews the status
    waits for the tick; the tick's stale reading does not take the crown back."""
    me(mm)
    assert await lease.command(client, "take") == "taken"
    renewing, go = asyncio.Event(), asyncio.Event()
    plain = mm.handler

    async def slow(req):
        if req.method == "PUT" and req.url.path == CUSTOM and not go.is_set():
            renewing.set()
            await go.wait()
        return plain(req)

    client._client = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    tick = asyncio.create_task(lease.command(client, "tick"))
    await asyncio.wait_for(renewing.wait(), 2)
    release = asyncio.create_task(lease.command(client, "release"))
    await asyncio.sleep(0.05)
    go.set()
    assert await tick == "" and await release == "released"
    assert not holds() and coord(tmp_path)["YIELD_UNTIL"] > time.time()
    assert mm.custom_status()["emoji"] != "crown"


async def test_two_crowns_the_younger_yields(mm, client):
    """RT-6.6 error handling: a race leaves two crowns; the older keeps it."""
    me(mm, "crown")
    await lease.command(client, "take")
    mm.agent(OLDER, "agent-old", 1000, "crown")
    await lease.command(client, "tick")
    assert not holds()


async def test_idle_coordinator_loses_the_crown_when_its_lease_runs_out(mm, client, tmp_path):
    """RT-6.6 step 6: idle extends nothing; once the lease ends the crown is gone."""
    me(mm)
    await lease.command(client, "take")
    until = coord(tmp_path)["CROWN_UNTIL"]
    await lease.set_status(client, "idle")
    assert coord(tmp_path)["CROWN_UNTIL"] == until
    assert mm.custom_status()["emoji"] == "crown"
    f = _dir(tmp_path) / f"coord-{UID}.env"
    f.write_text(f.read_text().replace(f"CROWN_UNTIL={until}", "CROWN_UNTIL=1"))
    await lease.set_status(client, "renew")
    assert mm.custom_status()["emoji"] == "zzz" and not holds()
    assert coord(tmp_path)["CROWN_UNTIL"] == 0


# --- the roster ------------------------------------------------------------------------

async def test_roster_lists_coordinator_first_and_skips_the_unreachable(mm, client):
    """RT-6.6, RT-6.14 step 4: the crown holder heads the roster and is marked;
    idle, offline and stale bots are left out."""
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")
    mm.agent(YOUNGER, "agent-young", 3000, "crown")
    mm.agent("i" * 26, "agent-idle", 1500, "zzz")
    mm.agent("s" * 26, "agent-stale", 500, "hammer_and_wrench", expires_in=None)
    mm.agent("x" * 26, "agent-gone", 600, "hammer_and_wrench", expires_in=-60)
    assert await lease.roster(client) == [
        RosterEntry("agent-young", "crown", coordinator=True, crown=True),
        RosterEntry("agent-old", "hammer_and_wrench")]
    assert await lease.command(client, "who") == "agent-young"


async def test_without_a_crown_the_oldest_reachable_heads_the_roster(mm, client):
    mm.agent(YOUNGER, "agent-young", 3000, "ear")
    mm.agent(OLDER, "agent-old", 1000, "hammer_and_wrench")
    rows = await lease.roster(client)
    assert [(r.username, r.coordinator, r.crown) for r in rows] == [
        ("agent-old", True, False), ("agent-young", False, False)]
    assert await lease.command(client, "who") == "agent-old"


async def test_roster_and_crown_follow_the_registrar_prefix(mm, client, monkeypatch):
    """RT-6.6, arc42 §7.1: the bus's bots are the ones with the registrar's
    prefix, handed in as MM_AGENT_PREFIX or derived from the session's own bot."""
    mm.agent(OLDER, "bot-old", 1000, "hammer_and_wrench")
    mm.agent(YOUNGER, "bot-young", 3000, "crown")
    mm.agent("x" * 26, "agent-other", 2000, "hammer_and_wrench")

    async def names() -> list[str]:
        return [r.username for r in await lease.roster(client)]

    monkeypatch.setenv("MM_AGENT_PREFIX", "bot-")
    assert await names() == ["bot-young", "bot-old"]
    assert await lease.command(client, "who") == "bot-young"
    monkeypatch.delenv("MM_AGENT_PREFIX")
    monkeypatch.setenv("MM_BOT_USERNAME", "bot-me")
    monkeypatch.setenv("MM_REG_BOT_NAME", "me")
    assert await lease.command(client, "who") == "bot-young"
    monkeypatch.delenv("MM_BOT_USERNAME")
    monkeypatch.delenv("MM_REG_BOT_NAME")
    assert await names() == ["agent-other"]


async def test_an_unreadable_roster_is_empty(mm, client):
    me(mm)
    mm.fail = True
    assert await lease.roster(client) == []
    assert await lease.command(client, "tick") == ""


def test_roster_rows_become_status_words():
    """RT-6.14 step 4: the status text's word after "coordinator", else the
    lease emoji's; the custom status stays in lease.py."""
    rows = [{"username": "agent-b", "emoji": "crown", "coordinator": True, "crown": True,
             "text": "coordinator · working · bus"},
            {"username": "agent-a", "emoji": "ear", "text": ""},
            {"username": "agent-c", "emoji": "zzz", "presence": "away", "reachable": True}]
    assert [lease.entry(r) for r in rows] == [
        RosterEntry("agent-b", "working", coordinator=True, crown=True),
        RosterEntry("agent-a", "listening"),
        RosterEntry("agent-c", "idle"),
    ]


# --- crown prefix and heartbeat ------------------------------------------------------------

def test_coordinator_posts_start_with_the_crown(tmp_path, monkeypatch):
    """RT-6.6: bot posts never show a status, so the crown goes into the post."""
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=int(time.time()) + 600)
    assert lease.with_crown("ACK", UID) == "👑 ACK"
    assert lease.with_crown("👑 ACK", UID) == "👑 ACK"


def test_no_prefix_without_a_live_crown(tmp_path, monkeypatch):
    _coord_file(tmp_path, monkeypatch, CROWN_UNTIL=1)
    assert lease.with_crown("ACK", UID) == "ACK"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "none"))
    assert lease.with_crown("ACK", UID) == "ACK"


async def test_heartbeat_ticks_and_survives_errors(monkeypatch):
    """RT-6.6 step 1: the heartbeat renews the lease; a failure never stops it."""
    calls: list[str] = []

    class _Client:
        token = "t"
        session = None

    async def fake_command(client, action):
        calls.append(action)
        if len(calls) == 1:
            raise RuntimeError("bus down")
        return ""

    async def get_client():
        return _Client()

    monkeypatch.setattr(lease, "command", fake_command)
    monkeypatch.setattr(lease, "heartbeat_sec", lambda: 0.01)
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    task = asyncio.create_task(lease.heartbeat(get_client))
    await asyncio.sleep(0.05)
    task.cancel()
    assert calls.count("tick") >= 2


async def test_idle_in_a_channel_session_is_reachable_and_keeps_the_crown(mm, client, tmp_path):
    """RT-6.6 step 6, RT-6.12: the server pushes into a session started as a
    channel, so between its turns it stays reachable and renews its crown."""
    me(mm)
    await lease.command(client, "take")
    (_dir(tmp_path) / f"channel-{UID}.env").write_text("CONFIRMED=1\n")
    until = coord(tmp_path)["CROWN_UNTIL"]
    f = _dir(tmp_path) / f"coord-{UID}.env"
    f.write_text(f.read_text().replace(f"CROWN_UNTIL={until}", f"CROWN_UNTIL={until - 60}"))
    await lease.set_status(client, "idle")
    assert mm.custom_status()["emoji"] == "crown" and holds()
    assert coord(tmp_path)["CROWN_UNTIL"] >= until
    await lease.command(client, "release")
    await lease.set_status(client, "renew")
    assert mm.custom_status()["emoji"] == "ear"


# --- the work channel's header (RT-6.6 step 7) ---------------------------------------

def _bus(at: str = "") -> str:
    return (f"🚌 agent bus · 👑 @agent-me since {at or time.strftime('%H:%M')}\n"
            "plain post → 👑 routes it · @agent → that agent · 🙋 = a question for you")


async def test_the_new_crown_holder_names_itself_in_the_header(mm, client):
    """RT-6.6 step 7: when the crown changes hands, the new holder's server puts
    who holds it, since when, and the addressing rule into the work channel's
    header, after what people wrote there. Every change posts a system
    message, so a renewal or a header that says it already writes nothing."""
    mm.header = "Release on Fridays"
    me(mm)
    await lease.command(client, "tick")
    assert holds() and mm.header == f"Release on Fridays\n\n{_bus()}"
    await lease.command(client, "tick")
    await lease.command(client, "take")
    assert len(mm.patches) == 1
    await lease.command(client, "release")
    assert len(mm.patches) == 1                  # the next holder writes it
    assert await lease.command(client, "take") == "taken"
    assert len(mm.patches) == 1                  # the same minute: it says that already


async def test_the_header_replaces_only_the_bus_part(mm, client):
    """RT-6.6 step 7: another holder's bus part is replaced where it stands;
    what people wrote before and after it stays."""
    mm.header = f"Notes\n\n{_bus('09:00').replace('agent-me', 'agent-old')}\nmore notes"
    me(mm)
    await lease.command(client, "tick")
    assert mm.header == f"Notes\n\n{_bus()}\nmore notes"


def test_the_header_stays_within_mattermosts_limit():
    """RT-6.6 step 7: past 1024 characters people's text is cut, never the bus part."""
    bus = lease.bus_header("agent-me", int(time.time()))
    out = lease.with_bus_header("x" * 2000, bus)
    assert len(out) == lease.HEADER_MAX and out.endswith(bus) and "…\n\n" in out
    assert lease.with_bus_header("", bus) == bus
    assert lease.with_bus_header(f"a\n{bus}", bus) == f"a\n\n{bus}"


async def test_without_permission_the_crown_works_and_it_logs_once(mm, client, caplog, monkeypatch):
    """RT-6.6 step 7: a header the bot may not change is logged once; the crown is taken."""
    monkeypatch.setattr(lease, "_header_logged", False)
    me(mm)
    with caplog.at_level("DEBUG", logger="mattermost_ai_agent_bus.lease"):
        await lease.command(client, "tick")
        assert holds()
        await lease._header(client, CHANNEL, "agent-me", int(time.time()))
    assert mm.patches == []
    assert [r.levelname for r in caplog.records if "header" in r.getMessage()] == ["WARNING", "DEBUG"]

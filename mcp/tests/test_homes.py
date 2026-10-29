"""A home per agent and a crown per home (ADR-002, WP1 of #63): the status
names the home (RT-6.6 step 1), the roster and the election count only the
home's agents (RT-6.6 steps 2-5, 7), the 👑 shows only in the home, and the
coordinator routes and is never gated only where it coordinates (RT-6.12
step 3, RT-6.16 step 2). Parity: one channel, and a status without a home,
behave as before."""

import time

import pytest
from fakes import BOT, WORK, FakeChat

from mattermost_ai_agent_bus import channel, gate, lease, server, tasks
from mattermost_ai_agent_bus.model import ChannelUser, RosterEntry

OLDER = "o" * 26
YOUNGER = "y" * 26
OTHER = "x" * 26   # another team channel: someone else's home
DM = "d" * 26
LOBBY = "l" * 26


def _until(sec: int = 600) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + sec))


class Home(FakeChat):
    """FakeChat whose roster is its members' custom statuses."""

    def __init__(self, home: str = "agents", **kw):
        super().__init__(**kw)
        self.work_channel_name = home  # type: ignore[misc]

    def agent(self, uid: str, name: str, created: int, emoji: str = "hammer_and_wrench",
              text: str = "working · x") -> None:
        status = {"emoji": emoji, "text": text, "duration": "date_and_time",
                  "expires_at": _until()} if emoji else {}
        self.members = [m for m in self.members if m.id != uid] + [
            ChannelUser(uid, name, created, custom_status=status)]
        self.presence[uid] = "online"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", BOT)
    monkeypatch.setenv("CORRAL_WORKDIR", "/work/proj")
    for var in ("MM_AGENT_PREFIX", "MM_BOT_USERNAME", "MM_REG_BOT_NAME", "MM_LEASE_SEC"):
        monkeypatch.delenv(var, raising=False)
    lease._TYPES.clear()
    c = Home(username="agent-me")
    c.agent(BOT, "agent-me", 2000)
    return c


def _crown(home: Home) -> None:
    lease._save_coord(BOT, int(time.time()) + 600, 0, 0)


# --- the status names the home (RT-6.6 step 1) ----------------------------------------

async def test_the_status_names_the_home(home):
    await lease.set_status(home, "working")
    assert home.status and home.status["text"] == "working · proj · ~agents"
    _crown(home)
    await lease.set_status(home, "working")
    assert home.status["text"] == "coordinator · working · proj · ~agents"


async def test_a_long_label_is_cut_never_the_home(home):
    await lease.set_status(home, "working", "l" * 200)
    text = home.status["text"] if home.status else ""
    assert len(text) == lease.STATUS_MAX and text.endswith(" · ~agents")
    assert lease.home_of(text) == "agents"


def test_a_status_without_a_home_names_none():
    """An older plugin's status, or one set by hand: it counts everywhere."""
    assert lease.home_of("coordinator · working · proj · ~team-b") == "team-b"
    assert lease.home_of("coordinator · working · proj") is None
    assert lease.home_of("working · ~a · b") is None
    assert lease.home_of("") is None


# --- a crown per home (RT-6.6 steps 2-5) ---------------------------------------------------

async def test_one_channel_elects_as_before(home):
    """Parity: everyone's home is this channel; the older agent keeps the crown."""
    home.agent(OLDER, "agent-old", 1000, "crown", "coordinator · working · y · ~agents")
    assert await lease.command(home, "take") == (
        "pending: @agent-old still holds the crown; it passes to you once released or expired")
    await lease.command(home, "tick")
    assert not lease.crown_held(BOT)
    assert [r.username for r in await lease.roster(home)] == ["agent-old", "agent-me"]


async def test_an_old_format_status_counts_everywhere(home):
    """Parity (RISK-04): a status that names no home counts in this home's
    election: its crown blocks a claim, and two crowns still yield to the older."""
    home.agent(OLDER, "agent-old", 1000, "crown", "coordinator · working · y")
    await lease.command(home, "tick")
    assert not lease.crown_held(BOT)
    _crown(home)
    await lease.command(home, "tick")
    assert not lease.crown_held(BOT)
    assert (await lease.roster(home))[0] == RosterEntry("agent-old", "working",
                                                        coordinator=True, crown=True)


async def test_two_homes_keep_two_crowns(home):
    """The older agent here holds the crown of its own home: no conflict, the
    younger takes this home's crown and names itself in the header."""
    home.agent(OLDER, "agent-b", 1000, "crown", "coordinator · working · y · ~team-b")
    await lease.command(home, "tick")
    assert lease.crown_held(BOT)
    assert await lease.command(home, "who") == "agent-me"
    assert "👑 @agent-me since" in home.header


async def test_a_member_whose_home_is_elsewhere_never_claims_the_crown(home):
    """The oldest reachable member lives in another home: it counts neither
    for this home's election nor as its coordinator; the oldest of this home
    takes the free crown."""
    home.agent(OLDER, "agent-b", 1000, "hammer_and_wrench", "working · y · ~team-b")
    await lease.set_status(home, "working")
    await lease.command(home, "tick")
    assert lease.crown_held(BOT)
    rows = await lease.roster(home)
    assert [(r.username, r.coordinator) for r in rows] == [("agent-me", True)]


async def test_an_ambassador_is_listed_with_its_homes_crown(home):
    """ADR-002: a guest holding its own home's crown is listed as "👑 of ~B",
    never as this home's crown."""
    home.agent(OLDER, "agent-b", 1000, "crown", "coordinator · working · y · ~team-b")
    home.agent(YOUNGER, "agent-c", 3000, "hammer_and_wrench", "working · z · ~team-b")
    rows = await lease.roster(home)
    assert rows == [RosterEntry("agent-me", "working", coordinator=True),
                    RosterEntry("agent-b", "working", crown_of="team-b")]
    assert [r for r in tasks.roster(rows, [], {}) if r.get("crown")] == [
        {"agent": "agent-b", "status": "working", "crown": "👑 of ~team-b"}]
    gate._coord = None
    assert await gate.coordinator_name(home) == ""


# --- the 👑 shows only in the home (RT-6.6 step 5) ----------------------------------------

async def test_the_crown_shows_only_in_the_home(home):
    _crown(home)
    home.dms = {DM}
    assert await lease.crown_here(home, BOT)                      # the home, by default
    assert await lease.crown_here(home, BOT, WORK)
    assert await lease.crown_here(home, BOT, DM)                  # a DM: no channel's crown
    assert not await lease.crown_here(home, BOT, OTHER)           # a guest there
    home.posts = [{"id": "r" * 26, "channel_id": OTHER}]
    assert not await lease.crown_here(home, BOT, root_id="r" * 26)


async def test_a_post_outside_the_home_carries_no_crown(home):
    _crown(home)
    await server.post_as_bot(home, "STATUS · building", channel_id=OTHER)
    await server.post_as_bot(home, "STATUS · building")
    out, here = home.posted
    assert not out["message"].startswith("👑") and not (out["props"] or {}).get("a2a", {}).get("crown")
    assert here["message"].startswith("👑 ") and here["props"]["a2a"]["crown"]


# --- the coordinator coordinates only where it routes (RT-6.12 step 3, RT-6.16 step 2) ------

async def test_the_coordinator_routes_in_its_home_and_the_lobby_only(home):
    """Its home, and MM_CHANNEL until WP5 (ADR-002): there a plain post wakes
    it and the gate passes it; in another team channel it is a guest."""
    _crown(home)
    home.lobby, home.dms = LOBBY, {DM}
    assert await lease.routes(home) == {WORK, LOBBY}
    for cid, expected in ((WORK, True), (LOBBY, True), (DM, True), ("", True), (OTHER, False)):
        assert await lease.coordinates_in(home, BOT, cid) is expected, cid
    plain = {"kind": "watched_channel", "post": {"message": "deploy?"}}
    for cid, expected in ((WORK, True), (LOBBY, True), (OTHER, False)):
        item = plain | {channel.CHANNEL_ID: cid}
        assert channel.wakes(item, coordinator=True, in_my_thread=False,
                             routes={WORK, LOBBY}) is expected, cid
    # A mention wakes it anywhere; without routes, as before.
    assert channel.wakes({"kind": "mention", channel.CHANNEL_ID: OTHER}, coordinator=False,
                         in_my_thread=False, routes={WORK})
    assert channel.wakes(plain | {channel.CHANNEL_ID: OTHER}, coordinator=True, in_my_thread=False)


async def test_one_channel_routes_as_before(home):
    """Parity: no project channel, the home is the lobby; the crown routes there."""
    _crown(home)
    assert await lease.routes(home) == {WORK}
    assert await lease.coordinates_in(home, BOT, WORK)

"""The crown belongs to its home (ADR-002, RT-6.6 step 5, RT-6.26 step 9; #258).

An agent whose home is A but whose environment carries B's id as
MM_PROJECT_CHANNEL_ID has one home, A: the id counts only while it is the
project channel's. The crown file records the home it was claimed in
(CROWN_HOME); a crown recorded for another channel is none here. Never a 👑
prefix or props.a2a.crown outside the home, no ask-mode acknowledgement or
mandate hint there, and bus_info names the home whose crown it is. The
regular case, the crown in its home, keeps prefix, acknowledgement and duties.
"""

import json
import logging
import time

import pytest
from fakes import BOT, WORK
from test_ask_route import _Client, _switch
from test_homes import Home
from test_mandate_signed import _pinned_post, chat, pinned  # noqa: F401 — fixtures

from mattermost_ai_agent_bus import askroute, departed, lease, mandate, server
from mattermost_ai_agent_bus.client import MattermostAdapter
from mattermost_ai_agent_bus.homes import watched_channel_ids
from mattermost_ai_agent_bus.lease import crown_held as real_crown_held
from mattermost_ai_agent_bus.model import Channel

A = "a" * 26       # the home, ~chat-next
B = "z" * 26       # another team channel, ~ai-agent-bus-dev
LOBBY = "l" * 26
CROWN = "\U0001F451"


def _claim(home: str | None = WORK, uid: str = BOT) -> None:
    """A live crown, claimed in `home` (None: a file from before #258, no CROWN_HOME)."""
    lease._save_coord(uid, int(time.time()) + 600, 0, 0, home)


def _crowned(post: dict) -> bool:
    return post["message"].startswith(CROWN) or bool(
        ((post.get("props") or {}).get("a2a") or {}).get("crown"))


# --- one home: MM_PROJECT_CHANNEL_ID only when it is the project channel's -------------------

def _adapter(monkeypatch, name_of_id: str, project: str = "chat-next") -> MattermostAdapter:
    c = MattermostAdapter(base_url="https://mm.example", token="t", project_channel_name=project)
    reads: list[str] = []

    async def get_channel(cid):
        reads.append(cid)
        return Channel(id=cid, name=name_of_id, type="O")

    async def get_channel_id(name=None, team_name=None):
        return {None: A, "chat-next": A}.get(name, LOBBY)

    async def no_guests():
        return set()

    monkeypatch.setattr(c, "get_channel", get_channel)
    monkeypatch.setattr(c, "get_channel_id", get_channel_id)
    monkeypatch.setattr(c, "guest_channel_ids", no_guests)
    c.reads = reads  # type: ignore[attr-defined]
    return c


async def test_a_mismatched_project_channel_id_resolves_to_the_home(monkeypatch, caplog):
    """RT-6.6, ADR-002 (#258): MM_PROJECT_CHANNEL_ID of ~ai-agent-bus-dev with the
    home ~chat-next: the home is resolved by its name, the mismatch logged once,
    and the bus does not watch B as its home."""
    monkeypatch.setenv("MM_PROJECT_CHANNEL_ID", B)
    c = _adapter(monkeypatch, "ai-agent-bus-dev")
    with caplog.at_level(logging.WARNING):
        assert await c.work_channel_id() == A
        assert await c.work_channel_id() == A
    assert len([r for r in caplog.records if "MM_PROJECT_CHANNEL_ID" in r.getMessage()]) == 1
    ids = await watched_channel_ids(c)
    assert A in ids and B not in ids


async def test_a_matching_project_channel_id_is_the_home(monkeypatch):
    """RT-6.6, ADR-002, regular case: the id is the project channel's, kept."""
    monkeypatch.setenv("MM_PROJECT_CHANNEL_ID", B)
    assert await _adapter(monkeypatch, "chat-next").work_channel_id() == B


async def test_a_refused_project_channel_id_is_passed_over_once_and_not_read_again(monkeypatch):
    """RT-6.6, ADR-002: an id the chat refuses (gone, not allowed) is no home:
    the name decides, and the refusal is kept, no read per call."""
    from mattermost_ai_agent_bus.model import NotFound
    monkeypatch.setenv("MM_PROJECT_CHANNEL_ID", B)
    c = _adapter(monkeypatch, "unused")

    async def refused(cid):
        c.reads.append(cid)  # type: ignore[attr-defined]
        raise NotFound("channel: not there (404)")

    monkeypatch.setattr(c, "get_channel", refused)
    assert await c.work_channel_id() == A
    assert await c.work_channel_id() == A
    assert c.reads == [B]  # type: ignore[attr-defined]


async def test_a_passing_read_failure_keeps_the_project_channel_id(monkeypatch):
    """RT-6.6 fail-open, as the shell CLI: a read that fails for a passing
    reason keeps the id, and the next call reads again."""
    monkeypatch.setenv("MM_PROJECT_CHANNEL_ID", B)
    c = _adapter(monkeypatch, "unused")

    async def flaky(cid):
        c.reads.append(cid)  # type: ignore[attr-defined]
        raise OSError("connection reset")

    monkeypatch.setattr(c, "get_channel", flaky)
    assert await c.work_channel_id() == B
    assert await c.work_channel_id() == B
    assert c.reads == [B, B]  # type: ignore[attr-defined]


async def test_an_id_without_a_project_channel_name_is_taken_unread(monkeypatch):
    """RT-6.6 parity: only the id given, nothing to compare it with: no read."""
    monkeypatch.setenv("MM_PROJECT_CHANNEL_ID", B)
    c = _adapter(monkeypatch, "anything", project="")
    assert await c.work_channel_id() == B and c.reads == []


# --- the crown bound to its home -------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MM_BOT_USER_ID", BOT)
    monkeypatch.setenv("CORRAL_WORKDIR", "/work/proj")
    for var in ("MM_AGENT_PREFIX", "MM_BOT_USERNAME", "MM_REG_BOT_NAME", "MM_LEASE_SEC"):
        monkeypatch.delenv(var, raising=False)
    lease._TYPES.clear()
    c = Home(username="agent-me")
    c.agent(BOT, "agent-me", 2000)
    return c


async def test_with_crown_leaves_a_post_plain_unless_the_caller_says_here(home):
    """ADR-002 (#258): with_crown no longer assumes the home."""
    _claim()
    assert lease.with_crown("x", BOT) == "x"
    assert lease.with_crown("x", BOT, here=True).startswith(CROWN)


async def test_posts_outside_the_home_carry_no_crown(home):
    """RT-6.6 step 5, ADR-002: home A, a post in B and a reply in B's thread are
    plain; in the home the 👑 shows."""
    _claim()
    home.posts = [{"id": "r" * 26, "channel_id": B}]
    await server.post_as_bot(home, "STATUS · building", channel_id=B)
    await server.post_as_bot(home, "STATUS · building", root_id="r" * 26)
    await server.post_as_bot(home, "STATUS · building")
    in_b, reply_b, at_home = home.posted
    assert not _crowned(in_b) and not _crowned(reply_b)
    assert at_home["message"].startswith(CROWN + " ") and at_home["props"]["a2a"]["crown"]


async def test_a_crown_recorded_for_another_channel_is_none_here(home):
    """RT-6.6, ADR-002 (#258): a crown claimed while the home was B is no crown
    in A: no prefix anywhere, no routes, no crown_until for the hooks."""
    _claim(B)
    await lease.home_id(home)
    assert lease.crown_home(BOT) == B and not lease.crown_held(BOT)
    assert lease.crown_until(BOT) == 0
    for channel_id in ("", WORK, B):
        assert not await lease.crown_here(home, BOT, channel_id)
        assert not await lease.coordinates_in(home, BOT, channel_id)
    await server.post_as_bot(home, "STATUS · building", channel_id=B)
    await server.post_as_bot(home, "STATUS · building")
    assert not any(_crowned(p) for p in home.posted)


async def test_the_beat_drops_a_crown_of_another_channel_and_records_the_home(home):
    """RT-6.6 steps 1 and 6 (#258): the beat records CROWN_HOME; a crown
    recorded for another channel is dropped, not renewed."""
    _claim(None)  # a file from before: the lease alone, the beat adds the home
    assert lease.crown_held(BOT)
    await lease.set_status(home, "bus")
    assert lease.crown_home(BOT) == WORK and lease.crown_held(BOT)
    _claim(B)
    await lease.set_status(home, "bus")
    assert lease._coord(BOT).get("CROWN_UNTIL") == 0 and not lease.crown_held(BOT)


async def test_a_claim_records_its_home(home):
    """RT-6.6 step 2: a take writes CROWN_HOME with the crown."""
    assert await lease.command(home, "take") == "taken"
    assert lease.crown_home(BOT) == WORK and lease.crown_held(BOT)


async def test_departed_answers_outside_the_home_without_the_crown(home):
    """RT-6.21, ADR-002: the departed note in B's thread is plain, in the home 👑."""
    _claim()
    from mattermost_ai_agent_bus.client import post_of
    for channel_id, crowned in ((B, False), (WORK, True)):
        home.posted.clear()
        await departed.tell(home, post_of({"id": "p" * 26, "channel_id": channel_id}), BOT,
                            [departed.Elsewhere("agent-x", "")])
        assert _crowned(home.posted[0]) is crowned


# --- bus_info ---------------------------------------------------------------------------------

async def _bus_info(monkeypatch, c) -> dict:
    async def none(client):
        return []
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(server, "_guest_names", none)
    monkeypatch.setattr(server, "_own_channels", none)
    monkeypatch.setattr(server, "_newer_installed", lambda: None)
    return json.loads(await server.bus_info())


async def test_bus_info_names_the_home_whose_crown_it_is(monkeypatch):
    """RT-6.6, ADR-002: `crown` is `~<home>` in the home, false for a crown
    recorded for another channel, false without one."""
    class _Info(_Client):
        trusted_source = "MM_TRUSTED_HUMANS"
        channel_name = "agents"
        work_channel_name = "agents"

        async def username(self, uid):
            return self.names.get(uid, "")
    c = _Info()
    assert (await _bus_info(monkeypatch, c))["crown"] is False
    me = (await c.get_me()).id
    _claim(uid=me)
    assert (await _bus_info(monkeypatch, c))["crown"] == "~agents"
    _claim(B, uid=me)
    assert (await _bus_info(monkeypatch, c))["crown"] is False


# --- the duties: ask-mode switch and mandate post ---------------------------------------------

async def test_a_switch_in_another_channel_gets_no_acknowledgement():
    """RT-6.25 step 2, RT-6.6, ADR-002: a trusted human's switch in B is not the
    home's; the holder of A's crown says nothing there."""
    c = _Client()
    _claim()
    assert await askroute.observe(c, _switch(channel_id=B), BOT) is False
    assert c.posted == []


async def test_a_switch_in_the_home_is_acknowledged_by_its_crown():
    """RT-6.25 step 2, regular case: the 👑 of the home acknowledges, with the prefix."""
    c = _Client()
    _claim()
    assert await askroute.observe(c, _switch(), BOT) is True
    [ack] = c.posted
    assert ack["message"].startswith(CROWN + " ask-mode coordinator")


async def test_a_crown_of_another_channel_does_not_acknowledge_in_the_home():
    """RT-6.25 step 2, ADR-002 (#258): kept, but no acknowledgement."""
    c = _Client()
    _claim(B)
    assert await askroute.observe(c, _switch(), BOT) is True
    assert c.posted == []


async def test_a_mandate_post_in_another_channel_gets_no_hint(chat, monkeypatch):  # noqa: F811
    """RT-6.26 step 8, RT-6.26 step 9, ADR-002: the hint only from the home's crown, in the home."""
    monkeypatch.setattr(lease, "crown_held", real_crown_held)
    _claim()
    assert await mandate.observe(chat, _pinned_post(channel_id=B), BOT) is False
    assert chat.posted == []
    _claim(B)
    assert await mandate.observe(chat, _pinned_post(), BOT) is True
    assert chat.posted == []
    _claim()
    assert await mandate.observe(chat, _pinned_post(), BOT) is True
    [hint] = chat.posted
    assert hint["message"].startswith(CROWN + " ") and "mandates nothing" in hint["message"]

"""Owner classes (ADR-001, ADR-003; WP2 of #63): a peer agent's TASK binds,
a foreign agent's is a request (RT-6.16 steps 1, 3 and 5, RT-6.14 step 3).
Parity: without trusted humans, or without `for @` on my own bot, every bus
agent is a peer, as before."""

import json
import time

import pytest
from fakes import BOT, WORK, FakeChat

from mattermost_ai_agent_bus import channel, gate, owners, server
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import RosterEntry
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent, WebSocketInbox

ALICE, BOB, CAROL = "a" * 26, "b" * 26, "c" * 26
PEER, RELAY, OTHERS, NOOWNER, CUT, MUTE = (c * 26 for c in "pqrstv")
NAMES = {ALICE: "alice", BOB: "bob", CAROL: "carol", PEER: "agent-p", RELAY: "agent-r",
         OTHERS: "agent-o", NOOWNER: "agent-n", CUT: "agent-cut", MUTE: "agent-mute"}
AGENTS = {PEER, RELAY, OTHERS, NOOWNER, CUT, MUTE}
MINE = "for @alice · trusts @alice, @bob · via agent-registrar"
DESCRIPTIONS = {
    BOT: MINE,
    PEER: "for @bob · trusts @bob, @alice · via agent-registrar",
    RELAY: "for @alice · trusts @alice, @carol · via agent-registrar",   # B trusts Carol, I don't
    OTHERS: "for @carol · trusts @carol · via agent-registrar",
    NOOWNER: "Registered via agent-registrar",                          # no `for @`
    CUT: "for @alice · trusts @alice, @bob +3 · via agent-registrar",   # the registrar cut it
    # MUTE: no description readable (an API error)
}
ORDER = [PEER, RELAY, OTHERS, NOOWNER, CUT, MUTE]


def _chat(**kw):
    kw = {"names": NAMES, "agents": AGENTS, "trusted": {ALICE: "alice", BOB: "bob"},
          "descriptions": DESCRIPTIONS} | kw
    return FakeChat(**kw)


@pytest.fixture(autouse=True)
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", BOT)
    monkeypatch.delenv("MM_AGENT_PREFIX", raising=False)
    monkeypatch.setattr(gate, "_coord", None)


def _post(pid, user, message, *, root="", at=0):
    return post_of({"id": pid, "user_id": user, "channel_id": WORK, "root_id": root,
                    "message": message, "create_at": at or int(time.time() * 1000)})


async def _classes(chat):
    return await gate.author_classes(chat, [_post(f"{i}" * 26, u, "hi") for i, u in enumerate(ORDER)])


def test_the_description_names_owner_trusts_and_a_cut():
    """ADR-003: only `for @…`, `trusts @…` and a `+N` are read."""
    assert owners.parse(DESCRIPTIONS[RELAY]) == owners.Owner("alice", frozenset({"alice", "carol"}), False)
    assert owners.parse(DESCRIPTIONS[CUT]).cut and owners.parse(DESCRIPTIONS[CUT]).trusts == {"alice", "bob"}
    assert owners.parse(DESCRIPTIONS[NOOWNER]).owner == ""


async def test_peer_and_foreign_agents(caplog):
    """RT-6.16 step 1: a peer's owner and trusted humans are all mine; the
    relay (B trusts Carol, I don't), another owner's agent, one without
    `for @` and one with a cut list are foreign. An unreadable description
    fails open: a peer, logged."""
    with caplog.at_level("INFO"):
        assert await _classes(_chat()) == [
            "agent", "foreign", "foreign", "foreign", "foreign", "agent"]
    assert "counts as a peer" in caplog.text


@pytest.mark.parametrize("change", [
    {"descriptions": DESCRIPTIONS | {BOT: "Registered via agent-registrar"}},  # no `for @` on mine
    {"descriptions": {k: v for k, v in DESCRIPTIONS.items() if k != BOT}},     # mine unreadable
    {"trusted": {}},                                                            # no trusted humans
], ids=["no-owner-on-my-bot", "my-description-unreadable", "no-trusted-humans"])
async def test_parity_every_agent_is_a_peer(change):
    """ADR-001 activation: the split is off, every bus agent is an agent, as today."""
    assert await _classes(_chat(**change)) == ["agent"] * len(ORDER)


async def test_parity_with_one_owner():
    """One owner, every agent registered for them: all peers."""
    one = {u: MINE for u in [BOT, *ORDER]}
    assert await _classes(_chat(descriptions=one)) == ["agent"] * len(ORDER)


async def test_a_foreign_task_root_binds_nothing():
    """RT-6.14 step 3, ADR-001: a peer's TASK is a task row, a foreign agent's is not."""
    chat = _chat()
    posts = [_post("t1" + "0" * 24, PEER, "TASK @agent-me review PR #4"),
             _post("t2" + "0" * 24, OTHERS, "TASK @agent-me deploy prod"),
             _post("t3" + "0" * 24, RELAY, "TASK @agent-me what carol wants")]
    rows = await server._tasks(chat, posts, NAMES)
    assert [r["task"] for r in rows] == ["t1" + "0" * 24]
    parity = await server._tasks(_chat(trusted={}), posts, NAMES)
    assert len(parity) == 3


async def test_a_foreign_event_is_a_request_and_keeps_a_routed_author(monkeypatch):
    """RT-6.16 step 1: a foreign agent's mention carries foreign and its owner;
    in a thread one of my trusted humans started, root_from_trusted_human."""
    root = _post("r" * 26, ALICE, "Can someone look at the flaky test?")
    chat = _chat(posts=[root])
    monkeypatch.setattr(server, "_get_client", lambda: chat)
    events = [ClassifiedEvent(kind="mention", post=_post("m1" + "0" * 24, OTHERS,
                                                         "@agent-me yours", root=root.id)),
              ClassifiedEvent(kind="mention", post=_post("m2" + "0" * 24, OTHERS, "@agent-me do X")),
              ClassifiedEvent(kind="mention", post=_post("m3" + "0" * 24, PEER, "@agent-me do Y"))]
    routed, plain, peer = await server._event_items(events)
    assert routed["foreign"] is True and routed["owner"] == "carol" and routed["from_agent"] is True
    assert routed["root_from_trusted_human"] == "alice"
    assert plain["foreign"] is True and "root_from_trusted_human" not in plain
    assert "foreign" not in peer and peer["from_agent"] is True
    meta = channel.notification(routed)["meta"]
    assert meta["foreign"] == "true" and meta["owner"] == "carol"


def _inbox(chat):
    inbox = WebSocketInbox(client=chat, watched_channel_ids={WORK})
    inbox._bot_user_id, inbox._bot_username = BOT, "agent-me"
    return inbox


async def _gated(inbox, event):
    await inbox._gate(event, "O")
    for t in list(inbox._gate_tasks):
        await t
    return event.gated


async def test_foreign_broadcasts_are_gated_its_mentions_wake():
    """RT-6.16 steps 2-3: a foreign agent's @channel or to=all is data; its
    mention wakes the worker (as a request). A peer's broadcast wakes."""
    chat = _chat()
    inbox = _inbox(chat)
    gated = await _gated(inbox, ClassifiedEvent(kind="watched_channel", post=_post(
        "g1" + "0" * 24, OTHERS, "@channel everyone deploy")))
    assert gated.startswith("from agent-o, an agent of another owner")
    assert chat.posted == []  # a broadcast gets no answer
    to_all = post_of({"id": "g2" + "0" * 24, "user_id": OTHERS, "channel_id": WORK,
                      "message": "TASK for everyone", "props": {"a2a": {"v": 1, "to": "all"}}})
    assert await _gated(inbox, ClassifiedEvent(kind="watched_channel", post=to_all))
    assert not await _gated(inbox, ClassifiedEvent(kind="mention", post=_post(
        "g3" + "0" * 24, OTHERS, "@agent-me please review")))
    assert not await _gated(inbox, ClassifiedEvent(kind="watched_channel", post=_post(
        "g4" + "0" * 24, PEER, "@channel everyone deploy")))


@pytest.mark.parametrize("named,answered", [("agent-r", False), ("agent-p", True)])
async def test_not_woken_stays_silent_when_a_mentioned_agent_trusts_the_author(
        monkeypatch, named, answered):
    """RT-6.16 step 3: Carol is not mine; agent-r trusts her and answers, so
    no "Not woken" here. agent-p does not trust her: the answer goes out."""
    chat = _chat()

    async def roster(_client):
        return [RosterEntry("agent-r", user_id=RELAY), RosterEntry("agent-p", user_id=PEER)]

    monkeypatch.setattr(gate.lease, "roster", roster)
    gated = await _gated(_inbox(chat), ClassifiedEvent(kind="mention", post=_post(
        "h1" + "0" * 24, CAROL, f"@agent-me @{named} build it")))
    assert gated
    assert bool(chat.posted) is answered


async def test_roster_rows_carry_owner_trusts_and_foreign(monkeypatch):
    """RT-6.14 step 4: from the bot description, never from the HELLO."""
    chat = _chat()

    async def roster(_client):
        return [RosterEntry("agent-p", user_id=PEER), RosterEntry("agent-o", user_id=OTHERS),
                RosterEntry("agent-mute", user_id=MUTE)]

    monkeypatch.setattr(server.lease, "roster", roster)
    monkeypatch.setattr(server, "_get_client", lambda: chat)
    rows = json.loads(await server.coordinator("roster"))["roster"]
    assert rows[0] == {"agent": "agent-p", "owner": "bob", "trusts": ["alice", "bob"]}
    assert rows[1] == {"agent": "agent-o", "owner": "carol", "trusts": ["carol"], "foreign": True}
    assert rows[2] == {"agent": "agent-mute"}


async def test_hello_warns_about_a_foreign_coordinator(monkeypatch):
    """ADR-001: the crown routes, it never authorises; the HELLO's answer says so."""
    chat = _chat(post_id="h" * 26)
    monkeypatch.setattr(server, "_get_client", lambda: chat)
    monkeypatch.setattr(server.lease, "crown_held", lambda uid: False)

    async def roster(_client):
        return [RosterEntry("agent-o", crown=True, coordinator=True, user_id=OTHERS)]

    monkeypatch.setattr(server.lease, "roster", roster)
    out = json.loads(await server.announce(project="p", task="t", model="m"))
    assert "agent-o is an agent of another owner (@carol)" in out["warning"]

    async def peer_roster(_client):
        return [RosterEntry("agent-p", crown=True, coordinator=True, user_id=PEER)]

    monkeypatch.setattr(server.lease, "roster", peer_roster)
    assert "warning" not in json.loads(await server.announce(project="p", task="t", model="m"))

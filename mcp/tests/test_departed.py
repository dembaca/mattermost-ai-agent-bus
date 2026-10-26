"""RT-6.21: a mention of a departed agent is answered once, by the coordinator's server."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import pytest

from mattermost_ai_agent_bus import channel, departed, gate, server, session_file
from mattermost_ai_agent_bus.client import MattermostClient
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent, WebSocketInbox, classify_post

ME, OLD, IDLE, WORKER, HUMAN = "m" * 26, "o" * 26, "i" * 26, "w" * 26, "h" * 26
PROJ, HELLO, BYE, LAST = "p" * 26, "e" * 26, "b" * 26, "l" * 26
BASE = "https://chat.example/team/pl/"


def _status(emoji, minutes):
    at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return {"customStatus": json.dumps({"emoji": emoji, "text": "", "duration": "date_and_time",
                                        "expires_at": at.isoformat()})}


class Bus:
    """The inbox of agent-me against a fake Mattermost: agent-old said BYE,
    agent-idle is 💤 on a live lease, agent-w works."""

    def __init__(self, monkeypatch):
        self.client = MattermostClient(base_url="https://chat.example", token="bot", team_name="team",
                                       project_channel_name="proj")
        self.posted = []
        self.fail = False
        self.users = {
            ME: {"id": ME, "username": "agent-me", "create_at": 3, "props": _status("crown", 10)},
            OLD: {"id": OLD, "username": "agent-old", "create_at": 1, "props": _status("ear", 10)},
            IDLE: {"id": IDLE, "username": "agent-idle", "create_at": 2, "props": _status("zzz", 10)},
            WORKER: {"id": WORKER, "username": "agent-w", "create_at": 4, "props": _status("hammer_and_wrench", 10)},
        }
        now = int(time.time() * 1000)
        self.posts = [  # newest first
            {"id": LAST, "user_id": IDLE, "root_id": "", "message": "IDLE · waiting", "create_at": now - 5000},
            {"id": BYE, "user_id": OLD, "root_id": HELLO, "message": "BYE · done, PR #9 open", "create_at": now - 60_000},
            {"id": HELLO, "user_id": OLD, "root_id": "", "message": "HELLO @agent-old\ntask: x", "create_at": now - 600_000},
        ]

        async def api(method, path, json=None, params=None, **_):
            if path == "/api/v4/users":
                return [dict(u, delete_at=0) for u in self.users.values()]
            if path == "/api/v4/users/status/ids":
                return [{"user_id": i, "status": "online"} for i in json]
            if path == f"/api/v4/channels/{PROJ}/posts":
                return {"order": [p["id"] for p in self.posts], "posts": {p["id"]: p for p in self.posts}}
            if path.startswith("/api/v4/posts/"):
                return {"id": path.rsplit("/", 1)[1], "channel_id": PROJ}
            if path == "/api/v4/posts":
                if self.fail:
                    raise RuntimeError("down")
                self.posted.append(json)
                return {"id": "n" * 26, "user_id": ME, **json}
            raise AssertionError(f"unexpected {method} {path}")

        monkeypatch.setattr(self.client, "_api", api)
        monkeypatch.setattr(gate, "_coord", None)
        self.inbox = WebSocketInbox(client=self.client, watched_channel_ids={PROJ})
        self.inbox._bot_user_id, self.inbox._bot_username = ME, "agent-me"

    async def arrive(self, pid, message, *, user=HUMAN, root="", props=None):
        post = {"id": pid, "user_id": user, "channel_id": PROJ, "root_id": root,
                "message": message, "props": props or {}}
        kind = classify_post(post, bot_user_id=ME, bot_username="agent-me", channel_type="O",
                             watched_channel_ids={PROJ})
        event = ClassifiedEvent(kind=kind, post=post)
        await self.inbox._departed(event)
        await asyncio.gather(*list(self.inbox._gate_tasks))
        return event


def _crown(uid=ME):
    d = session_file.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"coord-{uid}.env").write_text(f"CROWN_UNTIL={int(time.time()) + 600}\n")


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.setenv("MM_PROJECT_CHANNEL_ID", PROJ)
    monkeypatch.setenv("MM_BOT_USER_ID", ME)
    b = Bus(monkeypatch)
    monkeypatch.setattr(server, "_get_client", lambda: b.client)
    return b


def test_mentions_are_the_bus_agents_but_me():
    """RT-6.21: only names with the registrar's prefix, each once, not my own."""
    assert departed.mentioned("@agent-old, @agent-me and @dembaca: ask @Agent-Old. @agent-x.", "agent-me") == [
        "agent-old", "agent-x"]


@pytest.mark.asyncio
async def test_bye_is_answered_once_by_the_coordinator_and_wakes_it(bus):
    """RT-6.21 steps 2–4: the 👑's server says since when and links the BYE,
    once per thread and agent; the event wakes the coordinator, even when it
    is an agent's fyi post."""
    _crown()
    e = await bus.arrive("q" * 26, "ACK @agent-old over to you", user=WORKER, props={"a2a": {"intent": "fyi"}})
    assert e.departed == "agent-old"
    [reply] = bus.posted
    assert reply["root_id"] == "q" * 26 and reply["message"].startswith("👑 agent-old is gone since ")
    assert f"(BYE), see its handoff: {BASE}{BYE}. I route this instead." in reply["message"]
    [item] = await server._event_items([e])
    assert item["departed"] == "agent-old"
    assert channel.wakes(item | {"intent": "fyi"}, coordinator=True, in_my_thread=False)
    assert channel.notification(item)["meta"]["departed"] == "agent-old"
    await bus.arrive("r" * 26, "@agent-old again?", root="q" * 26)
    assert len(bus.posted) == 1  # that thread knows
    await bus.arrive("s" * 26, "@agent-old?")
    assert len(bus.posted) == 2


@pytest.mark.asyncio
async def test_a_lease_that_ran_out_links_the_last_post(bus):
    """RT-6.21 step 3: no live status: gone since the lease's end."""
    _crown()
    bus.users[IDLE]["props"] = _status("zzz", -30)
    await bus.arrive("q" * 26, "@agent-idle status?")
    [reply] = bus.posted
    assert "agent-idle is gone since " in reply["message"]
    assert f"(its lease ran out), see its last post: {BASE}{LAST}." in reply["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["agent-idle", "agent-w"])
async def test_idle_and_listening_agents_are_not_departed(bus, name):
    """RT-6.21 step 3: 💤 idle on a live lease is not gone; nor is a working agent."""
    _crown()
    e = await bus.arrive("q" * 26, f"@{name} status?")
    assert not e.departed and bus.posted == []


@pytest.mark.asyncio
async def test_a_new_hello_after_bye_is_back(bus):
    """RT-6.21 step 3: only the newest HELLO thread counts."""
    _crown()
    bus.posts.insert(0, {"id": "f" * 26, "user_id": OLD, "root_id": "", "message": "HELLO @agent-old",
                         "create_at": int(time.time() * 1000)})
    e = await bus.arrive("q" * 26, "@agent-old status?")
    assert not e.departed and bus.posted == []


@pytest.mark.asyncio
async def test_gone_from_the_channel_counts_unknown_names_do_not(bus):
    """RT-6.21 step 3: removed from the work channel with its HELLO still there
    is gone; a name the bus never saw gets no answer."""
    _crown()
    del bus.users[OLD]
    bus.posts.pop(1)  # no BYE: the lease decides, and there is none
    await bus.arrive("q" * 26, "@agent-old and @agent-nobody?")
    [reply] = bus.posted
    assert reply["message"].startswith("👑 agent-old is gone since ") and "nobody" not in reply["message"]
    assert f"its last post: {BASE}{HELLO}." in reply["message"]


@pytest.mark.asyncio
async def test_only_the_coordinator_answers(bus):
    """RT-6.21 step 2: with a live 👑 elsewhere a worker's server stays out."""
    bus.users[ME]["props"] = _status("hammer_and_wrench", 10)
    bus.users[WORKER]["props"] = _status("crown", 10)
    e = await bus.arrive("q" * 26, "@agent-old?")
    assert not e.departed and bus.posted == []


@pytest.mark.asyncio
@pytest.mark.parametrize("oldest,answers", [(True, True), (False, False)])
async def test_without_a_crown_the_oldest_reachable_agent_answers(bus, oldest, answers):
    """RT-6.21 step 2: no 👑 anywhere: the oldest reachable agent's server,
    the one the lease hands the crown to next."""
    bus.users[ME]["props"] = _status("hammer_and_wrench", 10)
    bus.users[OLD]["props"] = {}  # gone: no live status, not reachable
    bus.users[ME]["create_at"] = 1 if oldest else 9
    e = await bus.arrive("q" * 26, "@agent-old?")
    assert bool(e.departed) is answers and len(bus.posted) == int(answers)
    if answers:
        assert bus.posted[0]["message"].startswith("agent-old is gone")


@pytest.mark.asyncio
async def test_a_failed_answer_is_tried_again(bus):
    """RT-6.21 error handling: not posted, not remembered; the wake-up stands."""
    _crown()
    bus.fail = True
    e = await bus.arrive("q" * 26, "@agent-old?")
    assert e.departed == "agent-old" and bus.posted == []
    bus.fail = False
    await bus.arrive("r" * 26, "@agent-old?", root="q" * 26)
    assert len(bus.posted) == 1

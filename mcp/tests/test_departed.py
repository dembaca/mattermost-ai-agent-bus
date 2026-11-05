"""RT-6.21: a mention of a departed agent, or of a live one not in the
channel, is answered once, by the coordinator's server."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fakes import WORK, FakeChat
from mattermost_ai_agent_bus import channel, departed, gate, server, session_file
from mattermost_ai_agent_bus.client import MattermostAdapter, post_of
from mattermost_ai_agent_bus.model import ChannelUser
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent, WebSocketInbox, classify_post

ME, OLD, IDLE, WORKER, HUMAN = "m" * 26, "o" * 26, "i" * 26, "w" * 26, "h" * 26
FAR, BOB = "x" * 26, "y" * 26
PROJ, HELLO, BYE, LAST = "p" * 26, "e" * 26, "b" * 26, "l" * 26
BASE = "https://chat.example/team/pl/"


def _status(emoji, minutes, text=""):
    at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return {"customStatus": json.dumps({"emoji": emoji, "text": text, "duration": "date_and_time",
                                        "expires_at": at.isoformat()})}


class _FakeWs:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class Bus:
    """The inbox of agent-me against a fake Mattermost: agent-old said BYE
    and has no status left,
    agent-idle is 💤 on a live lease, agent-w works. Not in the channel:
    agent-far, live with its home ~bgl-infra, and agent-bob, a person."""

    def __init__(self, monkeypatch):
        self.client = MattermostAdapter(base_url="https://chat.example", token="bot", team_name="team",
                                       project_channel_name="proj")
        self.posted = []
        self.fail = False
        self.users = {
            ME: {"id": ME, "username": "agent-me", "create_at": 3, "props": _status("crown", 10)},
            OLD: {"id": OLD, "username": "agent-old", "create_at": 1, "props": {}},
            IDLE: {"id": IDLE, "username": "agent-idle", "create_at": 2, "props": _status("zzz", 10)},
            WORKER: {"id": WORKER, "username": "agent-w", "create_at": 4, "props": _status("hammer_and_wrench", 10)},
        }
        self.others = {
            FAR: {"id": FAR, "username": "agent-far", "create_at": 5, "is_bot": True,
                  "props": _status("hammer_and_wrench", 10, "on #12 · ~bgl-infra")},
            BOB: {"id": BOB, "username": "agent-bob", "create_at": 6, "props": {}},
        }
        self.calls = []
        now = int(time.time() * 1000)
        self.posts = [  # newest first
            {"id": LAST, "user_id": IDLE, "root_id": "", "message": "IDLE · waiting", "create_at": now - 5000},
            {"id": BYE, "user_id": OLD, "root_id": HELLO, "message": "BYE · done, PR #9 open", "create_at": now - 60_000},
            {"id": HELLO, "user_id": OLD, "root_id": "", "message": "HELLO @agent-old\ntask: x", "create_at": now - 600_000},
        ]

        async def api(method, path, json=None, params=None, **_):
            self.calls.append(path)
            if path == "/api/v4/users/usernames":  # members or not; is_bot as Mattermost has it
                every = list(self.users.values()) + list(self.others.values())
                return [dict(u, is_bot=u.get("is_bot", u["id"] != BOB), delete_at=0)
                        for u in every if u["username"] in json]
            if path.startswith(f"/api/v4/channels/{PROJ}/members/"):
                if path.rsplit("/", 1)[1] in self.users:
                    return {"channel_id": PROJ, "user_id": path.rsplit("/", 1)[1]}
                request = httpx.Request(method, "https://chat.example" + path)
                raise httpx.HTTPStatusError("404", request=request,
                                            response=httpx.Response(404, request=request))
            if path == "/api/v4/users":
                return [dict(u, delete_at=0) for u in self.users.values()]
            if path == "/api/v4/channels/direct":
                return {"id": "d" * 26, "type": "D"}
            if path == "/api/v4/users/status/ids":
                return [{"user_id": i, "status": "online"} for i in json]
            if path == "/api/v4/users/ids":  # accounts outlive their channel membership
                names = {ME: "agent-me", OLD: "agent-old", IDLE: "agent-idle", WORKER: "agent-w"}
                return [{"id": i, "username": names[i]} for i in json if i in names]
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
        post = post_of({"id": pid, "user_id": user, "channel_id": PROJ, "root_id": root,
                        "message": message, "props": props or {}})
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
    assert reply["root_id"] == "q" * 26 and reply["message"].startswith("\U0001F451 agent-old is gone since ")
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
async def test_a_bye_with_a_live_lease_is_not_departed(bus):
    """RT-6.21 step 3: BYE may mean "task finished": a live lease keeps it here."""
    _crown()
    bus.users[OLD]["props"] = _status("ear", 10)
    e = await bus.arrive("q" * 26, "@agent-old status?")
    assert not e.departed and bus.posted == []


@pytest.mark.asyncio
async def test_only_a_bye_in_the_newest_hello_thread_is_the_handoff(bus):
    """RT-6.21 step 3: gone after a new HELLO: its last post, not the old BYE."""
    _crown()
    bus.posts.insert(0, {"id": "f" * 26, "user_id": OLD, "root_id": "", "message": "HELLO @agent-old",
                         "create_at": int(time.time() * 1000)})
    e = await bus.arrive("q" * 26, "@agent-old status?")
    assert e.departed == "agent-old"
    assert f"(its lease ran out), see its last post: {BASE}{'f' * 26}." in bus.posted[0]["message"]


@pytest.mark.asyncio
async def test_gone_from_the_channel_counts_unknown_names_do_not(bus):
    """RT-6.21 step 3: removed from the work channel with its HELLO still there
    is gone; a name the bus never saw gets no answer."""
    _crown()
    del bus.users[OLD]
    bus.posts.pop(1)  # no BYE: the lease decides, and there is none
    await bus.arrive("q" * 26, "@agent-old and @agent-nobody?")
    [reply] = bus.posted
    assert reply["message"].startswith("\U0001F451 agent-old is gone since ") and "nobody" not in reply["message"]
    assert f"its last post: {BASE}{HELLO}." in reply["message"]


@pytest.mark.asyncio
async def test_gone_from_the_channel_found_by_its_hellos_author(bus):
    """RT-6.21 step 3, RT-6.14 step 1: the three-line HELLO names nobody; a
    removed member's HELLO is found by its author."""
    _crown()
    del bus.users[OLD]
    bus.posts.pop(1)
    bus.posts[-1]["message"] = "HELLO · **x**\n`bus` · `–` · Opus · member\nfor h · trusts – · can: –"
    await bus.arrive("q" * 26, "@agent-old?")
    [reply] = bus.posted
    assert reply["message"].startswith("\U0001F451 agent-old is gone since ")
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


# --- not here: a live agent that is no member of the channel ----------------------------

MEMBERS = f"/api/v4/channels/{PROJ}/members/"
NAMED = "/api/v4/users/usernames"


@pytest.mark.asyncio
async def test_a_live_agent_not_in_the_channel_is_told_with_its_home(bus):
    """RT-6.21 steps 3–5: a live agent that is no member of the channel gets
    one answer in the thread, with the home its status names; the event wakes
    the coordinator with `not_here`. It costs one read by name and one of
    its membership."""
    _crown()
    e = await bus.arrive("q" * 26, "@agent-far wäre das")
    assert e.not_here == "agent-far (~bgl-infra)" and not e.departed
    [reply] = bus.posted
    assert reply["root_id"] == "q" * 26
    assert reply["message"] == ("\U0001F451 agent-far is not in ~proj (home ~bgl-infra): "
                                "add it to the channel, or I forward it.")
    assert bus.calls.count(NAMED) == 1 and bus.calls.count(MEMBERS + FAR) == 1
    [item] = await server._event_items([e])
    assert item["not_here"] == "agent-far (~bgl-infra)" and "departed" not in item
    assert channel.wakes(item, coordinator=True, in_my_thread=False)
    assert channel.notification(item)["meta"]["not_here"] == "agent-far (~bgl-infra)"


@pytest.mark.asyncio
async def test_not_here_is_told_once_per_thread(bus):
    """RT-6.21 step 4: once per thread and agent, the record departed uses;
    mentioned again within a minute, the adapter reads nothing again."""
    _crown()
    await bus.arrive("q" * 26, "@agent-far?")
    e = await bus.arrive("r" * 26, "@agent-far again?", root="q" * 26)
    assert e.not_here and len(bus.posted) == 1
    assert bus.calls.count(NAMED) == 1 and bus.calls.count(MEMBERS + FAR) == 1
    await bus.arrive("s" * 26, "@agent-far?")
    assert len(bus.posted) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["agent-w", "agent-idle"])
async def test_a_member_gets_no_answer_and_costs_no_read(bus, name):
    """RT-6.21 step 3: a name in the channel's roster is here: no lookup by
    name, no membership read, no answer."""
    _crown()
    e = await bus.arrive("q" * 26, f"@{name} status?")
    assert not e.not_here and not e.departed and bus.posted == []
    assert NAMED not in bus.calls and not any(c.startswith(MEMBERS) for c in bus.calls)


@pytest.mark.asyncio
async def test_a_person_not_in_the_channel_is_left_to_mattermost(bus):
    """RT-6.21 step 3: a person outside the channel, even with the agents'
    prefix, gets no answer; a name without the prefix is not even read."""
    _crown()
    e = await bus.arrive("q" * 26, "@agent-bob and @dembaca, see this")
    assert not e.not_here and not e.departed and bus.posted == []
    assert not any(c.startswith(MEMBERS) for c in bus.calls)


@pytest.mark.asyncio
async def test_gone_keeps_its_answer_beside_one_not_here(bus):
    """RT-6.21 step 3: a name is gone or not here, never both. A gone agent
    keeps the departed text, also one that left the channel and lives on
    elsewhere; one answer holds both."""
    _crown()
    bus.others[OLD] = dict(bus.users.pop(OLD), is_bot=True,
                           props=_status("hammer_and_wrench", 10, "x · ~other"))
    e = await bus.arrive("q" * 26, "@agent-old and @agent-far?")
    assert e.departed == "agent-old" and e.not_here == "agent-far (~bgl-infra)"
    [reply] = bus.posted
    assert reply["message"].startswith("\U0001F451 agent-old is gone since ")
    assert reply["message"].endswith(
        f"(BYE), see its handoff: {BASE}{BYE}. I route this instead. "
        "agent-far is not in ~proj (home ~bgl-infra): add it to the channel, or I forward it.")
    assert MEMBERS + OLD not in bus.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("gated", ["", "untrusted"])
async def test_the_answering_crown_types_in_the_thread(bus, monkeypatch, gated):
    """RT-6.21 step 5, RT-6.5: the post is now the coordinator's task, so it
    shows typing in that thread, as for a human's plain post; a gated post
    is no task."""
    _crown()
    ws = _FakeWs()
    bus.inbox._ws = ws
    monkeypatch.setattr(server, "_get_inbox", lambda: bus.inbox)
    e = await bus.arrive("r" * 26, "@agent-far wäre das", root="q" * 26)
    e.gated = gated
    await server._typing_for_task([e], await server._event_items([e]))
    await asyncio.sleep(0)
    bus.inbox.stop_typing()
    typing = [m["data"] for m in ws.sent if m["action"] == "user_typing"]
    assert typing[:2] == ([] if gated else [{"channel_id": PROJ, "parent_id": "q" * 26},
                                            {"channel_id": PROJ, "parent_id": ""}])


@pytest.mark.asyncio
async def test_not_here_takes_a_live_lease_and_no_membership():
    """RT-6.21 step 3, through the port: a live bot outside the channel is not
    here, the home left out when its status names none; a member (also one
    beyond the roster's reach) or one without a live lease is not."""
    def user(minutes):
        at = datetime.now(timezone.utc) + timedelta(minutes=minutes)
        return ChannelUser(id=FAR, username="agent-far", custom_status={
            "emoji": "hammer_and_wrench", "text": "on #12", "duration": "date_and_time",
            "expires_at": at.isoformat()})

    client = FakeChat()
    client.bots = {"agent-far": user(10)}
    found = await departed.elsewhere(client, WORK, ["agent-far"])
    assert found == [departed.Elsewhere("agent-far", "")]
    assert departed.reply_text(found, "", client.work_channel_name) == (
        "agent-far is not in ~agents: add it to the channel, or I forward it.")
    client.members = [client.bots["agent-far"]]
    assert await departed.elsewhere(client, WORK, ["agent-far"]) == []
    client.members, client.bots = [], {"agent-far": user(-1)}
    assert await departed.elsewhere(client, WORK, ["agent-far"]) == []


# --- RT-6.14 step 10: a task to an agent that is not reachable --------------------

@pytest.mark.asyncio
async def test_an_outgoing_task_names_who_is_not_reachable(bus):
    got = await departed.unreachable(
        bus.client, "TASK for @agent-old, @agent-idle, @agent-w, @agent-far, @agent-nobody, @agent-me, @dembaca",
        "agent-me", PROJ)
    assert got[0].startswith("agent-old (gone since ") and "BYE" in got[0]
    assert got[1:] == ["agent-idle (idle, until its human resumes)",
                       "agent-far (live in ~bgl-infra, not in this channel)",
                       "agent-nobody (not in this channel, no agent of this bus by that name that I can see)"]


@pytest.mark.asyncio
async def test_an_offline_agent_and_one_with_no_mention_or_only_reachable_ones(bus, monkeypatch):
    async def offline(ids):
        return {i: ("offline" if i == WORKER else "online") for i in ids}
    monkeypatch.setattr(bus.client, "presences", offline)
    assert await departed.unreachable(bus.client, "TASK @agent-w", "agent-me", PROJ) == ["agent-w (offline)"]
    monkeypatch.undo()
    assert await departed.unreachable(bus.client, "TASK for nobody", "agent-me", PROJ) == []
    assert await departed.unreachable(bus.client, "TASK @agent-w", "agent-me", PROJ) == []


@pytest.mark.asyncio
async def test_unreadable_agents_give_no_warning(bus):
    bus.fail = False
    async def broken(channel_id):
        raise RuntimeError("down")
    bus.client.channel_users = broken
    assert await departed.unreachable(bus.client, "TASK @agent-old", "agent-me", PROJ) == []


@pytest.fixture
def posting(bus, monkeypatch):
    """The bus, with what a tool's post reads besides the roster: the bot, its team and channels."""
    inner = bus.client._api

    async def api(method, path, json=None, params=None, **kw):
        if path == "/api/v4/users/me":
            return {"id": ME, "username": "agent-me", "is_bot": True}
        if path == "/api/v4/teams/name/team":
            return {"id": "t" * 26, "name": "team"}
        if path == "/api/v4/users/me/channels" or path.endswith("/channels"):
            return []
        if path == "/api/v4/channels/" + "d" * 26:
            return {"id": "d" * 26, "type": "D"}
        if "/channels/name/" in path:
            return {"id": PROJ, "name": "proj", "type": "O"}
        return await inner(method, path, json=json, params=params, **kw)

    monkeypatch.setattr(bus.client, "_api", api)
    return bus


@pytest.mark.asyncio
@pytest.mark.parametrize("word, warned", [("TASK", True), ("DELEGATE", True), ("ACK", False), ("RESULT", False),
                                          ("IDLE", False), ("", False)])
async def test_only_a_task_or_delegate_is_checked_and_it_is_posted_anyway(posting, word, warned):
    bus = posting
    out = await server.post_as_bot(bus.client, f"{word} @agent-idle please look".strip(), kind="")
    assert bus.posted, "the post goes out in every case"
    assert ("not_reachable" in out) == warned
    if warned:
        assert out["not_reachable"] == ["agent-idle (idle, until its human resumes)"]
        assert out["warning"].startswith(f"this {word} names agents the roster does not show as reachable")


@pytest.mark.asyncio
async def test_a_reply_checks_the_channel_of_its_thread_and_a_dm_not_at_all(posting):
    bus = posting
    out = await server.reply_as_bot(bus.client, HELLO, "TASK @agent-old go")
    assert [n.split(" ")[0] for n in out["not_reachable"]] == ["agent-old"]
    out = await server.post_as_bot(bus.client, "TASK @agent-old go", to="agent-w")
    assert "not_reachable" not in out


GUEST = "g" * 26


@pytest.mark.asyncio
async def test_a_channel_that_is_not_the_home_decides_by_its_own_roster(bus):
    """A guest channel or the lobby: the agents of that channel count, not the home's."""
    everyone = await bus.client.channel_users(PROJ)
    only_w = [u for u in everyone if u.username == "agent-w"]

    async def users(channel_id):
        return only_w if channel_id == GUEST else everyone
    bus.client.channel_users = users

    async def member(channel_id, user_id):
        return channel_id != GUEST
    bus.client.is_member = member
    got = await departed.unreachable(bus.client, "TASK @agent-w @agent-idle", "agent-me", GUEST)
    assert got == ["agent-idle (live in another channel, not in this channel)"]
    assert await departed.unreachable(bus.client, "TASK @agent-w @agent-idle", "agent-me", PROJ) == [
        "agent-idle (idle, until its human resumes)"]


@pytest.mark.asyncio
async def test_a_member_beyond_the_first_page_of_a_big_channel_is_reachable(bus):
    """A channel of more than 200 accounts lists a page: a live member the page lacks is no absent one."""
    everyone = await bus.client.channel_users(PROJ)

    async def page(channel_id):
        return [u for u in everyone if u.username != "agent-w"]
    bus.client.channel_users = page

    async def member(channel_id, user_id):
        return True
    bus.client.is_member = member
    assert await departed.unreachable(bus.client, "TASK @agent-w", "agent-me", PROJ) == []

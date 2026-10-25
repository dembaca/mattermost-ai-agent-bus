"""RT-6.19 (docs/arc42/06-runtime-view.md): the coordinator's server notices a
stall and wakes it; open asks remind their human. Fake clocks throughout."""

import asyncio
import json
import time

import pytest

from mattermost_ai_agent_bus import a2a, asks, channel, hookapi, lease, remind, server, stall, stats
from mattermost_ai_agent_bus.model import RosterEntry
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

ME = "c" * 26          # the coordinator
BOB = "b" * 26         # an agent
EVE = "e" * 26         # another agent
HUMAN = "h" * 26       # a trusted human
NOW = float(int(time.time()))


def pid(n: int) -> str:
    return f"{n:026d}"


def post(n: int, user: str, message: str, ago_min: float, root: int = 0, kind: str = "") -> dict:
    p = {"id": pid(n), "user_id": user, "message": message,
         "create_at": int((NOW - ago_min * 60) * 1000), "root_id": pid(root) if root else ""}
    props = a2a.props(message, kind=kind)
    if props:
        p["props"] = props
    return p


class Client:
    token = "t"
    session = None

    def __init__(self):
        self.posts: list[dict] = []
        self.names = {ME: "agent-coord", BOB: "agent-bob", EVE: "agent-eve", HUMAN: "dembaca"}
        self.trusted = {HUMAN: "dembaca"}
        self.reacts: dict[str, list[dict]] = {}
        self.sent: list[dict] = []

    async def list_recent(self, per_page=20, since_ms=None, channel_id=None):
        return sorted(self.posts, key=lambda p: p["create_at"])

    async def usernames(self, ids):
        return {i: self.names[i] for i in ids if i in self.names}

    async def trusted_humans(self):
        return self.trusted

    async def agents(self, ids):
        return {i for i in ids if i in (ME, BOB, EVE)}

    async def reactions(self, post_id):
        return self.reacts.get(post_id, [])

    async def thread(self, post_id):
        return {"posts": [p for p in self.posts if post_id in (p["id"], p.get("root_id"))]}

    async def post(self, message, *, channel_id=None, root_id=None, props=None):
        self.sent.append({"message": message, "root_id": root_id, "props": props})
        return {"id": pid(900 + len(self.sent)), "message": message}


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.setenv("MM_AGENT_PREFIX", "agent-")
    monkeypatch.setenv("MM_BOT_USER_ID", ME)
    client = Client()
    roster = {"agent-coord": "listening", "agent-bob": "listening", "agent-eve": "listening"}

    async def fake_roster(_client):
        return [RosterEntry(username=u, status=s, coordinator=u == "agent-coord", crown=u == "agent-coord")
                for u, s in roster.items()]

    monkeypatch.setattr(lease, "roster", fake_roster)
    woken: list[str] = []

    def sink(text):
        woken.append(text)
        return True

    monkeypatch.setattr(stall, "sink", sink)
    monkeypatch.setattr(stall, "waiting", lambda: False)
    client.roster, client.woken = roster, woken
    return client


async def look(bus, after_min: float = 0):
    return await stall.check(bus, ME, NOW + after_min * 60)


# --- what is a stall --------------------------------------------------------------------

def _open(bus, what: str) -> None:
    if what == "task":
        bus.posts += [post(1, ME, "TASK · Fix the parser · @agent-bob", 60),
                      post(2, BOB, "ACK · on it", 50, root=1)]
    elif what == "ask":
        bus.posts += [post(1, BOB, "🙋 @dembaca — **Merge PR #4?**", 50, kind=a2a.ASK)]
    elif what == "post":
        bus.posts += [post(1, HUMAN, "@agent-bob can you look at the build?", 50)]
    else:
        bus.posts += [post(1, BOB, "HELLO · here", 50)]


@pytest.mark.parametrize("what", ["task", "ask", "post", "nothing"])
@pytest.mark.parametrize("quiet_min", [5, 15])
@pytest.mark.parametrize("working", [False, True])
async def test_stall_needs_something_open_quiet_and_nobody_working(bus, what, quiet_min, working):
    """RT-6.19: open item × 10 min quiet × no agent working, and only then."""
    _open(bus, what)
    bus.posts.append(post(3, EVE, "PROGRESS · still reading", quiet_min))
    if working:
        bus.roster["agent-eve"] = "working"
    await look(bus)
    assert bool(bus.woken) == (what != "nothing" and quiet_min >= 10 and not working)


async def test_nothing_open_looks_again_later_without_waking(bus):
    """RT-6.19: a quiet bus with nothing open is no stall."""
    _open(bus, "nothing")
    assert await look(bus) is None
    assert bus.woken == []


async def test_only_the_crown_holder_looks(bus, monkeypatch):
    """RT-6.19: the check runs in the coordinator's server only; reminders run for every agent."""
    calls = []

    async def fake_check(client, uid, now=None):
        calls.append("check")
        return 5.0

    async def fake_tick(client, uid, now=None):
        calls.append("remind")
        return 50.0

    monkeypatch.setattr(stall, "check", fake_check)
    monkeypatch.setattr(remind, "tick", fake_tick)
    monkeypatch.setattr(lease, "crown_held", lambda uid: False)
    assert await stall.watch(bus) == 50.0 and calls == ["remind"]
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    calls.clear()
    assert await stall.watch(bus) == 5.0 and calls == ["remind", "check"]


async def test_a_waiting_assignee_waits_on_purpose(bus):
    """RT-6.19: WAITING from the assignee is no stall; BLOCKED is."""
    bus.posts += [post(1, ME, "TASK · Deploy · @agent-bob", 60),
                  post(2, BOB, "ACK · on it", 55, root=1),
                  post(3, BOB, "WAITING · CI runs for an hour", 50, root=1)]
    await look(bus)
    assert bus.woken == []
    bus.posts.append(post(4, BOB, "BLOCKED · which branch?", 40, root=1))
    await look(bus, 1)
    assert bus.woken and "task blocked" in bus.woken[0] and "next: agent-coord" in bus.woken[0]


async def test_addressed_posts_count_until_the_addressee_answers(bus):
    """RT-6.19: a post @-mentioning a reachable agent, without its answer in the
    thread; not an agent's fyi post, not an unreachable agent, not a day old."""
    bus.posts += [post(1, HUMAN, "@agent-bob can you look at the build?", 50),
                  post(2, BOB, "yes, looking", 45, root=1)]
    await look(bus)
    assert bus.woken == []
    bus.posts = [post(1, EVE, "RESULT · done, fyi @agent-bob", 50, kind="")]
    await look(bus, 1)
    assert bus.woken == []
    bus.posts = [post(1, HUMAN, "@agent-gone are you there?", 50)]
    await look(bus, 2)
    assert bus.woken == []
    bus.posts = [post(1, HUMAN, "@agent-bob are you there?", 25 * 60)]
    await look(bus, 3)
    assert bus.woken == []
    bus.posts = [post(1, EVE, "@agent-bob which port?", 50)]
    await look(bus, 4)
    assert len(bus.woken) == 1 and "next: agent-bob" in bus.woken[0] and "post " + pid(1) in bus.woken[0]


async def test_reminders_are_no_activity(bus):
    """RT-6.19: the server's own ask reminders do not end the quiet."""
    _open(bus, "ask")
    bus.posts.append(post(5, BOB, "🔔 @dembaca — reminder: **Merge PR #4?**", 2, root=1, kind=a2a.REMINDER))
    await look(bus)
    assert len(bus.woken) == 1


async def test_wakes_back_off_and_cap_and_activity_starts_over(bus, monkeypatch):
    """RT-6.19: 10 min of quiet, then 20, 40 … min after each wake, capped at
    MM_REMIND_MAX_SEC; a post by an agent resets the sequence."""
    monkeypatch.setenv("MM_REMIND_MAX_SEC", "3000")
    _open(bus, "task")                       # last activity 50 min ago
    times = []
    for minute in range(0, 400):
        before = len(bus.woken)
        await look(bus, minute)
        if len(bus.woken) > before:
            times.append(minute)
        if len(times) == 5:
            break
    assert times == [0, 20, 60, 110, 160]    # gaps 20, 40, then the 50 min cap
    bus.posts.append(post(9, BOB, "PROGRESS · back", -165))   # activity at minute 165
    for minute in range(161, 200):
        before = len(bus.woken)
        await look(bus, minute)
        if len(bus.woken) > before:
            times.append(minute)
            break
    assert times[-1] == 175                  # 10 min after the new activity
    assert stall._read(ME)["WAKES"] == 1


async def test_a_restart_does_not_wake_early(bus):
    """RT-6.19: the backoff lives in stall-<uid>.env."""
    _open(bus, "task")
    await look(bus)
    assert len(bus.woken) == 1
    assert await look(bus, 5) == pytest.approx(15 * 60)
    assert len(bus.woken) == 1


async def test_one_undelivered_stall_at_a_time(bus, monkeypatch):
    """RT-6.19: while a stall event waits uncollected, no second one is queued."""
    _open(bus, "task")
    monkeypatch.setattr(stall, "waiting", lambda: True)
    await look(bus)
    assert bus.woken == [] and stall._read(ME)["WAKES"] == 1


async def test_without_an_inbox_it_tries_again(bus, monkeypatch):
    _open(bus, "task")
    monkeypatch.setattr(stall, "sink", lambda text: False)
    assert await look(bus) is None
    assert stall._read(ME)["WAKES"] == 0


def test_digest_says_what_who_is_next_and_last_activity_and_is_capped():
    """RT-6.19: one line per item, at most MAX_ITEMS lines of LINE characters."""
    found = [{"what": "task active", "id": pid(i), "title": "T" * 150, "next": "agent-bob",
              "last": "agent-bob ACK 10:00", "at": i} for i in range(12)]
    text = stall.digest(found, int((NOW - 1500) * 1000), NOW)
    lines = text.splitlines()
    assert lines[0].startswith("Stall: no post by an agent or trusted human for 25 min")
    assert all(len(line) <= stall.LINE for line in lines[1:-1])
    assert lines[1].endswith(f' "{"T" * 59}…" · next: agent-bob · last: agent-bob ACK 10:00')
    assert lines[-2] == '- … and 4 more (coordinator("tasks"))'
    assert "nudge exactly one addressee" in lines[-1]
    assert len(text) <= stall.MAX_TEXT


async def test_an_expired_ask_asks_for_a_new_one(bus):
    """RT-6.19: an ask older than 24 h approves nothing (RT-6.11): its asker opens a new one."""
    bus.posts += [post(1, BOB, "🙋 @dembaca — **Merge PR #4?**", 25 * 60, kind=a2a.ASK)]
    await look(bus)
    assert "ask expired" in bus.woken[0] and "agent-bob: resolve it, open a new ask" in bus.woken[0]


# --- how the stall reaches the coordinator ----------------------------------------------

async def test_the_event_reaches_wait_for_events_push_and_hooks(monkeypatch):
    """RT-6.19: queued in the inbox, it comes out of wait_for_events as
    {kind: stall}, wakes the channel push, and counts in /v1/pending."""
    inbox = WebSocketInbox(client=Client())
    inbox._ws = object()                     # connected
    monkeypatch.setattr(server, "_inbox", inbox)
    monkeypatch.setattr(server, "_get_inbox", lambda: inbox)
    monkeypatch.setattr(server, "_client", Client())
    assert server._stall_event("Stall: 1 open") is True
    assert server._stall_waits() is True

    async def no_unread(client):
        return []

    monkeypatch.setattr(hookapi.unread, "channels", no_unread)
    monkeypatch.setattr(stall, "waiting", server._stall_waits)
    code, body = await hookapi.pending(Client(), {}, {})
    assert (code, body) == (200, {"count": 1, "stall": 1})
    events = await inbox.wait_for_events(timeout_sec=0.1)
    items = await server._event_items(events)
    assert items == [{"kind": "stall", "from_trusted_human": False, "from_agent": False,
                      "post": {"message": "Stall: 1 open"}}]
    assert channel.wakes(items[0], coordinator=False, in_my_thread=False)
    assert channel.notification(items[0])["meta"]["kind"] == "stall"
    assert server._stall_waits() is False


async def test_the_heartbeat_brings_its_next_tick_forward(monkeypatch):
    """RT-6.19: the watch runs after each tick and may shorten the wait."""
    sleeps = []

    async def fake_command(client, action):
        return ""

    async def watch(client):
        return 1.0

    real_sleep = asyncio.sleep

    async def fake_sleep(sec):
        sleeps.append(sec)
        await real_sleep(0)

    monkeypatch.setattr(lease, "command", fake_command)
    monkeypatch.setenv("MM_BOT_USER_ID", ME)
    monkeypatch.setattr(lease.asyncio, "sleep", fake_sleep)

    async def get_client():
        return Client()

    task = asyncio.create_task(lease.heartbeat(get_client, watch))
    while len(sleeps) < 2:
        await real_sleep(0)
    task.cancel()
    assert sleeps[0] == lease.PENDING_POLL_SEC


# --- ask reminders --------------------------------------------------------------------------

def test_reminder_schedule_20_then_10_20_40_capped(monkeypatch):
    """RT-6.19: first after MM_ASK_REMIND_SEC, then half that, doubling, capped."""
    gaps, last = [], 0.0
    for n in range(7):
        nxt = remind.due(0.0, n, last)
        gaps.append(nxt - last if n else nxt)
        last = nxt
    assert gaps[:5] == [1200, 600, 1200, 2400, 4800]
    monkeypatch.setenv("MM_REMIND_MAX_SEC", "3000")
    assert remind.due(0.0, 4, 100.0) == 3100.0


def _ask(asked_ago_min: float, root: int = 0) -> str:
    ask_id = pid(1)
    asks._write(ME, {ask_id: NOW - asked_ago_min * 60}, set(),
                {ask_id: f"dembaca {pid(root) if root else ''}".rstrip()})
    return ask_id


async def test_reminders_follow_the_schedule_in_the_asks_thread(bus):
    """RT-6.19: 20 min after the ask, then +10, +20 …; a reply in the ask's
    thread with the human's @-mention and the ask's title, fyi for agents."""
    _ask(0, root=7)
    bus.posts += [post(7, HUMAN, "deploy plan", 1),
                  post(1, ME, "🙋 @dembaca — **Go to install on hogan?**", 0, root=7, kind=a2a.ASK)]
    at = []
    for minute in range(0, 75):
        before = len(bus.sent)
        await remind.tick(bus, ME, NOW + minute * 60)
        if len(bus.sent) > before:
            at.append(minute)
    assert at == [20, 30, 50]
    sent = bus.sent[0]
    assert sent["root_id"] == pid(7)
    assert sent["message"].startswith("🔔 @dembaca — reminder: **Go to install on hogan?**")
    assert "waiting 20 min" in sent["message"]
    assert sent["props"]["a2a"]["kind"] == a2a.REMINDER and sent["props"]["a2a"]["intent"] == a2a.FYI
    assert stats.summary("0", 0)["ask_reminders"] == 3


async def test_a_top_level_ask_is_reminded_in_its_own_thread(bus):
    ask_id = _ask(25)
    bus.posts.append(post(1, ME, "🙋 @dembaca — **Merge?**", 25, kind=a2a.ASK))
    await remind.tick(bus, ME, NOW)
    assert [s["root_id"] for s in bus.sent] == [ask_id]


@pytest.mark.parametrize("answer", ["reaction", "reply", "collected", "resolved"])
async def test_reminders_stop_once_answered_or_resolved(bus, answer):
    """RT-6.19: a yes/no reaction by someone else, a reply by the human, an
    answer the agent collected or resolve_ask end the reminders."""
    ask_id = _ask(25)
    bus.posts.append(post(1, ME, "🙋 @dembaca — **Merge?**", 25, kind=a2a.ASK))
    if answer == "reaction":
        bus.reacts[ask_id] = [{"user_id": ME, "emoji_name": "+1"}, {"user_id": HUMAN, "emoji_name": "-1"}]
    elif answer == "reply":
        bus.posts.append(post(2, HUMAN, "not yet", 5, root=1))
    elif answer == "collected":
        asks.answered(ME, ask_id)
    else:
        asks.resolve(ME, ask_id)
    assert await remind.tick(bus, ME, NOW) is None
    assert bus.sent == []
    await remind.tick(bus, ME, NOW + 3600)
    assert bus.sent == []


async def test_no_reminder_once_the_ask_is_a_day_old(bus):
    """RT-6.19: a yes approves nothing after 24 h (RT-6.11): no more reminders."""
    _ask(23 * 60)
    bus.posts.append(post(1, ME, "🙋 @dembaca — **Merge?**", 23 * 60, kind=a2a.ASK))
    remind._write(ME, {pid(1): (8, NOW - 60)}, set())  # the next gap ends after 24 h
    assert await remind.tick(bus, ME, NOW) is None
    assert bus.sent == []


async def test_a_restart_sends_no_reminder_twice(bus):
    """RT-6.19: remind-<uid>.env survives the server; the next one keeps its time."""
    _ask(20)
    bus.posts.append(post(1, ME, "🙋 @dembaca — **Merge?**", 20, kind=a2a.ASK))
    assert await remind.tick(bus, ME, NOW) == pytest.approx(600)
    assert len(bus.sent) == 1
    assert (remind.session_file.state_dir() / f"remind-{ME}.env").read_text().startswith(f"R={pid(1)} 1 ")
    assert await remind.tick(bus, ME, NOW + 60) == pytest.approx(540)
    assert len(bus.sent) == 1


async def test_a_permission_prompt_is_not_reminded(bus):
    asks.add(ME, pid(1))
    assert asks.waiting(ME) == []
    assert await remind.tick(bus, ME, NOW + 7200) is None


def test_the_reminder_kind_is_fyi_and_kept_in_props():
    note = a2a.annotate(kind=a2a.REMINDER)
    assert note == {"v": 1, "kind": "reminder", "intent": "fyi"}
    assert a2a.kind_of({"props": {"a2a": note}}) == a2a.REMINDER
    assert json.dumps(note)

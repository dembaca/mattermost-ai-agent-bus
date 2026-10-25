"""RT-6.20 a turn plugin (OpenCode) asks the hook API what the Stop hook,
the listener and the compaction hook decide (docs/arc42/06-runtime-view.md)."""

import hashlib
import os
import time

import httpx
import pytest

from mattermost_ai_agent_bus import hookapi, lease, ownpost, session_file, stall, turnend, unread

UID = "u" * 26
OTHER = "o" * 26
ASK = "k" * 26
PERSON = "q" * 26


class FakeClient:
    """What the routes use of MattermostClient: posts newest first."""

    def __init__(self, posts=(), agents=(), reactions=None):
        self.token = "t"
        self.project_channel_name = ""
        self.work_channel_name = "agents"
        self.channel_name = ""
        self.posts = list(posts)
        self._agents = set(agents)
        self._reactions = reactions or {}
        self.calls = []

    async def get_me(self):
        return {"id": UID, "username": "agent-me"}

    async def work_channel_id(self):
        return "c" * 26

    async def channel_posts(self, cid, *, per_page=None, since_ms=None):
        self.calls.append(("posts", per_page, since_ms))
        if since_ms is not None:
            return [p for p in self.posts if p["create_at"] >= since_ms]
        return list(self.posts)[:per_page]

    async def agents(self, ids):
        return set(ids) & self._agents

    async def reactions(self, pid):
        return self._reactions.get(pid, [])


def post(pid, message, user=OTHER, at=1000, root="", props=None, type_=""):
    return {"id": pid, "user_id": user, "message": message, "create_at": at,
            "root_id": root, "type": type_, "props": props or {}}


@pytest.fixture
async def api():
    made = []

    async def serve(client):
        async def get():
            return client

        a = hookapi.HookAPI(get)
        await a.start(write_file=False)
        h = httpx.AsyncClient(base_url=a.endpoint, headers={"Authorization": f"Bearer {a.key}"})
        made.append((a, h))
        return h

    yield serve
    for a, h in made:
        await h.aclose()
        await a.close()


@pytest.fixture
def unread_rows(monkeypatch):
    rows = []

    async def channels(client):
        return list(rows)

    async def detail(client, got):
        return [{"id": cid, "type": "O", "pending": n, "fyi": 1 if cid == "chan" else 0}
                for cid, n in got]

    monkeypatch.setattr(unread, "channels", channels)
    monkeypatch.setattr(unread, "detail", detail)
    return rows


async def test_turn_end_drains_signs_off_then_goes_idle(api, unread_rows):
    """RT-6.20 (RT-6.2 steps 1–2): the Stop hook's words, with the caller's
    turn state: drain while counters say so, at most MAX_DRAIN rounds, then
    one sign-off, then idle."""
    client = FakeClient(posts=[post("b", "WORKING · on #94", user=UID, at=2000)])
    h = await api(client)
    unread_rows[:] = [("dm", 2), ("chan", 1)]
    got = (await h.get("/v1/turn-end?push=1")).json()
    assert got["action"] == "drain" and got["pending"] == 3
    assert got["reason"].startswith("You have 3 unread message(s) on the Mattermost agent bus, "
                                    "2 needing action, the rest fyi. Call wait_for_events")
    assert "Pending per channel:\ndm\tO\tpending=2\nchan\tO\tpending=1\tfyi=1" in got["reason"]
    assert got["banner"] == "📨 Collecting 3 unread Mattermost message(s)"
    # Stale counters: after MAX_DRAIN rounds the drain lets go.
    got = (await h.get(f"/v1/turn-end?push=1&drains={turnend.MAX_DRAIN}")).json()
    assert got["action"] == "signoff"
    assert got["reason"].startswith("Before this turn ends: bus messages still wake you")
    assert "IDLE · done: … · open: … · next: …" in got["reason"]
    assert (await h.get("/v1/turn-end?push=0&drains=3")).json()["reason"].startswith(
        "Before this turn ends: once you are idle")
    unread_rows.clear()
    assert (await h.get("/v1/turn-end?push=1&signoff_asked=1")).json() == {"action": "idle"}
    # A turn the bus started needs no sign-off where the plugin wakes the agent.
    assert (await h.get("/v1/turn-end?push=1&by_bus=1")).json() == {"action": "idle"}
    assert (await h.get("/v1/turn-end?push=0&by_bus=1")).json()["action"] == "signoff"
    client.posts = [post("b", "IDLE · done: x", user=UID)]
    assert (await h.get("/v1/turn-end?push=1")).json() == {"action": "idle"}
    client.posts = [post("a", "hi")]  # never posted there: not part of the coordination
    assert (await h.get("/v1/turn-end?push=1")).json() == {"action": "idle"}
    for bad in ("drains=x", "push=yes", "by_bus=2"):
        assert (await h.get(f"/v1/turn-end?{bad}")).status_code == 400


async def test_wake_says_what_the_listener_would(api, unread_rows, monkeypatch):
    """RT-6.20 (RT-6.2 step 4): unread counters, reactions to open asks, then
    the work channel's new posts — named, in an own thread, not an agent's fyi
    elsewhere, never gated — and a digest once the oldest other post is old."""
    now = int(time.time() * 1000)
    fyi = {"a2a": {"v": 1, "kind": "done", "intent": "fyi"}}
    mine = post("h", "HELLO · agent-me", user=UID, at=now - 60_000)
    client = FakeClient(agents={OTHER}, posts=[mine])
    client.project_channel_name = "proj"
    h = await api(client)
    since, until = now - 30_000, int(time.time()) + 600
    assert (await h.get("/v1/wake")).status_code == 400
    assert (await h.get(f"/v1/wake?since={since}&until={until}")).json() == {
        "wake": False, "reason": "", "count": 0, "text": ""}
    unread_rows[:] = [("dm", 1)]
    got = (await h.get(f"/v1/wake?since={since}&until={until}")).json()
    assert got["reason"] == "dm" and got["text"].startswith(
        "Mattermost: 1 unread mention(s) or DM(s) for you. Collect them with wait_for_events")
    unread_rows.clear()
    d = session_file.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"asks-{UID}.env").write_text(f"ASK={ASK} {int(time.time())}\n")
    client._reactions = {ASK: [{"user_id": PERSON, "emoji_name": "+1"}]}
    assert (await h.get(f"/v1/wake?since={since}&until={until}")).json()["reason"] == "reaction"
    client._reactions = {}
    # An agent's fyi reply in my thread wakes me: I started it.
    client.posts = [post("r1", "DONE · merged", root="h", at=now - 1000, props=fyi), mine]
    assert (await h.get(f"/v1/wake?since={since}&until={until}")).json()["reason"] == "addressed"
    # A plain post nobody names: not at once, a digest once it is old.
    client.posts = [post("p", "anyone?", at=now - 1000), mine]
    assert (await h.get(f"/v1/wake?since={since}&until={until}")).json()["wake"] is False
    monkeypatch.setenv("MM_LISTEN_DIGEST_SEC", "0")
    got = (await h.get(f"/v1/wake?since={since}&until={until}")).json()
    assert got["reason"] == "digest" and "1 post(s) in the work channel since" in got["text"]
    monkeypatch.setenv("MM_LISTEN_DIGEST_SEC", "1800")
    # Named, or for all agents: at once; gated: never at once.
    client.posts = [post("n", "@agent-me. please look", at=now - 1000), mine]
    assert (await h.get(f"/v1/wake?since={since}&until={until}")).json()["reason"] == "addressed"
    (d / f"gate-{UID}.env").write_text(f"GATED=n {int(time.time())}\n")
    assert (await h.get(f"/v1/wake?since={since}&until={until}")).json()["wake"] is False
    # The coordinator sees every post (RT-6.6).
    client.posts = [post("p", "what is next?", at=now - 1000), mine]
    monkeypatch.setattr(lease, "crown_until", lambda uid: int(time.time()) + 600)
    assert (await h.get(f"/v1/wake?since={since}&until={until}")).json()["reason"] == "addressed"


async def test_a_stall_event_drains_and_wakes_like_a_message(api, unread_rows, monkeypatch):
    """RT-6.20 with RT-6.19: a stall event waiting in the inbox counts as
    /v1/pending counts it — the turn end drains it, an idle session wakes for it."""
    client = FakeClient(posts=[post("b", "IDLE · done: x", user=UID)])
    client.project_channel_name = "proj"
    h = await api(client)
    monkeypatch.setattr(stall, "waiting", lambda: True)
    got = (await h.get("/v1/turn-end?push=1")).json()
    assert got["action"] == "drain" and got["pending"] == 1 and "Pending per channel" not in got["reason"]
    assert (await h.get(f"/v1/turn-end?push=1&drains={turnend.MAX_DRAIN}")).json()["action"] == "idle"
    since, until = int(time.time() * 1000), int(time.time()) + 600
    got = (await h.get(f"/v1/wake?since={since}&until={until}")).json()
    assert got["wake"] is True and got["reason"] == "stall" and "wait_for_events" in got["text"]
    unread_rows[:] = [("dm", 2)]
    got = (await h.get(f"/v1/wake?since={since}&until={until}")).json()
    assert got["reason"] == "dm" and got["count"] == 3
    monkeypatch.setattr(stall, "waiting", lambda: False)
    unread_rows.clear()
    assert (await h.get(f"/v1/wake?since={since}&until={until}")).json()["wake"] is False


async def test_wake_only_in_the_listen_window_and_its_gates(api, unread_rows, monkeypatch):
    """RT-6.20 (RT-6.2 step 3): the turn end opens the Stop hook's listen
    window — a project channel, an own post in the work channel, not BYE,
    MM_LISTEN_SEC (the 👑 MM_LISTEN_COORD_SEC) — and /v1/wake wakes only inside it."""
    now = int(time.time())
    client = FakeClient(posts=[post("b", "IDLE · done: x", user=UID, at=(now - 100) * 1000)])
    h = await api(client)
    unread_rows[:] = [("dm", 1)]
    q = f"since={now * 1000}&until={now + 600}"
    # Not a project channel: no window, no wake, even for a DM.
    assert (await h.get(f"/v1/turn-end?push=1&drains={turnend.MAX_DRAIN}")).json() == {"action": "idle"}
    assert (await h.get(f"/v1/wake?{q}")).json()["wake"] is False
    client.project_channel_name = "proj"
    got = (await h.get(f"/v1/turn-end?push=1&drains={turnend.MAX_DRAIN}")).json()
    assert got["listen_since"] >= now and got["listen_until"] - got["listen_since"] == turnend.LISTEN_SEC
    assert (await h.get(f"/v1/wake?{q}")).json()["reason"] == "dm"
    # The window is over: nothing wakes.
    assert (await h.get(f"/v1/wake?since={now * 1000}&until={now - 1}")).json()["wake"] is False
    # A running window goes on unless the agent posted since it started.
    start = now - 50
    got = (await h.get(f"/v1/turn-end?push=1&drains=3&listen_since={start}")).json()
    assert (got["listen_since"], got["listen_until"]) == (start, start + turnend.LISTEN_SEC)
    got = (await h.get(f"/v1/turn-end?push=1&drains=3&listen_since={now - 200}")).json()
    assert got["listen_since"] >= now
    got = (await h.get(f"/v1/turn-end?push=1&drains=3&listen_since={now - turnend.LISTEN_SEC - 1}")).json()
    assert got["listen_since"] >= now  # it posted after that one started: a new one
    monkeypatch.setattr(lease, "crown_until", lambda uid: now + 600)
    got = (await h.get("/v1/turn-end?push=1&drains=3")).json()
    assert got["listen_until"] - got["listen_since"] == turnend.LISTEN_COORD_SEC
    monkeypatch.setattr(lease, "crown_until", lambda uid: 0)
    monkeypatch.setenv("MM_LISTEN_SEC", "0")
    assert (await h.get("/v1/turn-end?push=1&drains=3")).json() == {"action": "idle"}
    monkeypatch.setenv("MM_LISTEN_SEC", "99999")
    got = (await h.get("/v1/turn-end?push=1&drains=3")).json()
    assert got["listen_until"] - got["listen_since"] == turnend.MAX_LISTEN_SEC
    # BYE ends the session's part: no window, no wake; never posted: neither.
    client.posts = [post("b", "BYE · done", user=UID)]
    assert (await h.get("/v1/turn-end?push=1&drains=3")).json() == {"action": "idle"}
    assert (await h.get(f"/v1/wake?{q}")).json()["wake"] is False
    client.posts = [post("a", "hi")]
    assert (await h.get(f"/v1/wake?{q}")).json()["wake"] is False
    for bad in ("since=1", "since=1&until=x", "until=1"):
        assert (await h.get(f"/v1/wake?{bad}")).status_code == 400
    assert (await h.get("/v1/turn-end?listen_since=x")).status_code == 400


async def test_wake_reads_the_own_threads_once_until_the_agent_posts(api, unread_rows):
    """RT-6.20: an idle session asks every few seconds; its own threads are
    read again only once it posted (ownpost.py) or after OWN_TTL_SEC."""
    now = int(time.time())
    client = FakeClient(posts=[post("h", "HELLO · agent-me", user=UID, at=(now - 100) * 1000)])
    client.project_channel_name = "proj"
    h = await api(client)
    q = f"since={now * 1000}&until={now + 600}"
    for _ in range(3):
        assert (await h.get(f"/v1/wake?{q}")).json()["wake"] is False
    assert client.calls.count(("posts", hookapi.MAX_POSTS, None)) == 1
    ownpost.record(UID, "c" * 26, post("n", "IDLE · done: y", user=UID, at=now * 1000))
    assert (await h.get(f"/v1/wake?{q}")).json()["wake"] is False
    assert client.calls.count(("posts", hookapi.MAX_POSTS, None)) == 2


def test_empty_session_variables_count_as_unset(monkeypatch, tmp_path):
    """RT-6.20: the OpenCode plugin blanks CLAUDE_CODE_SESSION_ID and
    CLAUDE_PROJECT_DIR a Claude Code shell passed on, and names MM_WORKSPACE:
    the server keys its endpoint file on that workspace, where the plugin looks."""
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", " ")
    monkeypatch.setenv("MM_WORKSPACE", str(ws))
    key = hashlib.sha1(os.path.realpath(ws).encode()).hexdigest()[:16]
    assert session_file.session_id() == ""
    assert session_file.workspace() == (os.path.realpath(ws), "MM_WORKSPACE")
    assert session_file.keyed_path("endpoint") == session_file.state_dir() / f"endpoint-{key}.env"


def test_wake_rules_match_the_listener():
    """RT-6.20: the listener's jq rules — @channel/@all/@here and to=all name
    everyone; an agent's fyi post in someone else's thread waits for the digest."""
    fyi = {"fyi": True}
    recent = [{"id": "x", "user_id": UID, "root_id": "t"}]  # I replied in thread t
    base = {"user_id": OTHER, "type": "", "create_at": 5, "message": "", "to": "", "gated": False}
    for p, woken in [({"message": "@here build is red"}, True), ({"to": "all"}, True),
                     ({"message": "@agent-meow hi"}, False),
                     ({"root_id": "t", **fyi}, False), ({"root_id": "t"}, True)]:
        got = turnend.wake([{**base, **p}], recent, me=UID, username="agent-me", since_ms=0,
                           coordinator=False, digest_sec=3600, now=10)
        assert got["wake"] is woken, p


async def test_handoff_hands_back_the_own_posts(api):
    """RT-6.20 (RT-6.7): the compaction hook's context, from the server."""
    client = FakeClient(posts=[post("a", "IDLE · done: x\nnext: y", user=UID, at=3000, root="h"),
                               post("o", "other", at=2500),
                               post("j", "joined", user=UID, type_="system_join_channel"),
                               post("h", "HELLO · agent-me", user=UID, at=2000)])
    h = await api(client)
    text = (await h.get("/v1/handoff")).json()["context"]
    assert text.startswith("Your conversation was just compacted.")
    assert "] IDLE · done: x next: y\n- [" in text and "other" not in text and "joined" not in text
    assert "Your HELLO thread: root h; read it with get_thread for more." in text
    client.posts = [post("o", "other")]
    assert (await h.get("/v1/handoff")).json() == {"context": ""}


async def test_a_block_counts_its_kind(api):
    """RT-6.20 (RT-6.9): the plugin and the Stop hook count a block with its
    kind= (drain, signoff); the field must not collide with stats.record's own
    argument, which made the route answer 502."""
    from mattermost_ai_agent_bus import stats

    h = await api(FakeClient())
    assert (await h.post("/v1/stat", json={"k": "block", "kind": "drain"})).json() == {"ok": True}
    rows = []
    for path in session_file.state_dir().glob("stats-*.jsonl"):
        rows += [line for line in path.read_text().splitlines() if '"block"' in line]
    assert rows and '"kind": "drain"' in rows[-1]
    stats.record("block", kind="signoff")  # no TypeError

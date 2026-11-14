"""RT-6.2 step 2: a bus post that reached the agent and needs an answer waits
for one in its thread; the turn end blocks once for it (unanswered.py)."""

from __future__ import annotations

import time

import httpx
import pytest

from mattermost_ai_agent_bus import a2a, hookapi, server, turnend, unanswered
from mattermost_ai_agent_bus.client import MattermostAdapter, post_of
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent

from fakes import HookClient

UID, WORK, DM, OTHER = "u" * 26, "w" * 26, "d" * 26, "o" * 26
ROOT, MINE = "r" * 26, "m" * 26


def ev(kind, pid="x", root="", gated="", channel=WORK, props=None, type_=""):
    return ClassifiedEvent(kind=kind, gated=gated, post=post_of({
        "id": pid.ljust(26, "0"), "user_id": OTHER, "root_id": root, "channel_id": channel,
        "message": "can you check?", "type": type_, "props": props or {}}))


def item(kind, **extra):
    return {"kind": kind, "post": {"channel": "home", "author": "dembaca"}, **extra}


@pytest.fixture
def delivered(monkeypatch):
    """server._note_unanswered with the bot's id and its threads (MINE) known."""
    async def my_id(_client):
        return UID

    async def in_my_thread(it, uid):
        return (it.get("post") or {}).get("root_id") == MINE

    monkeypatch.setattr(server, "_get_client", lambda: None)
    monkeypatch.setattr(server, "_my_id", my_id)
    monkeypatch.setattr(server, "_in_my_thread", in_my_thread)

    async def deliver(pairs):
        await server._note_unanswered([e for e, _ in pairs], [i for _, i in pairs])
        return [r.post_id[:1] for r in unanswered.read(UID)]
    return deliver


@pytest.mark.asyncio
async def test_what_needs_an_answer(delivered):
    """RT-6.2 step 2: a mention, a DM, a post for the crown holder and a reply
    in the agent's thread wait for an answer, from a human or an agent; an fyi
    post (an agent's ACK, RESULT …), a gated post, a reaction, an expired
    answer, the 👑's advice, a system post, a reminder and the server's own
    event do not."""
    mine = item("watched_channel")
    mine["post"]["root_id"] = MINE
    elsewhere = item("watched_channel")
    elsewhere["post"]["root_id"] = ROOT
    assert await delivered([
        (ev("mention", "a"), item("mention")),
        (ev("dm", "b", channel=DM), item("dm")),
        (ev("watched_channel", "c"), item("watched_channel", for_crown=True)),
        (ev("watched_channel", "d", root=MINE), mine),
        (ev("watched_channel", "e", root=ROOT), elsewhere),
        (ev("mention", "f"), item("mention", from_agent=True, intent="fyi")),
        (ev("mention", "g", gated="not trusted"), item("mention", gated="not trusted")),
        (ev("reaction", "h"), item("reaction")),
        (ev("mention", "i"), item("mention", expired=True)),
        (ev("mention", "j"), item("mention", advice=ROOT)),
        (ev("mention", "k", type_="system_join_channel"), item("mention")),
        (ev("mention", "l", props=a2a.props(kind=a2a.REMINDER)), item("mention")),
        (ev("stall", "n"), item("stall")),
    ]) == ["a", "b", "c", "d"]
    rows = unanswered.read(UID)
    assert (rows[1].channel, rows[1].dm) == ("DM", True)
    assert (rows[3].root_id, rows[3].channel, rows[3].author) == (MINE, "home", "dembaca")
    assert rows[0].root_id == rows[0].post_id  # a root post: answered under itself


@pytest.mark.asyncio
async def test_an_agents_ack_never_owes_an_answer_back(delivered):
    """RT-6.2 step 2: agent A asks B; B's ACK or RESULT in A's thread is fyi,
    so A owes no answer and the two never block each other in a loop. A plain
    reply or a BLOCKED (a question) does wait for one."""
    for word in ("ACK", "RESULT", "DONE"):
        it = item("watched_channel", from_agent=True, intent=a2a.WORDS[word][2])
        it["post"]["root_id"] = MINE
        assert await delivered([(ev("watched_channel", "a", root=MINE), it)]) == []
    it = item("watched_channel", from_agent=True, intent="reply")
    it["post"]["root_id"] = MINE
    assert await delivered([(ev("watched_channel", "b", root=MINE), it)]) == ["b"]


def test_capped_aged_and_kept_across_a_restart(monkeypatch):
    """RT-6.2 step 2: at most MAX, the newest kept; none older than
    MAX_AGE_SEC; the file in the state directory is read again, so a
    restarted server of the same bot keeps them."""
    now = int(time.time())
    for n in range(unanswered.MAX + 3):
        unanswered.add(UID, post_of({"id": f"p{n:02d}".ljust(26, "0"), "channel_id": WORK}),
                       channel="home", author="a", dm=False, now=now - 100 + n)
    rows = unanswered.read(UID)
    assert len(rows) == unanswered.MAX and rows[0].post_id.startswith("p03")
    assert unanswered.path(UID).parent == unanswered.session_file.state_dir()
    # p03 … p12 came at now-97 … now-88: a day after now-92, p08 … p12 are left.
    aged = unanswered.read(UID, now=now - 92 + unanswered.MAX_AGE_SEC)
    assert [r.post_id[:3] for r in aged] == ["p08", "p09", "p10", "p11", "p12"]
    assert unanswered.read(UID, now=now + unanswered.MAX_AGE_SEC + 1) == []


def test_reminded_once():
    """RT-6.2 step 2: each open post is handed out once; it stays open, but
    blocks no further turn end."""
    unanswered.add(UID, post_of({"id": "a" * 26, "root_id": ROOT, "channel_id": WORK}),
                   channel="home", author="dembaca", dm=False)
    assert unanswered.remind(UID) == [{"post_id": "a" * 26, "root_id": ROOT, "channel": "home",
                                       "author": "dembaca"}]
    assert unanswered.remind(UID) == []
    assert [r.reminded for r in unanswered.read(UID)] == [True]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    c = MattermostAdapter(base_url="https://mm.example", token="tok", project_channel_name="proj")
    posts = iter(range(1, 99))

    async def api(method, path, json=None, **_):
        if path == f"/api/v4/teams/name/{c.team_name}":
            return {"id": "tid00000000000000000000000"}
        if path == "/api/v4/teams/tid00000000000000000000000/channels/name/proj":
            return {"id": WORK}
        if path.startswith("/api/v4/posts/"):  # a reply's root
            pid = path.rsplit("/", 1)[1]
            return {"id": pid, "channel_id": DM if pid.startswith("d") else WORK}
        assert path == "/api/v4/posts"
        return {"id": f"p{next(posts)}".ljust(26, "0"), "user_id": UID, "create_at": 1000, **json}

    monkeypatch.setattr(c, "_api", api)
    return c


@pytest.mark.asyncio
async def test_a_post_in_its_thread_answers_it(client):
    """RT-6.2 step 2: a reply in the thread (reply_in_thread, an ask_human's
    link line there) answers its open posts; in a DM any post there; a post
    elsewhere, a reminder the server sends on its own or a permission prompt
    does not."""
    unanswered.add(UID, post_of({"id": "a" * 26, "root_id": ROOT, "channel_id": WORK}),
                   channel="home", author="h", dm=False)
    unanswered.add(UID, post_of({"id": "d" * 26, "channel_id": DM}), channel="", author="h", dm=True)
    await client.post("ACK · elsewhere", channel_id=WORK)
    await client.post("reminder", root_id=ROOT, props=a2a.props(kind=a2a.REMINDER))
    await client.post("allow?", channel_id=DM, props=a2a.props(kind=a2a.PERMISSION))
    assert len(unanswered.read(UID)) == 2
    await client.post("\U0001F64B ask x", root_id=ROOT, props=a2a.props(kind=a2a.ASK_LINK))
    assert [r.post_id for r in unanswered.read(UID)] == ["d" * 26]
    await client.post("sure", channel_id=DM)
    assert unanswered.read(UID) == []
    assert not unanswered.path(UID).exists()


async def _api(client):
    async def get():
        return client
    a = hookapi.HookAPI(get)
    await a.start(write_file=False)
    return a, httpx.AsyncClient(base_url=a.endpoint, headers={"Authorization": f"Bearer {a.key}"})


@pytest.mark.asyncio
async def test_the_hook_api_hands_them_out_once():
    """RT-6.2 step 2, RT-6.17: GET /v1/last-post?unanswered=1 carries the open
    posts not yet reminded, each once; without the flag nothing is handed out
    (another reader never takes the Stop hook's reminder)."""
    a, h = await _api(HookClient(uid=UID))
    try:
        unanswered.add(UID, post_of({"id": "a" * 26, "root_id": ROOT, "channel_id": WORK}),
                       channel="home", author="dembaca", dm=False)
        assert "unanswered" not in (await h.get("/v1/last-post")).json()
        got = (await h.get("/v1/last-post?unanswered=1")).json()
        assert got["unanswered"] == [{"post_id": "a" * 26, "root_id": ROOT, "channel": "home",
                                      "author": "dembaca"}]
        assert "unanswered" not in (await h.get("/v1/last-post?unanswered=1")).json()
    finally:
        await h.aclose()
        await a.close()


@pytest.mark.asyncio
async def test_turn_end_answers_first_then_signs_off(monkeypatch):
    """RT-6.20, RT-6.2 step 2: for a turn plugin (unanswered=1) an open post
    blocks once as `answer`; with a sign-off due too, one `signoff` block
    whose reason asks for the answer first."""
    from mattermost_ai_agent_bus import ownpost

    a, h = await _api(HookClient(uid=UID, posts=[]))
    try:
        unanswered.add(UID, post_of({"id": "a" * 26, "root_id": ROOT, "channel_id": WORK}),
                       channel="home", author="dembaca", dm=False)
        r = (await h.get("/v1/turn-end?push=1&by_bus=1&unanswered=1")).json()
        assert r["action"] == "answer" and f"~home post {ROOT} by dembaca" in r["reason"]
        assert "reply_in_thread" in r["reason"] and "\U0001F4AC" in r["banner"]
        assert (await h.get("/v1/turn-end?push=1&by_bus=1&unanswered=1")).json()["action"] == "idle"
        ownpost.record(UID, WORK, post_of({"id": "i" * 26, "message": "PROGRESS · x", "create_at": 2000}))
        unanswered.add(UID, post_of({"id": "b" * 26, "channel_id": DM}), channel="", author="h", dm=True)
        r = (await h.get("/v1/turn-end?push=0&unanswered=1")).json()
        assert r["action"] == "signoff"
        assert r["reason"].index("DM post " + "b" * 26) < r["reason"].index("Then: Before this turn ends")
        assert r["banner"].count("\n") == 1
        # Without the flag (a plugin before 0.89.0) nothing is handed out.
        unanswered.add(UID, post_of({"id": "c" * 26, "channel_id": WORK}), channel="home", author="h", dm=False)
        assert (await h.get("/v1/turn-end?push=1&by_bus=1")).json()["action"] == "idle"
    finally:
        await h.aclose()
        await a.close()


def test_the_words_match_the_stop_hook():
    """RT-6.20: the agent reads the same words in every harness."""
    import re
    from pathlib import Path

    hook = (Path(server.__file__).resolve().parents[3] / "hooks" / "stop-inbox-check.sh").read_text()
    said = "".join(re.findall(r'^\s*answer_reason\+?="([^"]*)"$', hook, re.M)).replace("${UNANSWERED}", "~x post r by a")
    assert said == turnend.answer([{"channel": "x", "root_id": "r", "author": "a"}])["reason"]


@pytest.mark.asyncio
async def test_only_a_pushed_post_waits(delivered, monkeypatch):
    """RT-6.2 step 2, RT-6.12 step 3: a post the channel push held for the
    digest (or dropped) reached nobody yet and waits for no answer; one it
    pushed does."""
    from mattermost_ai_agent_bus import channel

    class Inbox:
        async def ack(self, events, typing=False):
            pass

        def start_typing(self, *a):
            pass

    async def nothing(*a):
        pass

    monkeypatch.setattr(server, "_get_inbox", lambda: Inbox())
    monkeypatch.setattr(server, "_note_delivered", nothing)
    for pid in ("a", "b"):
        e = ev("mention", pid)
        it = server._PushItem(item("mention"))
        it.event, it.batch = e, server._Batch([e])
        if pid == "b":
            it[channel.PUSHED] = True
        await server._push_delivered(it)
    assert [r.post_id[:1] for r in unanswered.read(UID)] == ["b"]

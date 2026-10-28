"""RT-6.2 step 1, RT-6.17: what is waiting for this agent, counted by the MCP
server from the bus's read watermark (unread.py, watermark.py;
docs/arc42/06-runtime-view.md)."""

import dataclasses
import json
import os
import stat
import time

import httpx
import pytest

from mattermost_ai_agent_bus import hookapi, session_file, unread, watermark
from mattermost_ai_agent_bus.websocket_inbox import classify_post
from mattermost_ai_agent_bus.client import MattermostAdapter

UID = "u" * 26
TEAM = "t" * 26
DM = "d" * 26
CHAN = "c" * 26
QUIET = "q" * 26
NEW = "n" * 26  # a channel the bot is in, but the server does not watch
GROUP = "g" * 26
WATCHED = {CHAN}  # the work channel
AGENT = "a" * 26
PERSON = "h" * 26
FYI = {"a2a": {"v": 1, "kind": "done", "intent": "fyi"}}
ALL = {"a2a": {"v": 1, "to": "all"}}
BORN = int(time.time() * 1000) - 3_600_000  # the bot's create_at


class FakeMattermost:
    def __init__(self):
        self.members: list[dict] = []
        self.channels: list[dict] = []
        self.posts: dict[str, list[dict]] = {}
        self.read: list[tuple[str, int]] = []
        self.born = BORN

    def channel(self, cid, ctype="O", total=0, seen=0, mentions=0, posts=(), last=None):
        """`last`: the channel's last_post_at, by default its newest post's."""
        self.members.append({"channel_id": cid, "msg_count": seen, "mention_count": mentions})
        newest = max((p["create_at"] for p in posts), default=0) if last is None else last
        self.channels.append({"id": cid, "type": ctype, "total_msg_count": total,
                              "last_post_at": newest})
        self.posts[cid] = list(posts)

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/v4/users/me":
            return httpx.Response(200, json={"id": UID, "username": "agent-me",
                                             "create_at": self.born})
        if p == "/api/v4/teams/name/agents":
            return httpx.Response(200, json={"id": TEAM})
        if p == f"/api/v4/teams/{TEAM}/channels/name/agents":
            return httpx.Response(200, json={"id": CHAN})
        if p == f"/api/v4/users/me/teams/{TEAM}/channels/members":
            return httpx.Response(200, json=self.members)
        if p == "/api/v4/users/me/channels":
            return httpx.Response(200, json=self.channels)
        if p.startswith("/api/v4/channels/") and p.endswith("/posts"):
            cid = p.split("/")[4]
            since = int(req.url.params["since"])
            self.read.append((cid, since))
            posts = [x for x in self.posts.get(cid, []) if x["create_at"] >= since]
            return httpx.Response(200, json={"order": [x["id"] for x in posts],
                                              "posts": {x["id"]: x for x in posts}})
        if p == "/api/v4/users/ids":
            ids = json.loads(req.content)
            known = {AGENT: ("agent-x", True), PERSON: ("dembaca", False)}
            return httpx.Response(200, json=[{"id": i, "username": known[i][0], "is_bot": known[i][1]}
                                             for i in ids if i in known])
        return httpx.Response(404, json={})


@pytest.fixture
def mm(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.delenv("MM_AGENT_PREFIX", raising=False)
    return FakeMattermost()


@pytest.fixture
def client(mm):
    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(mm.handler))
    return c


def now_ms(delta_s=0):
    return int((time.time() + delta_s) * 1000)


_seq = iter(range(10**6))


def post(pid, message="", user=AGENT, at=None, props=None, type_=""):
    return {"id": pid, "user_id": user, "message": message, "type": type_,
            "create_at": at if at is not None else now_ms(-60) + next(_seq), "props": props or {}}


async def pending(client):
    return [(row.channel.channel_id, row.waiting) for row in await unread.channels(client, WATCHED)]


async def test_a_dm_counts_others_posts_a_channel_those_for_me(mm, client):
    """RT-6.2 step 1: past the watermark, in a DM every post by someone else,
    elsewhere only the posts that name this agent (or everyone). The counters
    say nothing: a channel with a mention counter but no post past the mark
    is not even read."""
    mm.channel(DM, "D", total=5, seen=2, posts=[post("d1", "hi"), post("d2", "there")])
    mm.channel(CHAN, total=40, mentions=1, posts=[
        post("c1", "@agent-me look"), post("c2", "just talk"), post("c3", "@agent-mex no")])
    mm.channel(QUIET, total=9, mentions=3, posts=[post("q1", "@agent-me old", at=BORN - 5)])
    assert await pending(client) == [(DM, 2), (CHAN, 1)]
    assert [cid for cid, _ in mm.read] == [DM, CHAN]


async def test_only_what_came_after_the_watermark_waits(mm, client):
    """RT-6.2: handed-over posts are behind the mark; a channel whose newest
    post is not past it costs no request, whatever its counters say."""
    d1, d2 = post("d1", "first"), post("d2", "second")
    mm.channel(DM, "D", total=2, posts=[d1, d2])
    mm.channel(CHAN, total=1, seen=0, posts=[post("c1", "@agent-me")])
    watermark.advance(UID, DM, "d1", d1["create_at"])
    watermark.advance(UID, CHAN, "c1", mm.posts[CHAN][0]["create_at"])
    assert await pending(client) == [(DM, 1)]
    assert mm.read == [(DM, d1["create_at"])]


async def test_a_dm_with_only_the_bots_own_post_waits_for_nothing(mm, client):
    """RT-6.2, RT-6.13 (0.42.3): Mattermost counts the bot's own post as an
    unread DM for it; the bus never does. Its own, system and gated posts
    right after the mark move it on, so the next check does not read the DM."""
    own = post("own", "🔐 permission prompt", user=UID)
    mm.channel(DM, "D", total=1, posts=[post("sys", type_="system_join_channel", at=own["create_at"] - 1), own])
    assert await pending(client) == []
    assert watermark.read(UID)[DM] == ("own", own["create_at"])
    mm.read.clear()
    assert await pending(client) == []
    assert mm.read == []


async def test_the_counters_stand_in_only_without_a_last_post_time(mm, client):
    """RT-6.2: Mattermost's counters pick a channel only when it reports no
    time for the newest post."""
    mm.channel(DM, "D", total=1, posts=[post("d1", "hi")], last=0)
    mm.channel(QUIET, "D", total=0, posts=[post("q1", "hi")], last=0)
    assert await pending(client) == [(DM, 1)]
    assert [cid for cid, _ in mm.read] == [DM]


async def test_posts_for_all_agents_count_without_a_mention(mm, client, tmp_path):
    """RT-6.11 step 6: a post to all agents names nobody; it counts when someone
    else wrote it after the watermark, and the gate let it through."""
    mark = now_ms(-600)
    gated = post("g", props=ALL)
    mm.channel(CHAN, total=8, seen=2, posts=[
        post("old", props=ALL, at=mark - 1),
        post("p1", "main is red", props=ALL),
        post("p2", "for all of us", user=PERSON, props=ALL),
        post("own", props=ALL, user=UID),
        gated,
        post("sys", props=ALL, type_="system_join_channel"),
        post("plain", "just talk"),
    ])
    watermark.advance(UID, CHAN, "x", mark)
    d = tmp_path / "mm-agent-bus"
    (d / f"gate-{UID}.env").write_text(f"GATED=g {int(time.time())}\n")
    assert await pending(client) == [(CHAN, 2)]
    assert mm.read == [(CHAN, mark)]


async def test_reads_a_day_back_at_most_and_never_before_the_bot(mm, client):
    mm.born = now_ms(-3 * 86400)
    mm.channel(CHAN, total=1, posts=[post("p1", props=ALL, at=now_ms(-2 * 86400))],
               last=now_ms(-2 * 86400))
    mm.channel(DM, "D", posts=[post("d1", "hi")])
    assert await pending(client) == [(DM, 1)]
    assert mm.read and abs(mm.read[0][1] - now_ms(-86400)) < 5000
    mm.born, mm.read[:] = now_ms(-60), []
    client._me = None
    await pending(client)
    assert mm.read == [(DM, mm.born)]


async def test_detail_counts_an_agents_fyi_posts_for_me(mm, client):
    """RT-6.14 step 8: per channel, how many posts for this agent are an
    agent's fyi; a person's props count for nothing; at most five channels;
    no request beyond the count's but the one user lookup."""
    mm.channel(DM, "D", total=3, posts=[post("d1", "DONE", props=FYI),
                                        post("d2", "DONE", user=PERSON, props=FYI),
                                        post("d3", "what now?")])
    mm.channel(CHAN, total=6, mentions=1, posts=[
        post("c1", "DONE · thanks @agent-me.", props=FYI),
        post("c2", "DONE · @agent-mex", props=FYI),
        post("c3", "ACK @channel", props=FYI),
        post("c4", "RESULT", props={"a2a": {**FYI["a2a"], "to": "all"}}),
        post("c5", "@agent-me please look"),
    ])
    for i in range(5):
        mm.channel(f"{i}" * 26, "D", total=1, posts=[post(f"x{i}", "hi")])
    rows = await unread.channels(client, WATCHED)
    assert sum(r.waiting for r in rows) == 3 + 4 + 5
    reads = len(mm.read)
    got = await unread.detail(client, rows)
    assert len(mm.read) == reads
    assert got[:2] == [{"id": DM, "type": "D", "pending": 3, "fyi": 1},
                       {"id": CHAN, "type": "O", "pending": 4, "fyi": 3}]
    assert len(got) == unread.DETAIL_CHANNELS


def test_the_watermark_file_is_private_and_moves_forward_only(mm):
    """RT-6.2: read-<uid>.env, 0600, one line per channel; a mark never moves
    back by advance(), only hold() pulls it before a post still waiting."""
    assert watermark.advance(UID, DM, "p2", 2000)
    assert not watermark.advance(UID, DM, "p1", 1000)
    assert watermark.advance(UID, CHAN, "", 500)
    path = session_file.state_dir() / f"read-{UID}.env"
    assert path.read_text() == f"{CHAN} - 500\n{DM} p2 2000\n"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    watermark.hold(UID, DM, 1500)
    assert watermark.read(UID)[DM] == ("-", 1499)
    watermark.hold(UID, DM, 1600)  # already before it
    assert watermark.at(UID, DM) == 1499
    path.write_text(path.read_text() + "garbage\n../x p 1\n")
    assert set(watermark.read(UID)) == {CHAN, DM}
    watermark.remove(UID)
    assert not path.exists() and watermark.read(UID) == {}


async def test_only_what_wait_for_events_can_hand_over_counts(mm, client):
    """RT-6.2 step 1 (0.50.0): in a channel the server does not watch (the bot
    was added to it, or MM_PROJECT_CHANNEL changed without a restart) only a
    post naming the agent counts; an @channel, a post for all agents and a
    group post that names nobody become no event in wait_for_events, so they
    no longer keep the Stop hook draining an inbox that stays empty. Every
    counted post is one the inbox classifies."""
    mm.channel(NEW, posts=[post("n1", "@channel please read"), post("n2", props=ALL),
                           post("n3", "@agent-me over here")])
    mm.channel(GROUP, "G", posts=[post("g1", "hi all"), post("g2", "@agent-me you too")])
    mm.channel(CHAN, posts=[post("c1", "@channel standup"), post("c2", props=ALL)])
    mm.channel(DM, "D", posts=[post("d1", "hi")])
    rows = await unread.channels(client, WATCHED)
    assert [(r.channel.channel_id, r.waiting) for r in rows] == [(NEW, 1), (GROUP, 1), (CHAN, 2), (DM, 1)]
    for row in rows:
        for p in row.posts:
            p = dataclasses.replace(p, channel_id=row.channel.channel_id)
            assert classify_post(p, bot_user_id=UID, bot_username="agent-me",
                                 channel_type=row.channel.channel_type, watched_channel_ids=WATCHED)


async def test_the_stop_hook_count_leaves_out_an_unwatched_channel(mm, client):
    """RT-6.2 step 1, RT-6.17: GET /v1/pending counts with the channels the
    server watches (the work channel here); an @channel in another channel
    the bot is in counts nothing, a mention there still does."""
    mm.channel(NEW, posts=[post("n1", "@channel please read")])
    assert await hookapi.pending(client, {}, {}) == (200, {"count": 0})
    mm.channel(CHAN, posts=[post("c1", "@channel standup")])
    mm.posts[NEW].append(post("n2", "@agent-me over here"))
    mm.channels[0]["last_post_at"] = mm.posts[NEW][-1]["create_at"]
    assert await hookapi.pending(client, {}, {}) == (200, {"count": 2})

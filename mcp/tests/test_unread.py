"""RT-6.2 step 1, RT-6.17: what is waiting for this agent, counted by the MCP
server (unread.py; docs/arc42/06-runtime-view.md)."""

import json
import time

import httpx
import pytest

from mattermost_ai_agent_bus import unread
from mattermost_ai_agent_bus.client import MattermostClient

UID = "u" * 26
TEAM = "t" * 26
DM = "d" * 26
CHAN = "c" * 26
QUIET = "q" * 26
AGENT = "a" * 26
PERSON = "h" * 26
FYI = {"a2a": {"v": 1, "kind": "done", "intent": "fyi"}}
ALL = {"a2a": {"v": 1, "to": "all"}}


class FakeMattermost:
    def __init__(self):
        self.members: list[dict] = []
        self.channels: list[dict] = []
        self.posts: dict[str, list[dict]] = {}
        self.read: list[tuple[str, int]] = []

    def channel(self, cid, ctype="O", total=0, seen=0, mentions=0, viewed=0, posts=()):
        self.members.append({"channel_id": cid, "msg_count": seen, "mention_count": mentions,
                             "last_viewed_at": viewed})
        self.channels.append({"id": cid, "type": ctype, "total_msg_count": total})
        self.posts[cid] = list(posts)

    def handler(self, req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p == "/api/v4/users/me":
            return httpx.Response(200, json={"id": UID, "username": "agent-me"})
        if p == "/api/v4/teams/name/agents":
            return httpx.Response(200, json={"id": TEAM})
        if p == f"/api/v4/users/me/teams/{TEAM}/channels/members":
            return httpx.Response(200, json=self.members)
        if p == "/api/v4/users/me/channels":
            return httpx.Response(200, json=self.channels)
        if p.startswith("/api/v4/channels/") and p.endswith("/posts"):
            cid = p.split("/")[4]
            self.read.append((cid, int(req.url.params["since"])))
            posts = self.posts.get(cid, [])
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
    c = MattermostClient(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(mm.handler))
    return c


def now_ms(delta_s=0):
    return int((time.time() + delta_s) * 1000)


def post(pid, message="", user=AGENT, at=None, props=None, type_=""):
    return {"id": pid, "user_id": user, "message": message, "type": type_,
            "create_at": at if at is not None else now_ms(), "props": props or {}}


async def pending(client):
    return [(ch.channel_id, n) for ch, n in await unread.channels(client)]


async def test_a_dm_counts_its_messages_a_channel_its_mentions(mm, client):
    """RT-6.2 step 1: in a DM the unread messages (the total minus the
    watermark, which is no count itself), elsewhere the mentions."""
    mm.channel(DM, "D", total=5, seen=2)
    mm.channel(CHAN, total=40, seen=40, mentions=1)
    mm.channel(QUIET, total=9, seen=9)
    assert await pending(client) == [(DM, 3), (CHAN, 1)]
    assert mm.read == []  # no unread post outside a DM: no channel read


async def test_posts_for_all_agents_count_without_a_mention(mm, client, tmp_path):
    """RT-6.11 step 6: a post to all agents raises no mention counter; it counts
    when someone else wrote it since the last view, and the gate let it through."""
    viewed = now_ms(-600)
    gated = post("g", props=ALL)
    mm.channel(CHAN, total=8, seen=2, viewed=viewed, posts=[
        post("p1", "main is red", props=ALL),
        post("p2", "for all of us", user=PERSON, props=ALL),
        post("own", props=ALL, user=UID),
        gated,
        post("sys", props=ALL, type_="system_join_channel"),
        post("old", props=ALL, at=viewed - 1),
        post("plain", "just talk"),
    ])
    d = tmp_path / "mm-agent-bus"
    d.mkdir()
    (d / f"gate-{UID}.env").write_text(f"GATED=g {int(time.time())}\n")
    assert await pending(client) == [(CHAN, 2)]
    assert mm.read == [(CHAN, viewed)]


async def test_never_viewed_reads_a_day_back(mm, client):
    mm.channel(CHAN, total=1, posts=[post("p1", props=ALL, at=now_ms(-2 * 86400))])
    assert await pending(client) == []
    assert abs(mm.read[0][1] - now_ms(-86400)) < 5000


async def test_detail_counts_an_agents_fyi_posts_for_me(mm, client):
    """RT-6.14 step 8: per channel, how many posts for this agent are an
    agent's fyi; a person's props count for nothing; at most five channels."""
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
        mm.channel(f"{i}" * 26, "D", total=1)
    rows = await unread.channels(client)
    assert sum(n for _, n in rows) == 3 + 2 + 5   # mention + the post for all
    got = await unread.detail(client, rows)
    assert got[:2] == [{"id": DM, "type": "D", "pending": 3, "fyi": 1},
                       {"id": CHAN, "type": "O", "pending": 2, "fyi": 3}]
    assert len(got) == unread.DETAIL_CHANNELS

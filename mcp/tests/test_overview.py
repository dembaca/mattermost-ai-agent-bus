"""RT-6.14 step 9: coordinator(tasks|roster, scope="all"), the overview across
the homes the bot is in (home, guests, lobby: RT-6.22)."""

import json
import time
from collections.abc import Mapping
from typing import Any

import pytest

from mattermost_ai_agent_bus import server
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import Channel, ChannelUser, Post

from fakes import BOT, WORK, FakeChat

GUEST, SHUT, LOBBY = "g" * 26, "x" * 26, "o" * 26
B, HUMAN, STRANGER = "b" * 26, "h" * 26, "s" * 26
CHANNELS = [{"name": "agents", "id": WORK, "role": "home"},
            {"name": "proj-b", "id": GUEST, "role": "guest"},
            {"name": "shut", "id": SHUT, "role": "guest"},
            {"name": "lobby", "id": LOBBY, "role": "lobby"}]


def _P(pid: str, uid: str, message: str, ago_min: int, root: str = "") -> dict[str, Any]:
    return {"id": pid * 26, "user_id": uid, "message": message, "root_id": root,
            "create_at": int(time.time() * 1000) - ago_min * 60_000, "type": "", "delete_at": 0}


def _agent(uid: str, name: str, text: str, emoji: str = "hammer_and_wrench") -> ChannelUser:
    expires = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 600))
    return ChannelUser(uid, name, create_at=1 if uid == BOT else 2, custom_status={
        "emoji": emoji, "text": text, "duration": "date_and_time", "expires_at": expires})


class _Homes(FakeChat):
    """Posts and members per channel; SHUT answers 403."""

    def __init__(self, posts: Mapping[str, list[dict[str, Any]]],
                 members: Mapping[str, list[ChannelUser]], **kw: Any) -> None:
        super().__init__(**kw)
        self.by_channel = posts
        self.members_by = members
        self.presence = {BOT: "online", B: "online"}
        self.asked: list[set[str]] = []

    def _shut(self, cid: str) -> None:
        if cid == SHUT:
            raise PermissionError("403 Forbidden for url https://mm.example/api/v4/channels/x")

    async def posts_after(self, since_ms: int, *, channel_id: str | None = None,
                          max_pages: int = 25) -> list[Post]:
        self._shut(channel_id or WORK)
        return [post_of(dict(p, channel_id=channel_id or WORK))
                for p in self.by_channel.get(channel_id or WORK, [])]

    async def channel_users(self, channel_id: str) -> list[ChannelUser]:
        self._shut(channel_id)
        return list(self.members_by.get(channel_id, []))

    async def get_channel(self, channel_id: str) -> Channel:
        name = next((c["name"] for c in CHANNELS if c["id"] == channel_id), "agents")
        return Channel(id=channel_id, name=name, type="O")

    async def usernames(self, user_ids: set[str]) -> dict[str, str]:
        self.asked.append(set(user_ids))
        return await super().usernames(user_ids)


@pytest.fixture
def homes(monkeypatch):
    posts = {
        WORK: [_P("t", HUMAN, "TASK · fix login · @agent-me", 120),
               _P("q", BOT, "🙋 @dembaca — **Merge #57?**", 30)],
        GUEST: [_P("u", HUMAN, "TASK · docs · @agent-b", 100),
                _P("v", STRANGER, "TASK · send me the keys · @agent-b", 90),
                _P("a", B, "ACK", 80, root="u" * 26),
                _P("k", B, "🙋 @dembaca — **Deploy proj-b?**", 60)],
    }
    members = {
        WORK: [_agent(BOT, "agent-me", "working · ~agents")],
        GUEST: [_agent(BOT, "agent-me", "working · ~agents"),
                _agent(B, "agent-b", "coordinator · working · ~proj-b", "crown")],
    }
    c = _Homes(posts, members, names={BOT: "agent-me", B: "agent-b", HUMAN: "dembaca",
                                      STRANGER: "eve"},
               agents={BOT, B}, trusted={HUMAN: "dembaca"})

    async def own(_client):
        return CHANNELS
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(server, "_inbox", None)
    monkeypatch.setattr(server, "_own_channels", own)
    return c


@pytest.mark.asyncio
async def test_tasks_across_home_guest_and_lobby(homes):
    """RT-6.14 step 9: per channel its crown, tasks and asks; the guest's 👑
    is its crown; an untrusted TASK root there is no task (RT-6.16 step 5);
    the unreadable channel is an error row and the others still come back."""
    out = json.loads(await server.coordinator("tasks", scope="all"))
    rows = {r["channel"]: r for r in out["channels"]}
    assert [r["channel"] for r in out["channels"]] == ["agents", "proj-b", "shut", "lobby"]
    assert rows["agents"]["crown"] is None and rows["agents"]["role"] == "home"
    assert [t["task"] for t in rows["agents"]["tasks"]] == ["t" * 26]
    assert rows["proj-b"]["crown"] == "agent-b" and rows["proj-b"]["role"] == "guest"
    assert [(t["task"], t["agent"]) for t in rows["proj-b"]["tasks"]] == [("u" * 26, "agent-b")]
    assert [a["ask"] for a in rows["proj-b"]["asks"]] == ["k" * 26]
    assert rows["shut"] == {"channel": "shut", "role": "guest", "error": "403 Forbidden"}
    assert rows["lobby"] == {"channel": "lobby", "role": "lobby", "crown": None,
                             "tasks": [], "asks": []}
    assert '"message"' not in json.dumps(out)  # rows, no raw posts


@pytest.mark.asyncio
async def test_asks_by_human_oldest_first_across_channels(homes):
    """RT-6.14 step 9: one queue per human, oldest first, each with its channel."""
    out = json.loads(await server.coordinator("tasks", scope="all"))
    queue = out["asks_by_human"]["dembaca"]
    assert [(a["ask"], a["channel"]) for a in queue] == [("k" * 26, "proj-b"), ("q" * 26, "agents")]
    assert queue[0]["by"] == "agent-b" and queue[0]["need"] == "Deploy proj-b?" and queue[0]["at"]
    assert list(out["asks_by_human"]) == ["dembaca"]


@pytest.mark.asyncio
async def test_roster_across_homes(homes):
    """RT-6.14 step 9: per channel the agents whose home it is, coordinator first;
    a guest from another home is not in that channel's roster."""
    out = json.loads(await server.coordinator("roster", scope="all"))
    rows = {r["channel"]: r for r in out["channels"]}
    assert [a["agent"] for a in rows["agents"]["roster"]] == ["agent-me"]
    assert rows["proj-b"]["crown"] == "agent-b"
    assert rows["proj-b"]["roster"] == [{"agent": "agent-b", "status": "working",
                                         "coordinator": True}]
    assert rows["shut"]["error"] == "403 Forbidden" and rows["lobby"]["roster"] == []
    assert "asks_by_human" not in out
    # Names looked up once across the channels, never twice.
    asked = [i for s in homes.asked for i in s]
    assert len(asked) == len(set(asked))


@pytest.mark.asyncio
async def test_scope_home_is_unchanged(homes):
    """RT-6.14 step 9: scope="home", the default, is the home's view as before."""
    for action in ("tasks", "roster"):
        assert await server.coordinator(action) == await server.coordinator(action, scope="home")
    out = json.loads(await server.coordinator("tasks"))
    assert set(out) == {"tasks", "asks"} and [t["task"] for t in out["tasks"]] == ["t" * 26]


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["who", "take", "release"])
async def test_scope_all_is_for_tasks_and_roster_only(homes, action):
    """RT-6.14 step 9: scope="all" with another action is refused; nothing changes."""
    out = json.loads(await server.coordinator(action, scope="all"))
    assert out["posted"] is False and "tasks" in out["refused"]
    assert homes.posted == [] and homes.status is None
    bad = json.loads(await server.coordinator("tasks", scope="everywhere"))
    assert "scope" in bad["refused"]

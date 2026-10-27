"""Tests for client helpers (no live Mattermost)."""

import pytest

from mattermost_ai_agent_bus.client import MattermostAdapter, ws_url_from_http
from mattermost_ai_agent_bus.model import UnreadChannel


@pytest.mark.parametrize(
    "http,expected",
    [
        ("https://chat.example.com", "wss://chat.example.com/api/v4/websocket"),
        ("https://chat.example.com/", "wss://chat.example.com/api/v4/websocket"),
        (
            "http://localhost:8065",
            "ws://localhost:8065/api/v4/websocket",
        ),
        (
            "https://chat.example.com/sub",
            "wss://chat.example.com/sub/api/v4/websocket",
        ),
        (
            "https://chat.example.com/api/v4",
            "wss://chat.example.com/api/v4/websocket",
        ),
    ],
)
def test_ws_url_from_http(http, expected):
    assert ws_url_from_http(http) == expected


def test_from_env_requires_url(monkeypatch):
    monkeypatch.delenv("MM_CHAT_URL", raising=False)
    monkeypatch.delenv("MATTERMOST_URL", raising=False)
    with pytest.raises(RuntimeError, match="MM_CHAT_URL"):
        MattermostAdapter.from_env()


def test_from_env_aliases(monkeypatch):
    monkeypatch.setenv("MATTERMOST_URL", "https://mm.example/")
    monkeypatch.setenv("MATTERMOST_TOKEN", "tok")
    monkeypatch.setenv("MM_TEAM", "t1")
    monkeypatch.setenv("MM_CHANNEL", "c1")
    monkeypatch.delenv("MM_CHAT_URL", raising=False)
    monkeypatch.delenv("MM_BOT_TOKEN", raising=False)
    c = MattermostAdapter.from_env()
    assert c.base_url == "https://mm.example"
    assert c.token == "tok"
    assert c.team_name == "t1"
    assert c.channel_name == "c1"
    assert c.register_url == "https://mm.example/register/v1/agents"


def test_unexpanded_placeholders_are_treated_as_unset(monkeypatch):
    """A plugin manifest passes "${VAR}"; unset vars can arrive verbatim.

    Regression: the literal "${MM_PROJECT_CHANNEL}" was taken for a channel name,
    so every post_message 404'd while an explicit channel_id still worked.
    """
    monkeypatch.setenv("MM_CHAT_URL", "https://mm.example")
    monkeypatch.setenv("MM_CHANNEL", "agents")
    monkeypatch.setenv("MM_PROJECT_CHANNEL", "${MM_PROJECT_CHANNEL}")
    monkeypatch.setenv("MM_REGISTER_URL", "${MM_REGISTER_URL}")
    monkeypatch.setenv("MM_REG_SECRET", "${MM_REG_SECRET}")
    monkeypatch.setenv("MM_BOT_TOKEN", "${MM_BOT_TOKEN}")
    c = MattermostAdapter.from_env()
    assert c.project_channel_name == ""
    assert c.work_channel_name == "agents"
    assert c.register_url == "https://mm.example/register/v1/agents"
    assert c.reg_secret is None
    assert c.token is None


def test_placeholder_like_value_that_is_real_is_kept(monkeypatch):
    """Only a bare ${NAME} is ignored — a real name containing $ is not."""
    monkeypatch.setenv("MM_CHAT_URL", "https://mm.example")
    monkeypatch.setenv("MM_PROJECT_CHANNEL", "proj-${weird}-name")
    c = MattermostAdapter.from_env()
    assert c.project_channel_name == "proj-${weird}-name"


def test_from_env_project_channel_defaults_to_empty(monkeypatch):
    monkeypatch.setenv("MM_CHAT_URL", "https://mm.example")
    monkeypatch.setenv("MM_CHANNEL", "agents")
    monkeypatch.delenv("MM_PROJECT_CHANNEL", raising=False)
    c = MattermostAdapter.from_env()
    assert c.project_channel_name == ""
    # Without a project channel the work channel is the default channel.
    assert c.work_channel_name == "agents"


def test_project_channel_becomes_work_channel(monkeypatch):
    monkeypatch.setenv("MM_CHAT_URL", "https://mm.example")
    monkeypatch.setenv("MM_CHANNEL", "agents")
    monkeypatch.setenv("MM_PROJECT_CHANNEL", "proj-bus")
    c = MattermostAdapter.from_env()
    # The default channel is kept: the agent stays addressable there.
    assert c.channel_name == "agents"
    assert c.project_channel_name == "proj-bus"
    assert c.work_channel_name == "proj-bus"


@pytest.mark.asyncio
async def test_get_channel_id_caches_per_name(monkeypatch):
    """Both channels resolve independently and each is fetched only once."""
    c = MattermostAdapter(
        base_url="https://mm.example",
        token="tok",
        channel_name="agents",
        project_channel_name="proj-bus",
    )
    calls: list[str] = []

    async def fake_api(method, path, **kwargs):
        calls.append(path)
        if path.startswith("/api/v4/teams/name/"):
            return {"id": "team1000000000000000000000"}
        return {"id": f"cid-{path.rsplit('/', 1)[-1]}"}

    monkeypatch.setattr(c, "_api", fake_api)

    assert await c.get_channel_id() == "cid-proj-bus"  # default = work channel
    assert await c.get_channel_id("agents") == "cid-agents"
    assert await c.get_channel_id() == "cid-proj-bus"  # cached, no new call

    channel_calls = [p for p in calls if "/channels/name/" in p]
    assert len(channel_calls) == 2


@pytest.mark.asyncio
async def test_session_start_name_validation():
    c = MattermostAdapter(
        base_url="https://mm.example",
        reg_secret="sec",
        register_url="https://mm.example/register/v1/agents",
    )
    with pytest.raises(ValueError):
        await c.session_start("X")


@pytest.mark.asyncio
async def test_member_channels_carry_the_newest_post_and_the_counters(monkeypatch):
    """RT-6.2: every channel of the bot, with when its newest post came, which
    the read watermark is held against; the counters only stand in where
    Mattermost leaves that out. A DM counts every unread message, a channel
    only mentions."""
    c = MattermostAdapter(base_url="https://mm.example", token="tok")

    async def fake_api(method, path, **kwargs):
        if path == "/api/v4/users/me":
            return {"id": "bot1", "create_at": 5000}
        if path.startswith("/api/v4/teams/name/"):
            return {"id": "team1000000000000000000000"}
        if path.endswith("/channels/members"):
            return [{"channel_id": "proj", "mention_count": 1, "last_viewed_at": 0},
                    {"channel_id": "dm1", "msg_count": 2, "last_viewed_at": 9000},
                    {"channel_id": "quiet", "msg_count": 4, "mention_count": 0}]
        assert path == "/api/v4/users/me/channels"
        return [{"id": "proj", "type": "O"}, {"id": "dm1", "type": "D", "total_msg_count": 3},
                {"id": "quiet", "type": "O", "total_msg_count": 9, "last_post_at": 7000}]

    monkeypatch.setattr(c, "_api", fake_api)
    got = await c.member_channels()
    assert got == [UnreadChannel("proj"), UnreadChannel("dm1", "D"), UnreadChannel("quiet")]
    assert [(ch.last_post_at, ch.pending, ch.unread) for ch in got] == [
        (0, 1, 0), (0, 1, 1), (7000, 0, 5)]


@pytest.mark.asyncio
async def test_get_me_started_before_a_takeover_keeps_no_old_identity(monkeypatch):
    """RT-6.17: the hook API names the bot from get_me; a /users/me that was
    under way when the session took another bot over (_take) must not cache
    the old bot, it asks again as the new one."""
    import asyncio

    from mattermost_ai_agent_bus.client import SessionInfo

    c = MattermostAdapter(base_url="https://mm.example", token="old", channel_name="agents")
    started, release = asyncio.Event(), asyncio.Event()

    async def fake_api(method, path, **kwargs):
        token = c.token
        if token == "old":
            started.set()
            await release.wait()
        return {"id": token}

    monkeypatch.setattr(c, "_api", fake_api)
    pending = asyncio.create_task(c.get_me())
    await started.wait()
    c._take(SessionInfo(url="https://mm.example", name="n", username="agent-n",
                        user_id="u" * 26, bot_token="new"))
    release.set()
    assert (await pending)["id"] == "new"
    assert (await c.get_me())["id"] == "new"

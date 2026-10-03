"""Which channels the inbox watches (no live Mattermost)."""

import pytest

from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox


class _FakeClient:
    """Minimal stand-in for MattermostClient.ensure_identity's needs."""

    def __init__(self, channel_name="agents", project_channel_name="", missing=()):
        self.channel_name = channel_name
        self.project_channel_name = project_channel_name
        self.missing = set(missing)
        self.resolved: list[str] = []

    async def get_me(self):
        return {"id": "bot1", "username": "agent-test"}

    async def get_channel_id(self, name=None):
        name = name or (self.project_channel_name or self.channel_name)
        self.resolved.append(name)
        if name in self.missing:
            raise RuntimeError(f"channel {name!r} does not exist")
        return f"cid-{name}"


@pytest.mark.asyncio
async def test_watches_default_channel_only():
    client = _FakeClient()
    inbox = WebSocketInbox(client=client)
    await inbox.ensure_identity()
    assert inbox.watched_channel_ids == {"cid-agents"}


@pytest.mark.asyncio
async def test_watches_default_and_project_channel():
    """Work happens in the project channel; the agent stays addressable in both."""
    client = _FakeClient(project_channel_name="proj-bus")
    inbox = WebSocketInbox(client=client)
    await inbox.ensure_identity()
    assert inbox.watched_channel_ids == {"cid-agents", "cid-proj-bus"}
    assert client.resolved == ["agents", "proj-bus"]


@pytest.mark.asyncio
async def test_missing_project_channel_does_not_lose_the_default():
    """A bad MM_PROJECT_CHANNEL degrades to the default channel, not to nothing."""
    client = _FakeClient(project_channel_name="typo", missing={"typo"})
    inbox = WebSocketInbox(client=client)
    await inbox.ensure_identity()
    assert inbox.watched_channel_ids == {"cid-agents"}


@pytest.mark.asyncio
async def test_explicit_watch_list_is_not_overridden():
    client = _FakeClient(project_channel_name="proj-bus")
    inbox = WebSocketInbox(client=client, watched_channel_ids={"preset"})
    await inbox.ensure_identity()
    assert inbox.watched_channel_ids == {"preset"}
    assert client.resolved == []

"""announce posts the HELLO in one format, with for:/trusts: from configuration."""

import pytest

from mattermost_ai_agent_bus import server


class _Client:
    def __init__(self, trusted):
        self.trusted = trusted
        self.posted = []

    async def get_me(self):
        return {"id": "b" * 26, "username": "agent-vtpm-hogan-1"}

    async def trusted_humans(self):
        return self.trusted

    async def post(self, message, channel_id=None, root_id=None):
        self.posted.append((message, root_id))
        return {"id": "p1", "message": message, "user_id": "b" * 26, "channel_id": "c1"}

    async def usernames(self, ids):
        return {}

    async def get_channel(self, cid):
        return {"name": "proj-vtpm"}


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for v in ("MM_RESPONSIBLE_HUMAN", "MM_AGENT_KIND"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(server.lease, "crown_held", lambda uid: False)


@pytest.mark.asyncio
async def test_announce_writes_the_fixed_format(monkeypatch):
    c = _Client({"h" * 26: "dembaca", "k" * 26: "kollegin"})
    monkeypatch.setattr(server, "_get_client", lambda: c)
    await server.announce(project="vtpm-mds", task="Koordination,\n hogan-Verifikation",
                          model="Claude Code · Opus 5.5", capabilities="Proxmox-Stack, VM 399",
                          branch="#51", role="coordinator")
    message, root = c.posted[0]
    assert root is None
    assert message.splitlines() == [
        "HELLO @agent-vtpm-hogan-1",
        "project: vtpm-mds · branch/PR: #51",
        "task: Koordination, hogan-Verifikation",
        "model: Claude Code · Opus 5.5",
        "for: dembaca · trusts: dembaca, kollegin",  # RT-6.11: no mentions
        "capabilities: Proxmox-Stack, VM 399",
        "role: coordinator",
    ]


@pytest.mark.asyncio
async def test_announce_without_trust_and_autonomous(monkeypatch):
    c = _Client({})
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setenv("MM_RESPONSIBLE_HUMAN", "@ops-team")
    monkeypatch.setenv("MM_AGENT_KIND", "autonomous")
    await server.announce(project="infra", task="pending", model="Hermes")
    lines = c.posted[0][0].splitlines()
    assert "for: ops-team (autonomous) · trusts: –" in lines
    assert "branch/PR: –" in lines[1] and lines[-1] == "role: member"
    with pytest.raises(ValueError):
        await server.announce(project="x", task="y", model="z", role="boss")

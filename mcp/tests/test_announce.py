"""announce posts the HELLO in one format, with for/trusts from configuration."""

import json

import pytest

from mattermost_ai_agent_bus import a2a, server
from mattermost_ai_agent_bus.client import post_of

from fakes import FakeChat


def _Client(trusted):  # noqa: N802
    return FakeChat(uid="b" * 26, username="agent-vtpm-hogan-1", trusted=trusted,
                    channel="proj-vtpm", post_id="p1")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for v in ("MM_RESPONSIBLE_HUMAN", "MM_AGENT_KIND"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(server.lease, "crown_held", lambda uid: False)


@pytest.mark.asyncio
async def test_announce_writes_the_fixed_format(monkeypatch):
    """RT-6.14 step 1: three lines, no self-mention (Mattermost shows the
    author); a plain project and branch in backticks."""
    c = _Client({"h" * 26: "dembaca", "k" * 26: "kollegin"})
    monkeypatch.setattr(server, "_get_client", lambda: c)
    await server.announce(project="vtpm-mds", task="Koordination,\n hogan-Verifikation",
                          model="Claude Code · Opus 5.5", capabilities="Proxmox-Stack, VM 399",
                          branch="#51", role="coordinator")
    message, root = c.posted[0]["message"], c.posted[0]["root_id"]
    assert root is None
    assert message.splitlines() == [
        "HELLO · **Koordination, hogan-Verifikation**",
        "`vtpm-mds` · `#51` · Claude Code · Opus 5.5 · coordinator",
        "for dembaca · trusts dembaca, kollegin · can: Proxmox-Stack, VM 399",  # RT-6.11: no mentions
    ]
    assert "@" not in message


@pytest.mark.asyncio
async def test_announce_writes_a_link_branch_as_given(monkeypatch):
    """RT-6.14 step 1: a branch that is a link, or text with spaces, gets no backticks."""
    c = _Client({"h" * 26: "dembaca"})
    monkeypatch.setattr(server, "_get_client", lambda: c)
    link = "[PR #134](https://example.org/o/r/pull/134)"
    await server.announce(project="mattermost ai bus", task="review", model="Opus", branch=link)
    assert c.posted[0]["message"].splitlines()[1] == f"mattermost ai bus · {link} · Opus · member"
    await server.announce(project="bus", task="review", model="Opus", branch="https://x.example/pr/1")
    assert c.posted[1]["message"].splitlines()[1] == "`bus` · https://x.example/pr/1 · Opus · member"
    card = a2a.card_of(post_of({"message": c.posted[0]["message"]}), "agent-vtpm-hogan-1")
    assert card["meta"]["branch"] == link and card["meta"]["project"] == "mattermost ai bus"


@pytest.mark.asyncio
async def test_announce_never_notifies_everyone(monkeypatch):
    """RT-6.11 step 1: a task line that says @channel is written plain."""
    c = _Client({"h" * 26: "dembaca"})
    monkeypatch.setattr(server, "_get_client", lambda: c)
    await server.announce(project="vtpm-mds", task="tell @channel about the freeze",
                          model="Claude Code · Opus 5.5")
    lines = c.posted[0]["message"].splitlines()
    assert lines[0] == "HELLO · **tell channel about the freeze**"


@pytest.mark.asyncio
async def test_announce_stores_the_card_in_props(monkeypatch):
    """RT-6.14 step 1: the same fields as an A2A-shaped card in the HELLO's
    props, which no slim post shows; the text with its author gives the same card."""
    c = _Client({"h" * 26: "dembaca"})
    monkeypatch.setattr(server, "_get_client", lambda: c)
    out = await server.announce(project="vtpm-mds", task="hogan", model="Claude Code · Opus 5.5",
                                capabilities="Proxmox-Stack, VM 399", branch="#51")
    props = c.posted[0]["props"]
    card = props["agent_card"]
    assert props["a2a"] == {"v": 1, "kind": "hello", "intent": "fyi"}
    assert card["name"] == "agent-vtpm-hogan-1" and card["description"] == "hogan"
    assert card["skills"] == [{"id": "proxmox-stack", "name": "Proxmox-Stack"},
                              {"id": "vm-399", "name": "VM 399"}]
    assert card["meta"] == {"project": "vtpm-mds", "branch": "#51", "model": "Claude Code · Opus 5.5",
                            "role": "member", "kind": "session", "for": "dembaca",
                            "trusts": ["dembaca"]}
    assert "agent_card" not in out and "props" not in out
    # A HELLO written by hand has no props; the name comes from its author.
    from_text = a2a.card_of(post_of({"message": c.posted[0]["message"]}), "agent-vtpm-hogan-1")
    assert {k: from_text[k] for k in ("name", "description", "skills", "meta")} == {
        k: card[k] for k in ("name", "description", "skills", "meta")}
    crowned = a2a.card_of(post_of({"message": "👑 " + c.posted[0]["message"]}))
    assert crowned["name"] == "" and crowned["description"] == "hogan"


def test_a_hello_before_0_65_4_still_gives_its_card():
    """RT-6.14 step 1: the seven lines older versions wrote (no props) are
    still read, the name from `HELLO @<name>`."""
    old = ("HELLO @agent-old\nproject: vtpm-mds · branch/PR: #51\ntask: hogan\n"
           "model: Claude Code · Opus 5.5\nfor: dembaca (autonomous) · trusts: dembaca, kollegin\n"
           "capabilities: Proxmox-Stack, VM 399\nrole: coordinator")
    card = a2a.card_of(post_of({"message": old}))
    assert card["name"] == "agent-old" and card["description"] == "hogan"
    assert card["skills"] == [{"id": "proxmox-stack", "name": "Proxmox-Stack"},
                              {"id": "vm-399", "name": "VM 399"}]
    assert card["meta"] == {"project": "vtpm-mds", "branch": "#51", "model": "Claude Code · Opus 5.5",
                            "role": "coordinator", "kind": "session", "for": "dembaca",
                            "trusts": ["dembaca", "kollegin"]}


@pytest.mark.asyncio
async def test_announce_without_trust_and_autonomous(monkeypatch):
    """RT-6.14 step 1: an autonomous agent says so; no branch, trust or capabilities: –."""
    c = _Client({})
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setenv("MM_RESPONSIBLE_HUMAN", "@ops-team")
    monkeypatch.setenv("MM_AGENT_KIND", "autonomous")
    await server.announce(project="infra", task="pending", model="Hermes")
    lines = c.posted[0]["message"].splitlines()
    assert lines == ["HELLO · **pending**", "`infra` · `–` · Hermes · member",
                     "for ops-team (autonomous) · trusts – · can: –"]
    card = a2a.card_of(post_of({"message": c.posted[0]["message"]}), "agent-vtpm-hogan-1")
    assert card["meta"] == {"project": "infra", "model": "Hermes", "role": "member",
                            "kind": "session", "for": "ops-team"} and card["skills"] == []
    assert "refused" in json.loads(await server.announce(project="x", task="y", model="z", role="boss"))

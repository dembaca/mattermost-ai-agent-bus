"""RT-6.2: the agent's own latest post, kept for the Stop hook; BYE at exit."""

from __future__ import annotations

import json

import httpx
import pytest

from mattermost_ai_agent_bus import a2a, ownpost, server
from mattermost_ai_agent_bus.client import MattermostAdapter, post_of
from mattermost_ai_agent_bus.model import Me

UID, WORK, DM = "u" * 26, "w" * 26, "d" * 26


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
            return {"id": path.rsplit("/", 1)[1], "channel_id": WORK}
        assert path == "/api/v4/posts"
        return {"id": f"p{next(posts)}".ljust(26, "0"), "user_id": UID, "create_at": 1000 + len(json["message"]),
                **json}

    monkeypatch.setattr(c, "_api", api)
    return c


@pytest.mark.asyncio
async def test_the_work_channel_post_is_remembered_with_its_hello(client):
    """Step 2: whoever posts as the agent keeps its latest post in the work
    channel; the HELLO thread stays known through later posts, in any thread."""
    await client.post("HELLO @agent-me\nproject: x", props={"a2a": a2a.annotate("HELLO")})
    assert ownpost.read(UID) == {"CHANNEL": WORK, "ID": "p1000000000000000000000000", "AT": "1026",
                                 "MSG": "HELLO @agent-me project: x", "HELLO": "p1000000000000000000000000"}
    await client.post("👑 ACK · on it", root_id="p1000000000000000000000000", channel_id=WORK)
    last = ownpost.read(UID)
    assert (last["ID"], last["MSG"], last["HELLO"]) == ("p2".ljust(26, "0"), "ACK · on it", "p1000000000000000000000000")


@pytest.mark.asyncio
async def test_a_post_elsewhere_leaves_it_as_it_was(client):
    await client.post("HELLO @agent-me")
    await client.post("secret between us", channel_id=DM)
    assert ownpost.read(UID)["MSG"] == "HELLO @agent-me"


@pytest.mark.asyncio
async def test_no_bot_id_no_file(client, monkeypatch):
    monkeypatch.delenv("MM_BOT_USER_ID")
    await client.post("HELLO @agent-me")
    assert not ownpost.path(UID).exists()


def _bye_client(monkeypatch, seen, status=200):
    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(status, json={"id": "bye1", "create_at": 5000, **seen[-1][2]})

    real = httpx.Client
    monkeypatch.setattr("mattermost_ai_agent_bus.client.httpx.Client",
                        lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    return MattermostAdapter(base_url="https://mm.example", token="tok")


def _file(msg, hello="p1000000000000000000000000"):
    ownpost.record(UID, WORK, post_of({"id": "p9", "create_at": 1, "message": msg}))
    if hello:
        ownpost.path(UID).write_text(ownpost.path(UID).read_text().replace("HELLO=", f"HELLO={hello}"))


def test_bye_goes_to_the_hello_thread_with_its_props(monkeypatch):
    """Step 2 (sign-off): a session that ends on a busy post says BYE in its
    HELLO thread, with the kind and intent every BYE carries (RT-6.14)."""
    seen = []
    _file("PROGRESS · tests running")
    assert ownpost.bye_sync(_bye_client(monkeypatch, seen), UID) == "BYE posted"
    [(method, path, body)] = seen
    assert (method, path) == ("POST", "/api/v4/posts")
    assert body["root_id"] == "p1000000000000000000000000" and body["channel_id"] == WORK
    assert body["message"] == "`BYE` · session ended without sign-off"  # RT-6.14 step 2
    assert body["props"]["a2a"] == {"v": 1, "kind": "bye", "intent": "fyi"}
    assert ownpost.read(UID)["MSG"].startswith("`BYE`")  # a second handler adds none
    assert ownpost.bye_sync(_bye_client(monkeypatch, seen), UID) == "signed off"
    assert len(seen) == 1


@pytest.mark.parametrize("msg", ["IDLE · done: x", "BYE · done", "👑 IDLE · next: y"])
def test_no_bye_after_a_sign_off(monkeypatch, msg):
    seen = []
    _file(msg)
    assert ownpost.bye_sync(_bye_client(monkeypatch, seen), UID) == "signed off"
    assert seen == []


def test_a_handover_is_no_sign_off(monkeypatch):
    seen = []
    _file("HANDOVER · to agent-x")
    assert ownpost.bye_sync(_bye_client(monkeypatch, seen), UID) == "BYE posted"


def test_no_bye_without_an_announcement(monkeypatch):
    """An agent that never announced is not part of the coordination."""
    seen = []
    assert ownpost.bye_sync(_bye_client(monkeypatch, seen), UID) == "no announcement"
    _file("PROGRESS · x", hello="")
    assert ownpost.bye_sync(_bye_client(monkeypatch, seen), UID) == "no announcement"
    assert seen == []


def test_a_failing_bye_never_raises(monkeypatch):
    _file("PROGRESS · x")
    out = ownpost.bye_sync(_bye_client(monkeypatch, [], status=500), UID)
    assert out.startswith("BYE not posted")
    assert ownpost.read(UID)["MSG"] == "PROGRESS · x"


def test_the_exit_handler_says_bye_before_it_unregisters(monkeypatch):
    """Step 2: the BYE goes out first; the unregistering follows."""
    order = []
    c = MattermostAdapter(base_url="https://mm.example", token="tok", reg_secret="s",
                         register_url="https://mm.example/register/v1/agents")
    monkeypatch.setattr(ownpost, "bye_sync", lambda *_: order.append("bye") or "BYE posted")
    monkeypatch.setattr(c, "shutdown_sync", lambda: order.append("unregister") or "unregistered x")
    monkeypatch.setattr(server, "_client", c)
    monkeypatch.setattr(server, "_exit_handled", False)
    monkeypatch.setattr(server.stats, "summary", lambda *a: None)
    server._cleanup_on_exit()
    assert order == ["bye", "unregister"]


def test_unregistering_removes_the_file(monkeypatch):
    _file("IDLE")
    real = httpx.Client
    monkeypatch.setattr("mattermost_ai_agent_bus.client.httpx.Client", lambda **kw: real(
        transport=httpx.MockTransport(lambda req: httpx.Response(
            200, json={"id": UID, "username": "agent-x"})), **kw))
    c = MattermostAdapter(base_url="https://mm.example", token="tok",
                         register_url="https://mm.example/register/v1/agents")
    monkeypatch.setattr(c, "_me", Me(id=UID, username="agent-x"))
    monkeypatch.setattr(c, "session", None)
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    c.shutdown_sync()
    assert not ownpost.path(UID).exists()


@pytest.mark.asyncio
async def test_a_reminder_is_not_the_agents_post(client):
    """RT-6.2 step 2, RT-6.19 (0.65.1): a reminder the server sends on its own
    leaves the agent's latest post, its IDLE, as it was."""
    await client.post("IDLE · done: x", props={"a2a": a2a.annotate("IDLE")})
    before = ownpost.read(UID)
    await client.post("🔔 @dembaca — reminder: **Merge?**", root_id="p1".ljust(26, "0"),
                      props=a2a.props(kind=a2a.REMINDER))
    assert ownpost.read(UID) == before and ownpost.signed_off(before["MSG"], before)


@pytest.mark.asyncio
async def test_an_addressed_delivery_ends_the_sign_off(client, monkeypatch):
    """RT-6.2 step 2 (0.65.1): a mention, DM, a trusted human's answer on an ask
    or (with the crown) a human's task handed to the agent means something
    changed on the bus since its IDLE; a gated mention, an agent's fyi post
    or a stall event does not. The next own post clears it."""
    from mattermost_ai_agent_bus import lease
    from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent

    async def my_id(_client):
        return UID

    monkeypatch.setattr(server, "_get_client", lambda: client)
    monkeypatch.setattr(server, "_my_id", my_id)
    monkeypatch.setattr(lease, "crown_held", lambda uid: False)
    await client.post("IDLE · done: x")

    def ev(kind, gated=""):
        return ClassifiedEvent(kind=kind, gated=gated, post=post_of({"id": "x" * 26, "user_id": DM}))

    quiet = [(ev("mention", gated="not trusted"), {"kind": "mention"}),
             (ev("watched_channel"), {"kind": "watched_channel", "from_agent": True, "intent": "fyi"}),
             (ev("stall"), {"kind": "stall"})]
    await server._note_delivered([e for e, _ in quiet], [i for _, i in quiet])
    rec = ownpost.read(UID)
    assert ownpost.signed_off(rec["MSG"], rec)
    await server._note_delivered([ev("mention")], [{"kind": "mention"}])
    rec = ownpost.read(UID)
    assert rec["DELIVERED"] == "1" and not ownpost.signed_off(rec["MSG"], rec)
    await client.post("IDLE · answered")
    rec = ownpost.read(UID)
    assert "DELIVERED" not in rec and ownpost.signed_off(rec["MSG"], rec)

"""RT-6.12: bus events pushed into the session as a Claude Code channel."""

import asyncio

import anyio
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp_types import INVALID_REQUEST, JSONRPCNotification, JSONRPCRequest
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from mattermost_ai_agent_bus import channel, server

MENTION = {
    "kind": "mention",
    "from_trusted_human": True,
    "trusted_human": "dembaca",
    "post": {"id": "p1", "root_id": "r1", "channel": "agents", "author": "dembaca",
             "at": "2026-10-20 09:00", "message": "@agent please look at #46"},
}


def test_enabled_by_the_start_flag_or_the_override(monkeypatch):
    """RT-6.12 trigger: Claude Code's command line decides; MM_CHANNEL_PUSH
    overrides it either way."""
    monkeypatch.delenv("MM_CHANNEL_PUSH", raising=False)
    monkeypatch.setattr(channel, "started_as_channel", lambda: True)
    assert channel.enabled()
    monkeypatch.setenv("MM_CHANNEL_PUSH", "0")
    assert not channel.enabled()
    monkeypatch.setattr(channel, "started_as_channel", lambda: False)
    monkeypatch.setenv("MM_CHANNEL_PUSH", "1")
    assert channel.enabled()
    monkeypatch.delenv("MM_CHANNEL_PUSH")
    assert not channel.enabled()

    def unreadable():
        raise PermissionError("sandbox")

    monkeypatch.setattr(channel, "started_as_channel", unreadable)
    assert not channel.enabled()


ENTRY = "plugin:mattermost-ai-agent-bus@mattermost-ai-agent-bus"


@pytest.mark.parametrize("argv, on", [
    (["claude", "--continue", "--channels", ENTRY], True),
    (["claude", "--dangerously-load-development-channels", ENTRY, "fix it"], True),
    (["claude", f"--channels={ENTRY}"], True),
    (["claude", "--channels", "plugin:other@x", ENTRY], True),
    (["claude", "--channels", f"plugin:other@x,{ENTRY}"], True),
    (["claude", "--channels", "plugin:other@x", "--model", ENTRY], False),
    (["claude", "--continue"], False),
    (["claude", ENTRY], False),
])
def test_start_flag_names_this_plugin(argv, on):
    assert channel.names_this_plugin(argv) is on


def test_nearest_claude_ancestor_decides():
    """uv, python and shells sit between the server and Claude Code."""
    procs = {10: (["python", "-m", "x"], 9), 9: (["uv", "run"], 8),
             8: (["/opt/homebrew/bin/claude", "--channels", ENTRY], 7),
             7: (["corral", "run", "--", "--channels", ENTRY], 1)}

    def found(pid, procs=procs):
        return channel.started_as_channel(
            pid, argv_of=lambda p: procs[p][0], parent_of=lambda p: procs[p][1])

    assert found(10)
    assert not found(10, procs | {8: (["/opt/homebrew/bin/claude"], 7)})
    assert not found(10, procs | {8: (["zsh"], 1)})


def test_notification_carries_message_and_routing():
    params = channel.notification(MENTION)
    assert params["content"] == "@agent please look at #46"
    meta = params["meta"]
    assert meta == {"kind": "mention", "post_id": "p1", "root_id": "r1", "channel": "agents",
                    "author": "dembaca", "at": "2026-10-20 09:00",
                    "from_trusted_human": "true", "from_agent": "false",
                    "trusted_human": "dembaca"}
    # Claude Code drops attributes whose key is not an identifier, and wants strings.
    assert all(k.isidentifier() and isinstance(v, str) for k, v in meta.items())


def test_reaction_keeps_its_answer():
    item = {"kind": "reaction", "from_trusted_human": False, "ask": "a1", "emoji": "+1",
            "answer": "yes", "post": {"id": "a1:u:+1", "message": ":+1:"}}
    meta = channel.notification(item)["meta"]
    assert meta["from_trusted_human"] == "false"
    assert (meta["ask"], meta["emoji"], meta["answer"]) == ("a1", "+1", "yes")


async def _send(stream, msg):
    await stream.send(SessionMessage(message=msg))


async def _recv(stream):
    with anyio.fail_after(5):
        while True:
            item = await stream.receive()
            if isinstance(item, SessionMessage):
                return item.message.model_dump(by_alias=True, exclude_none=True)


@pytest.mark.anyio
async def test_handshake_era_channel_pushes_events():
    """A 2026-07-28 probe is refused, so the client falls back to initialize —
    the only era Claude Code registers a channel on — and events then arrive
    as notifications/claude/channel."""
    low = MCPServer("t")._lowlevel_server
    opts = low.create_initialization_options(experimental_capabilities={channel.CAPABILITY: {}})
    pusher = channel.Pusher()
    queued = [MENTION]

    async def wait(timeout):
        if queued:
            return [queued.pop()]
        await asyncio.sleep(3600)
        return []

    async with create_client_server_memory_streams() as ((c_read, c_write), (s_read, s_write)):
        async with anyio.create_task_group() as tg:
            async def serve():
                await channel.serve_handshake_only(
                    low, s_read, s_write, lifespan_state={}, init_options=opts,
                    pusher=pusher, on_connected=lambda: pusher.run(wait, check_after=(0.01,)))
            tg.start_soon(serve)

            await _send(c_write, JSONRPCRequest(jsonrpc="2.0", id=1, method="server/discover", params={
                "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}}))
            refused = await _recv(c_read)
            assert refused["error"]["code"] == INVALID_REQUEST

            await _send(c_write, JSONRPCRequest(jsonrpc="2.0", id=2, method="initialize", params={
                "protocolVersion": HANDSHAKE_PROTOCOL_VERSIONS[-1], "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": "test"}}))
            init = (await _recv(c_read))["result"]
            assert init["protocolVersion"] in HANDSHAKE_PROTOCOL_VERSIONS
            assert init["capabilities"]["experimental"] == {channel.CAPABILITY: {}}

            # Nothing is pushed before the client says it is ready; then the
            # check, and the inbox only once the agent confirmed it.
            await _send(c_write, JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized"))
            pushed = await _recv(c_read)
            assert pushed["method"] == channel.METHOD
            assert pushed["params"] == channel.check(pusher.code)
            assert queued == [MENTION]
            assert pusher.confirm(pusher.code)
            pushed = await _recv(c_read)
            assert pushed["params"] == channel.notification(MENTION)
            tg.cancel_scope.cancel()


class Conn:
    def __init__(self):
        self.initialized = anyio.Event()
        self.initialized.set()
        self.sent = []

    async def notify(self, method, params):
        self.sent.append(params)


def _pusher(**kw):
    pusher = channel.Pusher(**kw)
    pusher.connection = Conn()
    return pusher


@pytest.mark.anyio
async def test_pusher_waits_for_the_bus():
    """No bus yet (before session_start, or a dropped socket): retry, don't die."""
    pusher = _pusher()
    pusher.confirm(pusher.code)
    calls = prepared = 0

    async def prepare():
        nonlocal prepared
        prepared += 1
        if prepared == 1:
            raise RuntimeError("no bot token")

    async def wait(timeout):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("no bot token")
        if calls == 2:
            return [MENTION]
        await asyncio.sleep(3600)

    with anyio.move_on_after(0.5):
        await pusher.run(wait, prepare=prepare, retry_sec=0.01)
    assert calls >= 2
    assert pusher.connection.sent == [channel.notification(MENTION)]


@pytest.mark.anyio
async def test_unconfirmed_channel_reads_nothing():
    """RT-6.12 step 1: a session that never received the check never confirms
    it, so the inbox stays with wait_for_events and the listener — nothing is
    marked read unseen."""
    pusher = _pusher()
    calls = 0

    async def wait(timeout):
        nonlocal calls
        calls += 1
        return [MENTION]

    with anyio.move_on_after(0.2):
        await pusher.run(wait, check_after=(0.01,), check_every=10)
    assert calls == 0
    assert pusher.connection.sent == [channel.check(pusher.code)]
    assert not pusher.confirm("deadbeef") and not pusher.confirmed.is_set()


@pytest.mark.anyio
async def test_check_comes_late_and_again_until_confirmed():
    """RT-6.12 step 2: Claude Code drops a push sent before it registered the
    channel, so the check waits a moment and repeats with growing gaps; once
    confirmed it stops and the inbox is read."""
    pusher = _pusher()
    reads = 0

    async def wait(timeout):
        nonlocal reads
        reads += 1
        await asyncio.sleep(3600)

    async def confirm_later():
        await asyncio.sleep(0.25)
        pusher.confirm(pusher.code)

    with anyio.move_on_after(0.5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(confirm_later)
            await pusher.run(wait, check_after=(0.05, 0.1), check_every=1)
    checks = [p for p in pusher.connection.sent if p["meta"]["kind"] == "channel_check"]
    assert len(checks) == 2  # at 0.05 s and 0.15 s; confirmed before the next
    assert reads == 1


def _post(message, kind="watched_channel", trusted=False, root=""):
    return {"kind": kind, "from_trusted_human": trusted,
            "post": {"id": "p", "root_id": root, "author": "agent-x", "at": "09:00",
                     "message": message}}


def test_a_post_for_all_agents_says_so_in_the_push():
    """RT-6.11 step 3: the tag tells the agent the post was for everyone."""
    meta = channel.notification(_post("main is red") | {"to": "all"})["meta"]
    assert meta["to"] == "all"
    assert "to" not in channel.notification(_post("main is red"))["meta"]


def test_wake_rules_match_the_listener():
    """RT-6.12 step 2: the listener's rules (RT-6.2) decide what wakes at once."""
    def wakes(item, coordinator=False, in_my_thread=False):
        return channel.wakes(item, coordinator=coordinator, in_my_thread=in_my_thread)

    assert wakes(_post("hi", kind="mention"))
    assert wakes(_post("hi", kind="dm"))
    assert wakes(_post(":+1:", kind="reaction"))
    # A trusted human's plain post is for the coordinator to route (RT-6.6).
    assert not wakes(_post("deploy now?", trusted=True))
    assert wakes(_post("deploy now?", trusted=True), coordinator=True)
    assert wakes(_post("Heads up @channel: main is red."))
    # What agents write instead (RT-6.11): props.a2a.to = "all", no @ in the text.
    assert wakes(_post("main is red") | {"to": "all"})
    assert not wakes(_post("main is red") | {"to": "all", "gated": "from x"})
    assert wakes(_post("done", root="r"), in_my_thread=True)
    assert wakes(_post("IDLE done x"), coordinator=True)
    assert not wakes(_post("IDLE done x"))
    assert not wakes(_post("mail me at a@all.example"))


@pytest.mark.anyio
async def test_other_posts_come_as_one_digest(monkeypatch):
    """RT-6.12 step 2: posts not for the agent are held, then pushed together
    once the oldest is MM_LISTEN_DIGEST_SEC old: one wake-up, not one each.
    System posts ("… joined the channel") are dropped: their author is the
    human who added the bot, so a pusher that wakes for that human would wake."""
    pusher = _pusher(digest_sec=0.3)
    pusher.confirm(pusher.code)
    joined = _post("agent-y joined the channel.", trusted=True)
    joined["post"]["type"] = "system_join_channel"
    batches = [[_post("IDLE a"), joined], [_post("IDLE b", root="r1")], [MENTION]]
    timeouts = []

    async def wait(timeout):
        timeouts.append(timeout)
        if batches:
            return batches.pop(0)
        await asyncio.sleep(timeout)
        return []

    async def addressed(item):
        return item["kind"] == "mention" or item["from_trusted_human"]

    with anyio.move_on_after(1.0):
        await pusher.run(wait, addressed=addressed, wait_cap=5)
    sent = pusher.connection.sent
    assert sent[0] == channel.notification(MENTION)  # at once, while two are held
    assert len(sent) == 2 and sent[1]["meta"] == {"kind": "digest", "count": "2"}
    assert "IDLE a" in sent[1]["content"] and "thread r1" in sent[1]["content"]
    # While posts are held the wait ends when the digest is due, not at the cap.
    assert timeouts[0] == 5 and all(t <= 0.3 for t in timeouts[1:4])


@pytest.mark.anyio
async def test_channel_ok_tells_the_hooks(monkeypatch, tmp_path):
    """RT-6.12 step 1: only the pushed code confirms; then channel-<uid>.env
    tells the Stop hook to skip the listen window, until the server ends."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    pusher = channel.Pusher()
    monkeypatch.setattr(server, "_pusher", pusher)
    monkeypatch.setattr(server, "_confirmed", None)
    monkeypatch.setattr(server, "_get_client", lambda: None)

    async def my_id(_client):
        return "u" * 26

    monkeypatch.setattr(server, "_my_id", my_id)
    f = channel.confirmed_file("u" * 26)
    assert '"ok":false' in (await server.channel_ok("nope")).replace(" ", "")
    assert not f.exists()
    assert '"ok":true' in (await server.channel_ok(pusher.code)).replace(" ", "")
    assert pusher.confirmed.is_set() and f.read_text().startswith("CONFIRMED=")
    monkeypatch.setattr(server, "_client", None)
    server._cleanup_on_exit()
    assert not f.exists()


@pytest.mark.anyio
async def test_reply_in_own_thread_wakes(monkeypatch, tmp_path):
    """RT-6.12 step 2: a reply in a thread this bot posted in is for it — also
    a thread its Stop hook signed off in, so the thread itself is asked."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("MM_BOT_USER_ID", "u" * 26)
    monkeypatch.setattr(server, "_my_threads", set())

    class Client:
        async def thread(self, root):
            return {"posts": [{"user_id": "u" * 26 if root == "mine" else "o" * 26}]}

    monkeypatch.setattr(server, "_get_client", lambda: Client())
    assert await server._push_addressed(_post("thanks", root="mine"))
    assert not await server._push_addressed(_post("thanks", root="theirs"))
    assert not await server._push_addressed(_post("IDLE done"))


def test_reads_real_processes():
    """The lookups work on this OS (sysctl/libproc on macOS, /proc on Linux)."""
    import os

    assert channel._parent(os.getpid()) == os.getppid()
    assert channel._argv(os.getpid())

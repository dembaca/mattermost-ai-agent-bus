"""RT-6.13: Claude Code permission prompts relayed to the responsible human."""

import asyncio
import json

import anyio
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp_types import JSONRPCNotification, JSONRPCRequest
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from mattermost_ai_agent_bus import asks, channel, permission, server

BOT = "b" * 26
HUMAN = "h" * 26
OTHER = "o" * 26
PROMPT = "p" * 26
DM = "d" * 26
REQUEST = {"request_id": "abcde", "tool_name": "Bash",
           "description": "Comment on PROJ-1", "input_preview": '{"command": "jira comment PROJ-1"}'}


@pytest.mark.parametrize("message,verdict", [
    ("yes abcde", ("allow", "abcde")),
    ("  Y ABCDE ", ("allow", "abcde")),
    ("no abcde", ("deny", "abcde")),
    ("n abcde", ("deny", "abcde")),
    ("yes", None),
    ("yes abcdl", None),  # never an l in Claude Code's ids
    ("approve it", None),
    ("> yes abcde", None),  # a quote is not the human's own answer
    ("yes abcde please", None),
])
def test_reply_format(message, verdict):
    assert permission.parse(message) == verdict


def test_prompt_shows_untrusted_text_as_code():
    """RT-6.13 step 2: description and preview come from the model's tool call;
    no backtick run inside them can close the block."""
    text = permission.prompt(REQUEST | {"tool_name": "Bash**`x", "input_preview": "a ```` b"})
    assert "`Bash**x`" not in text and "`Bashx`" in text
    assert "`````\na ```` b\n`````" in text
    assert "yes abcde" in text and "no abcde" in text
    long = permission.prompt(REQUEST | {"input_preview": "x" * 20000})
    assert len(long) < 13000 and "⋯ cut" in long


def _shown(tool, args):
    return json.loads(permission.preview(tool, json.dumps(args)))


def test_bash_command_goes_in_full():
    """RT-6.13 step 2: no decision without the command, however long; Claude
    Code's masking of secrets stays as it came."""
    cmd = "curl -H 'Authorization: [REDACTED]' https://x.example/" + "a/" * 400
    assert _shown("Bash", {"command": cmd, "description": "fetch"}) == {
        "command": cmd, "description": "fetch"}
    assert permission.preview("Bash", "ls -la") == "ls -la"  # not JSON: as it came


def test_file_content_stays_in_the_terminal():
    """RT-6.13 step 2: tools that carry file content show the path and sizes,
    never the content, however short."""
    assert _shown("Write", {"file_path": "/r/a.py", "content": "API=1\nB=2\n"}) == {
        "file_path": "/r/a.py", "content": "‹10 chars, 2 lines›"}
    assert _shown("Edit", {"file_path": "/r/a.py", "old_string": "x", "replace_all": True,
                           "new_string": "ab ⋯ 4000 code points elided ⋯ cd"}) == {
        "file_path": "/r/a.py", "old_string": "‹1 chars›", "replace_all": True,
        "new_string": "‹4004 chars›"}
    assert _shown("MultiEdit", {"file_path": "/r/a.py", "edits": [
        {"old_string": "a", "new_string": "bb"}]})["edits"] == [
        {"old_string": "‹1 chars›", "new_string": "‹2 chars›"}]
    assert _shown("NotebookEdit", {"notebook_path": "/n.ipynb", "cell_type": "code",
                                   "new_source": "x = 1"}) == {
        "notebook_path": "/n.ipynb", "cell_type": "code", "new_source": "‹5 chars›"}


def test_other_tools_show_short_values_and_size_long_ones():
    """RT-6.13 step 2: an MCP tool's short arguments are what the human decides
    on; a long or multi-line string may be a file's content."""
    assert _shown("mcp__jira__comment", {"issue": "PROJ-1", "body": "Done.",
                                         "text": "x" * 201, "note": "a\nb"}) == {
        "issue": "PROJ-1", "body": "Done.", "text": "‹201 chars›", "note": "‹3 chars, 2 lines›"}


def test_a_preview_cut_inside_its_structure_shows_no_content():
    """RT-6.13 step 2: Claude Code cuts each top-level field, so an array can
    arrive unparseable: only Bash shows it then, or a short one."""
    cut = '{"file_path": "/r/a.py", "edits": [{"old_string": "k ⋯ 9000 code points elided ⋯'
    shown = permission.preview("MultiEdit", cut)
    assert shown.startswith("file_path: /r/a.py ‹input not shown:") and "old_string" not in shown
    assert "‹input not shown" in permission.preview("mcp__x__y", '{"a": [1, ⋯ 900 code points elided ⋯')
    assert permission.preview("mcp__x__y", "short, not JSON") == "short, not JSON"


def test_relay_only_with_someone_to_answer(monkeypatch):
    """RT-6.13 step 1: on by default — off only without a trusted human."""
    for v in ("MM_TRUSTED_HUMAN_USER_IDS", "MM_TRUSTED_HUMANS", "MM_OWNER_USER_ID", "MM_OWNER"):
        monkeypatch.delenv(v, raising=False)
    assert not permission.relayed()
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "dembaca")
    assert permission.relayed()


async def _send(stream, msg):
    await stream.send(SessionMessage(message=msg))


async def _recv(stream):
    with anyio.fail_after(5):
        while True:
            item = await stream.receive()
            if isinstance(item, SessionMessage):
                return item.message.model_dump(by_alias=True, exclude_none=True)


@pytest.mark.anyio
async def test_request_reaches_the_server_and_the_verdict_goes_back():
    """RT-6.13 steps 1 and 4 on the wire: the capability is declared, the
    request goes to on_permission, a verdict goes out as its notification."""
    low = MCPServer("t")._lowlevel_server
    opts = low.create_initialization_options(experimental_capabilities={
        channel.CAPABILITY: {}, permission.CAPABILITY: {}})
    pusher = channel.Pusher()
    got = []

    async def on_permission(params):
        got.append(params)

    async with create_client_server_memory_streams() as ((c_read, c_write), (s_read, s_write)):
        async with anyio.create_task_group() as tg:
            async def serve():
                await channel.serve_handshake_only(
                    low, s_read, s_write, lifespan_state={}, init_options=opts, pusher=pusher,
                    on_connected=lambda: asyncio.sleep(3600), on_permission=on_permission)
            tg.start_soon(serve)
            await _send(c_write, JSONRPCRequest(jsonrpc="2.0", id=1, method="initialize", params={
                "protocolVersion": HANDSHAKE_PROTOCOL_VERSIONS[-1], "capabilities": {},
                "clientInfo": {"name": "claude-code", "version": "test"}}))
            init = (await _recv(c_read))["result"]
            assert permission.CAPABILITY in init["capabilities"]["experimental"]
            await _send(c_write, JSONRPCNotification(jsonrpc="2.0", method="notifications/initialized"))
            await _send(c_write, JSONRPCNotification(
                jsonrpc="2.0", method=permission.REQUEST, params=REQUEST))
            with anyio.fail_after(5):
                while not got:
                    await asyncio.sleep(0.01)
            assert got == [REQUEST]
            await pusher.verdict("abcde", "allow")
            sent = await _recv(c_read)
            assert sent["method"] == permission.VERDICT
            assert sent["params"] == {"request_id": "abcde", "behavior": "allow"}
            tg.cancel_scope.cancel()


class _Client:
    def __init__(self, trusted=None):
        self.posted = []
        self.trusted = {HUMAN: "dembaca"} if trusted is None else trusted

    async def get_me(self):
        return {"id": BOT, "username": "agent-x"}

    async def trusted_humans(self):
        return self.trusted

    async def direct_channel_with(self, user_id):
        return DM if user_id == HUMAN else "nope"

    async def post(self, message, channel_id=None, root_id=None, props=None):
        self.posted.append({"message": message, "channel_id": channel_id, "root_id": root_id})
        return {"id": PROMPT}


class _Conn:
    def __init__(self):
        self.sent = []

    async def notify(self, method, params):
        self.sent.append((method, params))


@pytest.fixture
def relay(monkeypatch):
    monkeypatch.delenv("MM_RESPONSIBLE_HUMAN", raising=False)
    c = _Client()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(server, "_relay", permission.Relay())
    pusher = channel.Pusher()
    pusher.connection = _Conn()
    monkeypatch.setattr(server, "_pusher", pusher)
    return c, pusher.connection


def _item(message, *, kind="dm", trusted=True, **extra):
    item = {"kind": kind, "from_trusted_human": trusted,
            "post": {"id": "r" * 26, "author": "dembaca" if trusted else "agent-y",
                     "message": message}}
    if trusted:
        item["trusted_human"] = "dembaca"
    return item | extra


@pytest.mark.anyio
async def test_prompt_goes_by_dm_to_the_responsible_human(relay):
    """RT-6.13 step 2: a DM, not the work channel; the post is an open ask so
    a reaction on it reaches the inbox, without blocking ask_human."""
    c, _ = relay
    await server._permission_request(REQUEST)
    assert c.posted[0]["channel_id"] == DM and "abcde" in c.posted[0]["message"]
    assert server._relay.for_post(PROMPT) == "abcde"
    assert asks.is_open(BOT, PROMPT) and not asks.open_to(BOT, "dembaca")
    await server._permission_request(REQUEST | {"request_id": "fghij", "tool_name": "Write",
                                                "input_preview": '{"file_path": "/r/.env", "content": "PW=x"}'})
    assert "/r/.env" in c.posted[1]["message"] and "PW=x" not in c.posted[1]["message"]


@pytest.mark.anyio
async def test_no_trusted_responsible_human_keeps_the_terminal(relay, monkeypatch):
    """RT-6.13 error handling: only a trusted human's verdict counts, so a
    responsible human who is not trusted gets nothing."""
    c, _ = relay
    monkeypatch.setenv("MM_RESPONSIBLE_HUMAN", "someone-else")
    await server._permission_request(REQUEST)
    assert c.posted == [] and server._relay.pending == {}


@pytest.mark.anyio
async def test_trusted_yes_allows_and_no_denies(relay):
    """RT-6.13 steps 3-4: the verdict goes to Claude Code, the agent never sees it."""
    c, conn = relay
    await server._permission_request(REQUEST)
    assert await server._push_intercept(_item("yes abcde"))
    assert conn.sent == [(permission.VERDICT, {"request_id": "abcde", "behavior": "allow"})]
    assert "allowed by dembaca" in c.posted[-1]["message"] and c.posted[-1]["root_id"] == PROMPT
    assert not asks.is_open(BOT, PROMPT)
    await server._permission_request(REQUEST | {"request_id": "fghij"})
    assert await server._push_intercept(_item("No FGHIJ", kind="watched_channel"))
    assert conn.sent[-1] == (permission.VERDICT, {"request_id": "fghij", "behavior": "deny"})


@pytest.mark.anyio
async def test_reaction_on_the_prompt_is_a_verdict(relay):
    """RT-6.13 step 3: 👍/👎 on the prompt post, as for an ask (RT-6.11)."""
    c, conn = relay
    await server._permission_request(REQUEST)
    remark = _item(":eyes:", kind="reaction", ask=PROMPT, emoji="eyes", answer=None)
    assert await server._push_intercept(remark) and conn.sent == []
    other = _item(":+1:", kind="reaction", ask="x" * 26, emoji="+1", answer="yes")
    assert not await server._push_intercept(other)
    assert await server._push_intercept(
        _item(":-1:", kind="reaction", ask=PROMPT, emoji="-1", answer="no"))
    assert conn.sent == [(permission.VERDICT, {"request_id": "abcde", "behavior": "deny"})]


@pytest.mark.anyio
async def test_untrusted_verdict_is_only_data(relay):
    """RT-6.13 error handling: another agent or person answering is a post like
    any other; Claude Code gets nothing."""
    _, conn = relay
    await server._permission_request(REQUEST)
    assert not await server._push_intercept(_item("yes abcde", trusted=False))
    assert not await server._push_intercept(
        _item(":+1:", kind="reaction", trusted=False, ask=PROMPT, emoji="+1", answer="yes"))
    assert conn.sent == [] and "abcde" in server._relay.pending


@pytest.mark.anyio
async def test_unknown_id(relay):
    """RT-6.13 error handling: in the DM a short reply; in a channel it may be
    another agent's prompt, so it passes on untouched."""
    c, conn = relay
    assert await server._push_intercept(_item("yes zzzzz"))
    assert "No open permission prompt `zzzzz`" in c.posted[-1]["message"]
    n = len(c.posted)
    assert not await server._push_intercept(_item("yes zzzzz", kind="watched_channel"))
    assert len(c.posted) == n and conn.sent == []


@pytest.mark.anyio
async def test_a_verdict_after_24_hours_counts_nothing(relay):
    """RT-6.13 error handling: a prompt is open as long as a yes on an ask
    approves (RT-6.11); a later verdict goes nowhere, and a 👍 on the old prompt
    reaches the agent like an answer to an ask, marked expired there."""
    c, conn = relay
    assert permission.MAX_AGE_SEC == asks.APPROVAL_SEC
    await server._permission_request(REQUEST)
    server._relay.pending["abcde"].at -= permission.MAX_AGE_SEC + 1
    assert await server._push_intercept(_item("yes abcde"))
    assert "more than 24 h old" in c.posted[-1]["message"] and conn.sent == []
    assert not await server._push_intercept(
        _item(":+1:", kind="reaction", ask=PROMPT, emoji="+1", answer="yes"))


@pytest.mark.anyio
async def test_pusher_hands_intercepted_events_to_nobody():
    """RT-6.13 step 4: an intercepted event neither wakes the agent nor waits for the digest."""
    pusher = channel.Pusher()
    conn = _Conn()
    conn.initialized = anyio.Event()
    conn.initialized.set()
    pusher.connection = conn
    pusher.confirm(pusher.code)
    queued = [_item("hello"), _item("yes abcde")]

    async def wait(timeout):
        if queued:
            return [queued.pop()]
        await asyncio.sleep(3600)

    async def intercept(item):
        return item["post"]["message"] == "yes abcde"

    with anyio.move_on_after(0.3):
        await pusher.run(wait, intercept=intercept)
    assert [p["content"] for _, p in conn.sent] == ["hello"] and pusher.held == []


@pytest.mark.anyio
async def test_the_dm_holds_nothing_unread_after_prompt_and_verdict(relay, monkeypatch):
    """RT-6.13, RT-6.2 step 1: the bot's own prompt and verdict posts count as
    unread DMs for it, and the human's intercepted answer reaches no inbox, so
    nothing would collect them: the Stop hook drained an empty inbox every turn.
    Each of these posts marks the DM read."""
    c, _ = relay
    viewed = []

    async def post(message, channel_id=None, root_id=None, props=None):
        c.posted.append({"message": message, "channel_id": channel_id, "root_id": root_id})
        return {"id": PROMPT, "channel_id": DM}

    async def view_channel(cid):
        viewed.append(cid)

    class _Inbox:
        def holds(self, cid):
            return False

        def stop_typing(self):
            pass

    monkeypatch.setattr(c, "post", post, raising=False)
    c.view_channel = view_channel
    monkeypatch.setattr(server, "_inbox", _Inbox())
    await server._permission_request(REQUEST)
    assert viewed == [DM]
    assert await server._push_intercept(_item("yes abcde"))
    assert viewed == [DM, DM]
    assert await server._push_intercept(_item("yes fghij"))  # no such prompt: the note is a post too
    assert viewed == [DM, DM, DM]

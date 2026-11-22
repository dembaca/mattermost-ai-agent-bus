"""RT-6.13: Claude Code permission prompts relayed to the responsible human."""

import asyncio
import json
import os
import stat
import time

import anyio
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp_types import JSONRPCNotification, JSONRPCRequest
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import Me
from mattermost_ai_agent_bus import asks, channel, hookapi, lease, permission, server, session_file, watermark
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

from fakes import FakeEvents

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
        return Me(id=BOT, username="agent-x")

    async def trusted_humans(self):
        return self.trusted

    async def direct_channel_with(self, user_id):
        return DM if user_id == HUMAN else "nope"

    async def post(self, message, channel_id=None, root_id=None, props=None):
        self.posted.append({"message": message, "channel_id": channel_id, "root_id": root_id})
        return post_of({"id": PROMPT})


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
    queued = [_item("hello"), _item("yes abcde")]

    async def answer():  # every connection is confirmed by its own check (RT-6.12 step 2)
        while not conn.sent:
            await asyncio.sleep(0.005)
        pusher.confirm(pusher.code)

    async def wait(timeout):
        if queued:
            return [queued.pop()]
        await asyncio.sleep(3600)

    async def intercept(item):
        return item["post"]["message"] == "yes abcde"

    with anyio.move_on_after(0.5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(answer)
            await pusher.run(wait, intercept=intercept, check_after=(0.01,))
    pushed = [p["content"] for _, p in conn.sent if p["meta"]["kind"] != "channel_check"]
    assert pushed == ["hello"] and pusher.held == []


@pytest.mark.anyio
async def test_the_dm_holds_nothing_unread_after_prompt_and_verdict(relay, monkeypatch, tmp_path):
    """RT-6.13, RT-6.2 step 1: the bot's own prompt and verdict posts, and the
    human's intercepted answer reaches no inbox, so nothing would collect them:
    the Stop hook drained an empty inbox every turn. Each of these posts is
    handled: the read watermark passes it, and Mattermost's display is cleared."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    c, _ = relay
    viewed = []
    made = []

    async def post(message, channel_id=None, root_id=None, props=None):
        c.posted.append({"message": message, "channel_id": channel_id, "root_id": root_id})
        made.append(post_of({"id": PROMPT, "channel_id": DM, "create_at": 1000 + len(made)}))
        return made[-1]

    async def view_channel(cid):
        viewed.append(cid)

    inbox = WebSocketInbox(client=c, source=FakeEvents())  # connected
    inbox._bot_user_id = "b" * 26
    monkeypatch.setattr(c, "post", post, raising=False)
    c.view_channel = view_channel
    monkeypatch.setattr(server, "_inbox", inbox)
    await server._permission_request(REQUEST)
    assert viewed == [DM]
    assert await server._push_intercept(_item("yes abcde"))
    assert viewed == [DM, DM]
    assert await server._push_intercept(_item("yes fghij"))  # no such prompt: the note is a post too
    assert viewed == [DM, DM, DM]
    await server._permission_request(REQUEST)
    await server.permissions_obsolete(c)  # the turn's end note too (step 5)
    assert viewed == [DM, DM, DM, DM, DM]
    assert watermark.at("b" * 26, DM) == made[-1].create_at


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["idle", "listening"])
async def test_the_turns_end_closes_prompts_the_terminal_answered(relay, monkeypatch, state):
    """RT-6.13 step 5: Claude Code says nothing when the terminal answers, but
    no turn ends with a dialog open. The Stop hook's idle or listening closes
    every prompt still open, with a note in the DM; a late yes goes nowhere."""
    c, conn = relay

    async def set_status(client, state, label=""):
        pass

    monkeypatch.setattr(lease, "set_status", set_status)
    end = {"state": state} | ({"until": int(time.time()) + 600} if state == "listening" else {})
    await server._permission_request(REQUEST)
    assert await hookapi.presence(c, {}, {"state": "working"}) == (200, {"ok": True})
    assert "abcde" in server._relay.pending
    assert await hookapi.presence(c, {}, end) == (200, {"ok": True})
    assert "Answered in the terminal" in c.posted[-1]["message"]
    assert c.posted[-1]["root_id"] == PROMPT
    assert server._relay.pending == {} and not asks.is_open(BOT, PROMPT)
    n = len(c.posted)
    await hookapi.presence(c, {}, end)
    assert len(c.posted) == n  # nothing open: no post
    assert await server._push_intercept(_item("yes abcde"))
    assert "No open permission prompt `abcde`" in c.posted[-1]["message"] and conn.sent == []


# --- the relay log (RT-6.13 step 6) ------------------------------------------------

SECRET_REQUEST = REQUEST | {
    "description": "DESCRIPTION-TEXT rm the build",
    "input_preview": '{"command": "curl -H TOKEN-IN-COMMAND https://internal.example/PATH-IN-COMMAND"}',
}


@pytest.fixture
def logged(relay, monkeypatch):
    monkeypatch.setenv("MM_BOT_USER_ID", BOT)
    return relay


def _lines():
    path = permission.log_path()
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.anyio
async def test_each_step_of_the_relay_writes_one_line(logged, monkeypatch):
    """RT-6.13 step 6: request received, DM posted, verdict sent, closed at the
    turn's end, a yes for an id that is not open, not relayed, posting failed:
    one JSON line each, with time, request id, tool, step and reason."""
    c, _ = logged

    async def set_status(client, state, label=""):
        pass

    monkeypatch.setattr(lease, "set_status", set_status)
    await server._permission_request(REQUEST)
    assert await server._push_intercept(_item("yes abcde"))
    await server._permission_request(REQUEST | {"request_id": "fghij", "tool_name": "Write"})
    assert await hookapi.presence(c, {}, {"state": "idle"}) == (200, {"ok": True})
    assert await server._push_intercept(_item("no fghij"))  # closed: not open any more

    monkeypatch.setenv("MM_RESPONSIBLE_HUMAN", "someone-else")
    await server._permission_request(REQUEST | {"request_id": "klmno"})
    monkeypatch.delenv("MM_RESPONSIBLE_HUMAN")

    async def failing(*args, **kwargs):
        raise ConnectionError("bus down")

    monkeypatch.setattr(c, "post", failing)
    await server._permission_request(REQUEST | {"request_id": "pqrst"})

    lines = _lines()
    assert [(r["id"], r["step"]) for r in lines] == [
        ("abcde", "request received"), ("abcde", "dm posted"), ("abcde", "verdict sent"),
        ("fghij", "request received"), ("fghij", "dm posted"), ("fghij", "closed at turn end"),
        ("fghij", "unknown id"),
        ("klmno", "request received"), ("klmno", "not relayed"),
        ("pqrst", "request received"), ("pqrst", "posting failed"),
    ]
    assert all(set(r) == {"t", "id", "tool", "step", "reason"} for r in lines)
    assert all(r["t"].endswith("Z") for r in lines)
    by_step = {r["step"]: r for r in lines}
    assert by_step["verdict sent"]["reason"] == "allow by dembaca" and by_step["verdict sent"]["tool"] == "Bash"
    assert by_step["not relayed"]["reason"] == "responsible human not trusted"
    assert by_step["posting failed"]["reason"] == "ConnectionError"  # the class, never the message
    assert "bus down" not in permission.log_path().read_text()
    assert by_step["closed at turn end"]["reason"] == "answered in the terminal"
    assert stat.S_IMODE(os.stat(permission.log_path()).st_mode) == 0o600


@pytest.mark.anyio
async def test_the_log_never_holds_the_description_or_the_input(logged):
    """RT-6.13 step 6: no description, no preview, no command, path or content
    in the log, whichever step wrote the line."""
    await server._permission_request(SECRET_REQUEST)
    assert await server._push_intercept(_item("yes abcde"))
    await server._permission_request(SECRET_REQUEST | {"request_id": "fghij", "tool_name": "Write",
                                                       "input_preview": '{"file_path": "/r/.env", "content": "PW=x"}'})
    text = permission.log_path().read_text()
    assert text.count("\n") == 5
    for secret in ("DESCRIPTION-TEXT", "TOKEN-IN-COMMAND", "internal.example", "PATH-IN-COMMAND",
                   "curl", "/r/.env", "PW=x", "jira"):
        assert secret not in text


def test_a_tool_name_cannot_smuggle_text_into_the_log(logged):
    """RT-6.13 step 6: the fields come from Claude Code's request; they are cut
    to a short plain word, never a second line."""
    permission.note("request received", "ab\ncde" + "x" * 50, "Bash\n{\"step\": 1} " + "y" * 200)
    (line,) = _lines()
    assert len(line["id"]) <= 16 and len(line["tool"]) <= 64
    assert "\n" not in json.dumps(line) and line["step"] == "request received"


def test_the_log_keeps_only_its_newest_lines(logged):
    """RT-6.13 step 6: never more than 500 lines; past that the oldest go and
    the newest 400 stay, so a full log is not rewritten by every note."""
    for n in range(permission.LOG_LINES):
        permission.note("request received", f"id{n}", "Bash")
    assert len(_lines()) == permission.LOG_LINES
    permission.note("request received", f"id{permission.LOG_LINES}", "Bash")
    lines = _lines()
    assert len(lines) == permission.LOG_KEEP
    assert lines[0]["id"] == f"id{permission.LOG_LINES - permission.LOG_KEEP + 1}"
    for n in range(permission.LOG_LINES + 1, permission.LOG_LINES + 40):
        permission.note("request received", f"id{n}", "Bash")
    lines = _lines()
    assert len(lines) == permission.LOG_KEEP + 39 and lines[-1]["id"] == f"id{permission.LOG_LINES + 39}"


@pytest.mark.anyio
async def test_a_log_that_cannot_be_written_does_not_break_the_relay(logged, monkeypatch):
    """RT-6.13 step 6: a failed write is ignored; the prompt still goes by DM
    and the verdict still reaches Claude Code."""
    c, conn = logged

    def refuse(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(session_file, "append_private", refuse)
    permission.note("request received", "abcde", "Bash")  # no exception
    await server._permission_request(REQUEST)
    assert c.posted[0]["channel_id"] == DM and server._relay.for_post(PROMPT) == "abcde"
    assert await server._push_intercept(_item("yes abcde"))
    assert conn.sent == [(permission.VERDICT, {"request_id": "abcde", "behavior": "allow"})]
    assert not permission.log_path().exists()


def test_no_bot_no_log(monkeypatch):
    """RT-6.13 step 6: without a bot id there is no file to name."""
    monkeypatch.delenv("MM_BOT_USER_ID", raising=False)
    monkeypatch.setattr("mattermost_ai_agent_bus.stats._uid", "")
    assert permission.log_path() is None
    permission.note("request received", "abcde", "Bash")  # nothing to write, nothing raised

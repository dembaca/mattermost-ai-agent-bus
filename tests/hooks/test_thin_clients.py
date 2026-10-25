"""RT-6.17 the shell scripts an agent runs are clients of its session's MCP
server (docs/arc42/06-runtime-view.md): mm-agent-post.sh and mm-agent-poll.sh
through the hook API, and how a shell finds its own server in Claude Code,
Cursor and OpenCode, never another workspace's."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import threading
from pathlib import Path

import pytest
from conftest import CHANNEL, PROJECT, ROOT, TEAM
from mattermost_ai_agent_bus import hookapi, session_file
from mattermost_ai_agent_bus.client import MattermostClient

POST = "bin/mm-agent-post.sh"
POLL = "bin/mm-agent-poll.sh"


def posted(mm) -> dict:
    """The body of the last post the server wrote."""
    return [b for m, p, b in mm.writes if (m, p) == ("POST", "/api/v4/posts")][-1]


# --- post and poll end to end ------------------------------------------------

def test_post_goes_through_the_servers_post_message(mm, run):
    """RT-6.17 step 3, RT-6.11, RT-6.14: the server posts it with the tool's
    code: a human's @-mention as a plain name, @channel as a plain word, the
    kind first and in the props, to=all in the props; its answer is printed."""
    r = run(POST, "--kind", "RESULT", "--to", "all", "@dembaca @channel built it")
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["message"] == "RESULT · dembaca channel built it"
    assert out["unmentioned"] == ["dembaca", "channel"] and "ask_human" in out["hint"]
    body = posted(mm)
    assert body["channel_id"] == CHANNEL
    assert body["props"]["a2a"]["to"] == "all" and body["props"]["a2a"]["kind"] == "result"


def test_reply_and_the_old_form(mm, run):
    """RT-6.17: --root (or the 0.36 form CHANNEL MESSAGE ROOT) is a reply,
    through reply_in_thread's code; a message from stdin with "-"."""
    root = "r" * 26
    assert run(POST, "--root", root, "-", stdin="ACK on it\n").code == 0
    assert posted(mm)["root_id"] == root and posted(mm)["message"] == "ACK on it"
    assert run(POST, PROJECT, "PROGRESS · half way", root).code == 0
    assert {"channel_id": PROJECT, "root_id": root}.items() <= posted(mm).items()


@pytest.mark.parametrize("message", ["- done: built it", "-1 test left", "--- end", "--kindly"])
def test_a_message_may_start_with_a_dash(mm, run, message):
    """RT-6.17: only the known options are options; a message starting with
    "-" is posted as it is, in the new form and the 0.36 form."""
    r = run(POST, message)
    assert r.code == 0, r.stderr
    assert posted(mm)["message"] == message
    assert run(POST, PROJECT, message).code == 0
    assert {"channel_id": PROJECT, "message": message}.items() <= posted(mm).items()


def test_option_names_as_messages_and_a_lone_dash(mm, run):
    """RT-6.17: "--" ends the options; in the 0.36 form CHANNEL MESSAGE [ROOT]
    nothing after the channel id is an option, and "-" is the text, not stdin."""
    assert run(POST, "--", "--help").code == 0 and posted(mm)["message"] == "--help"
    assert run(POST, PROJECT, "--root", stdin="not read\n").code == 0
    assert {"channel_id": PROJECT, "message": "--root"}.items() <= posted(mm).items()
    assert run(POST, PROJECT, "-", stdin="not read\n").code == 0 and posted(mm)["message"] == "-"
    assert run(POST, "-", stdin="from stdin\n").code == 0 and posted(mm)["message"] == "from stdin"
    n = len(mm.writes)
    assert run(POST, "--chanel", "x", "hello").code == 2  # a typo is no message
    assert len(mm.writes) == n


def test_a_refused_post_says_why(mm, run):
    """RT-6.14: the refusal of post_message, on stderr, exit 1; nothing posted."""
    r = run(POST, "--kind", "SHOUT", "hello")
    assert r.code == 1 and '"refused"' in r.stderr and '"kinds"' in r.stderr
    r = run(POST, "--to", "bob", "--root", "r" * 26, "hi")
    assert r.code == 1 and "a reply stays in its thread" in r.stderr
    assert not [w for w in mm.writes if w[1] == "/api/v4/posts"]


def test_poll_prints_new_posts_oldest_first(mm, run):
    """RT-6.17: mm-agent-poll.sh reads GET /v1/posts: JSON lines, oldest first,
    with what the server knows of each post (to, intent, fyi, gated)."""
    mm.posts = [{"id": "p2", "user_id": "o" * 26, "create_at": 3000, "message": "second"},
                {"id": "p1", "user_id": "o" * 26, "create_at": 2000, "message": "first",
                 "props": {"a2a": {"to": "all"}}}]
    r = run(POLL, "--once", "--since-ms", "1000")
    assert r.code == 0, r.stderr
    lines = [json.loads(x) for x in r.stdout.splitlines()]
    assert [x["id"] for x in lines] == ["p1", "p2"] and lines[0]["to"] == "all"
    assert {"user_id", "root_id", "create_at", "intent", "fyi", "gated"} <= set(lines[0])
    assert run(POLL, "--once", "--channel", "proj-x", MM_PROJECT_CHANNEL="proj-x").code == 0
    r = run(POLL, "--once", "--channel", "o" * 26)  # a DM, say: not a channel the bus watches
    assert r.code == 1 and "HTTP 403" in r.stderr
    assert run(POLL, "--team", "x").code == 2


@pytest.mark.parametrize("script,args", [(POST, ("hello",)), (POLL, ("--once",)),
                                         ("bin/mm-agent-status.sh", ("working",))])
def test_without_its_server_a_script_says_what_to_start(mm, run, script, args):
    """RT-6.17 error handling: no token in an agent's shell, so no server of
    its session means no bus: exit 3 and what to do, nothing written."""
    r = run(script, *args, bus=None)
    assert r.code == 3 and "start the MCP server of this session" in r.stderr
    assert mm.writes == []


def test_no_token_in_any_shell_clients_env_or_argv(mm, run, tmp_path):
    """RT-6.17 step 1: the scripts get no token, and the hook API's key goes
    to curl on stdin, never in its argv."""
    shim, log = tmp_path / "shim", tmp_path / "curl.log"
    shim.mkdir()
    real = next(os.path.join(d, "curl") for d in os.environ["PATH"].split(os.pathsep)
                if os.access(os.path.join(d, "curl"), os.X_OK))
    (shim / "curl").write_text(f'#!/bin/sh\n{{ echo "ARGV $*"; env; }} >>"{log}"\nexec "{real}" "$@"\n')
    (shim / "curl").chmod(0o755)
    path = f"{shim}{os.pathsep}{os.environ['PATH']}"
    for script, args in ((POST, ("hello",)), (POLL, ("--once",)),
                         ("bin/mm-agent-status.sh", ("working",))):
        assert run(script, *args, PATH=path, bus="file").code == 0
    seen = log.read_text()
    assert "/v1/post" in seen and "/v1/posts" in seen and "/v1/presence" in seen
    assert "bot-token-for-tests" not in seen and "MM_BOT_TOKEN" not in seen
    assert "Bearer" not in "\n".join(x for x in seen.splitlines() if x.startswith("ARGV"))


# --- which server a shell finds (RT-6.17 step 2) ------------------------------

@contextlib.contextmanager
def server(mm, state: Path, cwd: Path, **env):
    """A session's MCP server (its hook API, with the endpoint file) started
    with this environment and working directory; yields the HookAPI."""
    run_env = {"PATH": os.environ["PATH"], "HOME": str(state.parent), "XDG_STATE_HOME": str(state),
               "MM_CHAT_URL": mm.url, "MM_BOT_TOKEN": "bot-token-for-tests",
               "MM_TEAM_ID": TEAM, "MM_CHANNEL_ID": CHANNEL, **env}
    saved, here = dict(os.environ), os.getcwd()
    os.environ.clear()
    os.environ.update(run_env)
    os.chdir(cwd)
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    client = MattermostClient.from_env()

    async def get():
        return client

    api = hookapi.HookAPI(get)
    try:
        asyncio.run_coroutine_threadsafe(api.start(), loop).result(5)
        yield api
    finally:
        asyncio.run_coroutine_threadsafe(api.close(), loop).result(5)
        asyncio.run_coroutine_threadsafe(client.close(), loop).result(5)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
        loop.close()
        os.chdir(here)
        os.environ.clear()
        os.environ.update(saved)


def found(state: Path, cwd: Path, **env) -> str:
    """The endpoint a shell in this place reaches ('' for none)."""
    base = {"PATH": os.environ["PATH"], "HOME": str(state.parent), "XDG_STATE_HOME": str(state)}
    p = subprocess.run(["bash", "-c", f'source "{ROOT}/bin/mm-agent-lib.sh"; '
                        'mm_bus GET /v1/me && sed -n "s/^MM_BUS_ENDPOINT=//p" "$MM_BUS_FROM"'],
                       env={**base, **env}, cwd=cwd, capture_output=True, text=True, timeout=30)
    return p.stdout.strip()


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """Two workspaces side by side, a plugin directory elsewhere, a state dir."""
    monkeypatch.setattr(session_file, "_roots_dir", None)
    for d in ("one/src/deep", "two", "plugin/mcp", "state"):
        (tmp_path / d).mkdir(parents=True)
    return tmp_path


def test_claude_code_keys_on_the_session_id(mm, ws):
    """RT-6.17 step 2, RT-6.10: server and shell have CLAUDE_CODE_SESSION_ID;
    another session's shell finds nothing, wherever it runs."""
    state = ws / "state"
    with server(mm, state, ws / "one", CLAUDE_CODE_SESSION_ID="s1") as a:
        assert found(state, ws / "plugin", CLAUDE_CODE_SESSION_ID="s1") == a.endpoint
        assert found(state, ws / "one", CLAUDE_CODE_SESSION_ID="s2") == ""


def test_opencode_server_in_the_workspace_shell_in_a_subdirectory(mm, ws):
    """RT-6.17 step 2: OpenCode starts the server in the workspace and the
    shell there (OPENCODE_SESSION_ID, no project variable): the shell walks
    up from its real directory to the closest endpoint file."""
    state = ws / "state"
    with server(mm, state, ws / "one") as a:
        assert found(state, ws / "one" / "src" / "deep", OPENCODE_SESSION_ID="ses_1") == a.endpoint
        assert found(state, ws / "two", OPENCODE_SESSION_ID="ses_2") == ""


def test_cursor_names_the_workspace(mm, ws):
    """RT-6.17 step 2: Cursor's server runs elsewhere with MM_WORKSPACE =
    ${workspaceFolder}; hooks name it in CURSOR_PROJECT_DIR, the shell runs
    in it."""
    state = ws / "state"
    with server(mm, state, ws / "plugin" / "mcp", MM_WORKSPACE=str(ws / "one")) as a:
        assert found(state, ws / "plugin", CURSOR_PROJECT_DIR=str(ws / "one")) == a.endpoint
        assert found(state, ws / "one" / "src") == a.endpoint
        assert found(state, ws / "one", CURSOR_PROJECT_DIR=str(ws / "two")) == ""


def test_the_clients_roots_name_the_workspace(mm, ws, monkeypatch):
    """RT-6.17 step 2: no variable names it, the client's first root does:
    the endpoint file moves from the working directory's name to the root's."""
    state = ws / "state"
    with server(mm, state, ws / "plugin" / "mcp") as a:
        assert found(state, ws / "plugin" / "mcp") == a.endpoint
        with monkeypatch.context() as m:
            m.setenv("XDG_STATE_HOME", str(state))
            m.chdir(ws / "plugin" / "mcp")
            path = session_file.root_path((ws / "one").as_uri())
            assert session_file.set_roots_dir(path)
            a.rekey()
        assert found(state, ws / "one" / "src") == a.endpoint
        assert found(state, ws / "plugin" / "mcp") == ""


def test_two_workspaces_never_cross(mm, ws):
    """RT-6.17 step 2: each shell reaches its own workspace's server only."""
    state = ws / "state"
    with server(mm, state, ws / "one") as a, server(mm, state, ws / "plugin",
                                                   MM_WORKSPACE=str(ws / "two")) as b:
        assert a.endpoint != b.endpoint
        assert found(state, ws / "one" / "src") == a.endpoint
        assert found(state, ws / "two") == b.endpoint
        assert found(state, ws / "plugin") == ""


def test_a_second_server_in_the_workspace_leaves_the_first_ones_file(mm, ws):
    """RT-6.17 error handling: a second MCP server in the same workspace does
    not take the first one's file while that one runs, and says so."""
    state = ws / "state"
    with server(mm, state, ws / "one") as a:
        with server(mm, state, ws / "one") as b:
            assert found(state, ws / "one") == a.endpoint
            assert b.conflict and f"pid {os.getpid()}" in b.warning()
        assert found(state, ws / "one") == a.endpoint
    with server(mm, state, ws / "one") as c:
        assert not c.conflict and found(state, ws / "one") == c.endpoint


def test_the_walk_stops_at_the_repository(mm, ws, run):
    """RT-6.17 step 2: walking up from $PWD stops at the nearest git top. A
    repository nested in another workspace, or a worktree under a checkout,
    never reaches the outer workspace's server: exit 3."""
    state, outer = ws / "state", ws / "one"
    nested, worktree = outer / "src" / "deep", outer / "wt"
    (nested / ".git").mkdir()
    worktree.mkdir()
    (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/wt\n")
    with server(mm, state, outer) as a:
        assert found(state, outer / "src") == a.endpoint
        assert found(state, nested) == "" and found(state, worktree) == ""
        r = run(POST, "hello", bus=None, cwd=str(nested))
        assert r.code == 3 and "start the MCP server of this session" in r.stderr
    assert not [w for w in mm.writes if w[1] == "/api/v4/posts"]


def test_a_symlinked_workspace(mm, ws):
    """RT-6.17 step 2: server and shell key on the real path, whichever name
    of the workspace each one is given."""
    state, link = ws / "state", ws / "link"
    link.symlink_to(ws / "one", target_is_directory=True)
    with server(mm, state, ws / "plugin" / "mcp", MM_WORKSPACE=str(link)) as a:
        assert found(state, ws / "plugin", CURSOR_PROJECT_DIR=str(link)) == a.endpoint
        assert found(state, ws / "plugin", CURSOR_PROJECT_DIR=str(ws / "one")) == a.endpoint
        assert found(state, link / "src" / "deep") == a.endpoint
    with server(mm, state, link) as b:
        assert found(state, ws / "one" / "src") == b.endpoint


def test_no_server_keyed_on_the_home_directory(mm, ws):
    """RT-6.17 step 2: a server started in $HOME writes no endpoint file
    there, so no shell below finds it; its warning says why."""
    state = ws / "state"  # the server's and the shell's HOME is ws
    with server(mm, state, ws) as a:
        assert a.unkeyed and "MM_WORKSPACE" in a.warning()
        assert found(state, ws) == "" and found(state, ws / "one" / "src") == ""

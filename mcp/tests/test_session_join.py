"""session_start side effects: project-channel join, suffix, hook hand-over."""

import json
import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from mattermost_ai_agent_bus import ownpost, session_file
from mattermost_ai_agent_bus.client import MattermostClient, SessionInfo, post_of, unique_name

REPO = Path(__file__).resolve().parents[2]
INFO = SessionInfo(
    url="https://mm.example", name="proj-ab12cd34", username="agent-proj-ab12cd34",
    user_id="uid10000000000000000000000", bot_token="tok-secret",
)


@pytest.fixture(autouse=True)
def _state(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)


def _client(handler, project="proj-chan"):
    c = MattermostClient(base_url="https://mm.example", token="t",
                         project_channel_name=project)
    c.session = INFO
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def _handler(join_status):
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        p = req.url.path
        if p == "/api/v4/teams/name/agents":
            return httpx.Response(200, json={"id": "tid00000000000000000000000"})
        if p == "/api/v4/teams/tid00000000000000000000000/channels/name/proj-chan":
            return httpx.Response(200, json={"id": "cid00000000000000000000000"})
        if p == "/api/v4/channels/cid00000000000000000000000/members":
            assert json.loads(req.content) == {"user_id": "uid10000000000000000000000"}
            return httpx.Response(join_status, json={})
        if p.startswith("/api/v4/users/me/teams/"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    return handler, calls


async def test_joins_project_channel():
    h, calls = _handler(201)
    out = await _client(h).finish_session_setup()
    assert out["project_channel"] == {"name": "proj-chan", "id": "cid00000000000000000000000", "joined": True}
    assert ("POST", "/api/v4/channels/cid00000000000000000000000/members") in calls


@pytest.mark.parametrize("status,needle", [(403, "private"), (404, "does not exist")])
async def test_join_failure_is_reported_not_raised(status, needle):
    h, _ = _handler(status)
    out = await _client(h).finish_session_setup()
    assert out["project_channel"]["joined"] is False
    assert needle in out["project_channel"]["error"]


async def test_missing_project_channel_is_reported():
    def h(req):
        if req.url.path == "/api/v4/teams/name/agents":
            return httpx.Response(200, json={"id": "tid00000000000000000000000"})
        return httpx.Response(404, json={})

    out = await _client(h).finish_session_setup()
    assert "does not exist" in out["project_channel"]["error"]


async def test_no_project_channel_no_join():
    h, calls = _handler(201)
    out = await _client(h, project="").finish_session_setup()
    assert "project_channel" not in out
    assert ("POST", "/api/v4/channels/cid00000000000000000000000/members") not in calls


async def test_welcome_dm_is_marked_read():
    plain, calls = _handler(201)

    def handler(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.startswith("/api/v4/users/me/teams/"):
            return httpx.Response(200, json=[{"channel_id": "dm100000000000000000000000", "msg_count": 0,
                                              "mention_count": 0, "last_viewed_at": 0}])
        if p == "/api/v4/users/me/channels":
            return httpx.Response(200, json=[{"id": "dm100000000000000000000000", "type": "D", "total_msg_count": 1}])
        if p == "/api/v4/users/me":
            return httpx.Response(200, json={"id": "uid10000000000000000000000", "create_at": 0})
        if p == "/api/v4/channels/members/me/view":
            calls.append(("view", json.loads(req.content)["channel_id"]))
            return httpx.Response(200, json={})
        return plain(req)

    await _client(handler).finish_session_setup()
    assert ("view", "dm100000000000000000000000") in calls


def test_unique_name_suffix_and_length():
    n = unique_name("vtpm-hogan")
    assert n.startswith("vtpm-hogan-") and len(n) == len("vtpm-hogan-") + 4
    long = unique_name("a" * 32)
    assert len(long) <= 32 and long.rsplit("-", 1)[0] == "a" * 27  # 32 - "-" - 4 random chars
    assert unique_name("vtpm") != unique_name("vtpm")


def test_unique_name_uses_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "018ggFVR-xxxx")
    a, b = unique_name("vtpm"), unique_name("vtpm")
    assert a.startswith("vtpm-018gg") and a != b  # same session, new name each time


def test_session_file_is_private_and_removed():
    path = session_file.write(INFO, "agents", "agents")
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert "MM_BOT_TOKEN=tok-secret" in path.read_text()
    session_file.remove()
    assert not path.exists()
    session_file.remove()  # idempotent


def _bash(script, **env):
    return subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True,
        env={**os.environ, **env}, cwd=REPO,
    )


FIND = f'source "{REPO}/bin/mm-agent-lib.sh"; mm_bus_file; echo "[$MM_BOT_TOKEN]"'


def _endpoint(session_path):
    """The endpoint file the server writes next to its session file, same key:
    the file mm_bus_file looks for."""
    path = session_path.with_name(session_path.name.replace("mcp-", "endpoint-", 1))
    path.write_text("")
    return path


def test_hooks_find_the_file_the_server_names():
    """RT-6.10, RT-6.17 step 2: Python and bash derive the same file name —
    otherwise hooks stay inert. The shell finds the file; it never loads the
    token from it (no token in an agent's shell)."""
    path = _endpoint(session_file.write(INFO, "agents", "agents"))
    r = _bash(FIND, MM_BOT_TOKEN="", MATTERMOST_TOKEN="")
    assert r.stdout.strip() == f"{path}[]", r.stderr
    assert "mm_load_hook_session" not in (REPO / "bin" / "mm-agent-lib.sh").read_text()


def test_hooks_launch_under_bash():
    """Regression: `sh` is dash on Debian; the scripts use <<< and [[ ]]."""
    hooks = json.loads((REPO / "hooks/hooks.json").read_text())["hooks"]
    for entries in hooks.values():
        for entry in entries:
            for hook in entry["hooks"]:
                assert hook["command"].startswith("bash "), hook["command"]


def test_stop_hook_without_bus_exits_zero():
    r = subprocess.run(
        ["bash", str(REPO / "hooks/stop-inbox-check.sh")],
        input='{"stop_hook_active": false}', capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith(("MM_", "MATTERMOST"))},
    )
    assert r.returncode == 0, r.stderr


async def test_session_file_failure_is_reported(monkeypatch, tmp_path):
    """A hook hand-over that cannot be written must not fail silently."""
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))  # a file, not a directory

    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "username": "agent-x", "user_id": "uid10000000000000000000000", "bot_token": "t"})

    c = MattermostClient(base_url="https://mm.example", reg_secret="s",
                         register_url="https://mm.example/register/v1/agents")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await c.session_start("proj")
    st = c.session_file_status
    assert st["written"] is False and st["error"] and st["project_dir"]


async def test_session_file_success_is_reported():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "username": "agent-x", "user_id": "uid10000000000000000000000", "bot_token": "t"})

    c = MattermostClient(base_url="https://mm.example", reg_secret="s",
                         register_url="https://mm.example/register/v1/agents")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await c.session_start("proj")
    assert c.session_file_status["written"] is True
    assert Path(c.session_file_status["path"]).exists()


def test_a_shell_in_a_subdirectory_walks_up_to_the_workspace(tmp_path):
    """RT-6.17 step 2: no variable names the workspace (OpenCode's shell): the
    shell walks up from its real directory to the server's file. A named
    workspace is taken as it is, with no walk."""
    path = _endpoint(session_file.write(INFO, "agents", "agents"))  # keyed on CLAUDE_PROJECT_DIR=tmp_path
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    r = subprocess.run(["bash", "-c", FIND], capture_output=True, text=True, env=env, cwd=sub)
    assert r.stdout.strip() == f"{path}[]", r.stderr
    assert _bash(FIND, CLAUDE_PROJECT_DIR=str(sub)).stdout.strip() == "[]"


def test_hook_outside_project_finds_nothing(tmp_path):
    other = tmp_path.parent / "elsewhere"
    other.mkdir(exist_ok=True)
    r = _bash(FIND, MM_BOT_TOKEN="", MATTERMOST_TOKEN="", CLAUDE_PROJECT_DIR=str(other))
    assert r.stdout.strip() == "[]"


def _shutdown_client(handler, *, own_session, token="bot-tok"):
    c = MattermostClient(
        base_url="https://mm.example", token=token,
        register_url="https://mm.example/register/v1/agents",
    )
    if own_session:
        c.session = INFO
    real = httpx.Client
    return c, real


def _patch_http(monkeypatch, handler):
    real = httpx.Client
    monkeypatch.setattr(
        "mattermost_ai_agent_bus.client.httpx.Client",
        lambda **kw: real(transport=httpx.MockTransport(handler), **kw),
    )


def test_shutdown_unregisters_own_session_that_signed_off(monkeypatch):
    """RT-6.10 step 7: without a session id the bot is kept for the workspace's
    next server, unless the agent's last post was a BYE."""
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(204)

    _patch_http(monkeypatch, handler)
    session_file.write(INFO, "agents", "agents")
    ownpost.record(INFO.user_id, "cid00000000000000000000000", post_of({"id": "p1", "message": "BYE · done"}))
    c, _ = _shutdown_client(handler, own_session=True, token=INFO.bot_token)
    assert c.shutdown_sync() == "unregistered proj-ab12cd34"
    assert seen == [("DELETE", "/register/v1/agents/proj-ab12cd34")]
    assert not session_file.session_file_path().exists()
    assert c.shutdown_sync() == "no session"  # idempotent


def test_shutdown_host_registered_session(monkeypatch):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        if req.url.path == "/api/v4/users/me":
            return httpx.Response(200, json={"id": "u9000000000000000000000000", "username": "agent-vtpm-b87c"})
        return httpx.Response(204)

    _patch_http(monkeypatch, handler)
    c, _ = _shutdown_client(handler, own_session=False)
    assert c.shutdown_sync() == "unregistered vtpm-b87c"
    assert ("PUT", "/api/v4/users/u9000000000000000000000000/status") in seen
    assert seen[-1] == ("DELETE", "/register/v1/agents/vtpm-b87c")


def test_shutdown_never_deletes_a_human(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req.method)
        return httpx.Response(200, json={"id": "u1000000000000000000000000", "username": "andreas"})

    _patch_http(monkeypatch, handler)
    c, _ = _shutdown_client(handler, own_session=False)
    assert "left alone" in c.shutdown_sync()
    assert "DELETE" not in seen


def test_shutdown_survives_network_failure(monkeypatch):
    def handler(req):
        raise httpx.ConnectError("down")

    _patch_http(monkeypatch, handler)
    c, _ = _shutdown_client(handler, own_session=True)
    assert c.shutdown_sync().startswith("unregister failed")


def test_two_sessions_in_one_checkout_keep_their_own_bot(monkeypatch):
    """RT-6.10: keyed on the project alone, the second session's file replaced the
    first one's, and the first agent's hooks ran as the second bot."""
    other = SessionInfo(url=INFO.url, name="proj-zz99", username="agent-proj-zz99",
                        user_id="uid2", bot_token="tok-other")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-one")
    first = session_file.write(INFO, "agents", "agents")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-two")
    second = session_file.write(other, "agents", "agents")
    assert first != second and first.exists() and second.exists()
    for sid, path in (("sess-one", _endpoint(first)), ("sess-two", _endpoint(second))):
        r = _bash(FIND, MM_BOT_TOKEN="", MATTERMOST_TOKEN="", CLAUDE_CODE_SESSION_ID=sid)
        assert r.stdout.strip() == f"{path}[]", r.stderr
    session_file.remove()  # sess-two ends; sess-one keeps its file
    assert first.exists() and not second.exists()


def test_hook_with_session_id_ignores_a_project_keyed_file():
    """RT-6.10: a project-keyed file may belong to another session."""
    session_file.write(INFO, "agents", "agents")  # no session id: project key
    r = _bash(FIND, MM_BOT_TOKEN="", MATTERMOST_TOKEN="", CLAUDE_CODE_SESSION_ID="sess-new")
    assert r.stdout.strip() == "[]"


def test_hook_takes_the_session_id_from_its_input(monkeypatch):
    """RT-6.10: without CLAUDE_CODE_SESSION_ID in the environment the hook payload's
    session_id keys the lookup."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-one")
    path = _endpoint(session_file.write(INFO, "agents", "agents"))
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    r = subprocess.run(
        ["bash", "-c", f'source "{REPO}/bin/mm-agent-lib.sh"; mm_read_hook_input; mm_bus_file'],
        input='{"session_id": "sess-one"}', capture_output=True, text=True,
        env={**os.environ, "MM_BOT_TOKEN": "", "MATTERMOST_TOKEN": ""}, cwd=REPO,
    )
    assert r.stdout.strip() == str(path), r.stderr


def test_exit_keeps_the_bot_for_claude_continue(monkeypatch):
    """RT-6.10: `claude --continue` keeps the session id; the bot must survive
    the server's end (Claude Code kills it ~0.5 s after asking it to stop)."""
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={})

    _patch_http(monkeypatch, handler)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-one")
    path = session_file.write(INFO, "agents", "agents")
    os.utime(path, (1, 1))
    c, _ = _shutdown_client(handler, own_session=True)
    assert c.shutdown_sync() == "kept proj-ab12cd34 for claude --continue"
    assert not any(m == "DELETE" for m, _ in seen)
    assert ("PUT", "/api/v4/users/uid10000000000000000000000/status") in seen  # offline, best effort
    assert path.exists() and path.stat().st_mtime > 1  # grace period starts now


def _adopting_client():
    return MattermostClient(base_url="https://mm.example", token=None,
                            register_url="https://mm.example/register/v1/agents")


def test_next_server_takes_the_bot_over(monkeypatch):
    """RT-6.10: same session id, token still accepted → same bot, no new name."""
    _patch_http(monkeypatch, lambda req: httpx.Response(200, json={"id": "uid10000000000000000000000"}))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-one")
    session_file.write(INFO, "agents", "agents")
    c = _adopting_client()
    info = c.adopt_sync()
    assert info is not None and info.username == "agent-proj-ab12cd34"
    assert c.token == "tok-secret" and c.adopted and c.keeps_bot_on_exit


def test_a_rejected_token_is_not_taken_over(monkeypatch):
    _patch_http(monkeypatch, lambda req: httpx.Response(401, json={}))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-one")
    path = session_file.write(INFO, "agents", "agents")
    c = _adopting_client()
    assert c.adopt_sync() is None and c.token is None
    assert not path.exists()


def test_no_take_over_without_a_session_id(monkeypatch):
    """A project-keyed file may belong to another session."""
    _patch_http(monkeypatch, lambda req: httpx.Response(200, json={}))
    session_file.write(INFO, "agents", "agents")
    assert _adopting_client().adopt_sync() is None


# --- a harness restarts the server, no session id (RT-6.10 step 7) -----------

def _restart(monkeypatch, *, ended_ago=0.0, workspace=None):
    """A server of this workspace ended without session_end; returns its file."""
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(200, json={"id": INFO.user_id})

    _patch_http(monkeypatch, handler)
    path = session_file.write(INFO, "agents", "agents")
    c, _ = _shutdown_client(handler, own_session=True, token=INFO.bot_token)
    assert c.shutdown_sync() == "kept proj-ab12cd34 for the workspace's next server"
    assert not any(m == "DELETE" for m, _ in seen)
    if ended_ago:
        text = path.read_text().replace(f"{session_file.ENDED}=", "OLD=")
        path.write_text(text + f"{session_file.ENDED}={int(time.time() - ended_ago)}\n")
    if workspace is not None:
        monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(workspace))
    return path


def test_a_restarted_server_takes_the_workspace_bot_over(monkeypatch):
    """RT-6.10 step 7: OpenCode reloads its plugin, Cursor restarts a server."""
    path = _restart(monkeypatch)
    c = _adopting_client()
    info = c.adopt_sync()
    assert info is not None and info.username == INFO.username and c.token == INFO.bot_token
    assert session_file.ENDED not in session_file.parse(path)  # in use again
    assert _adopting_client().adopt_sync() is None  # a third server gets nothing


def test_a_running_server_keeps_its_workspace_bot(monkeypatch):
    """RT-6.10 step 7: only a server that ended marks its file."""
    _patch_http(monkeypatch, lambda req: httpx.Response(200, json={}))
    session_file.write(INFO, "agents", "agents")
    assert _adopting_client().adopt_sync() is None


async def test_after_the_grace_period_the_workspace_bot_goes(monkeypatch):
    """RT-6.10 step 7: too late to take over; the new registration reaps it
    instead of orphaning it under the same file name."""
    path = _restart(monkeypatch, ended_ago=2 * 86400)
    assert _adopting_client().adopt_sync() is None and path.exists()
    deleted = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE":
            deleted.append(req.url.path.rsplit("/", 1)[1])
            return httpx.Response(204)
        return httpx.Response(200, json={
            "username": "agent-x", "user_id": "uid90000000000000000000000", "bot_token": "t"})

    c = MattermostClient(base_url="https://mm.example", reg_secret="s",
                         register_url="https://mm.example/register/v1/agents")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await c.session_start("proj")
    assert deleted == ["proj-ab12cd34"] and c.reaped == ["proj-ab12cd34"]


def test_another_workspace_never_takes_the_bot_over(monkeypatch, tmp_path):
    """RT-6.10 step 7: the key is the workspace."""
    other = tmp_path / "other"
    other.mkdir()
    path = _restart(monkeypatch, workspace=other)
    assert _adopting_client().adopt_sync() is None
    assert session_file.ENDED in session_file.parse(path)  # still there for its own


async def test_session_end_wins_over_a_restart(monkeypatch):
    """RT-6.10 step 7: an explicit session_end unregisters at once; the next
    server finds nothing to take over."""
    deleted = []

    def handler(req: httpx.Request) -> httpx.Response:
        deleted.append(req.method)
        return httpx.Response(204)

    session_file.write(INFO, "agents", "agents")
    c = _client(handler)
    c.token, c.register_url = INFO.bot_token, "https://mm.example/register/v1/agents"
    await c.session_end()
    assert deleted == ["DELETE"] and not session_file.session_file_path().exists()
    assert c.shutdown_sync() == "no session"
    _patch_http(monkeypatch, lambda req: httpx.Response(200, json={}))
    assert _adopting_client().adopt_sync() is None


def _other_session(name, *, pid, age_sec, extra=""):
    d = session_file.state_dir()
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"mcp-sid-{name}.env"
    p.write_text(f"MM_REG_BOT_NAME={name}\nMM_BOT_TOKEN=tok-{name}\nMM_SESSION_PID={pid}\n{extra}")
    import time
    t = time.time() - age_sec
    os.utime(p, (t, t))
    return p


async def test_session_start_reaps_bots_of_long_ended_sessions(monkeypatch):
    """RT-6.10: bots kept for --continue go once their session ended a day ago."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-new")
    gone = _other_session("old-gone", pid="999999", age_sec=2 * 86400)
    dead_token = _other_session("old-dead", pid="999999", age_sec=2 * 86400)
    recent = _other_session("recent", pid="999999", age_sec=3600)
    live = _other_session("live", pid=str(os.getpid()), age_sec=2 * 86400)
    deleted = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "DELETE":
            deleted.append(req.url.path.rsplit("/", 1)[1])
            code = 401 if "old-dead" in req.url.path else 204
            return httpx.Response(code)
        return httpx.Response(200, json={
            "username": "agent-x", "user_id": "uid90000000000000000000000", "bot_token": "t"})

    c = MattermostClient(base_url="https://mm.example", reg_secret="s",
                         register_url="https://mm.example/register/v1/agents")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await c.session_start("proj")
    assert sorted(deleted) == ["old-dead", "old-gone"]
    assert sorted(c.reaped) == ["old-dead", "old-gone"]
    assert not gone.exists() and not dead_token.exists()
    assert recent.exists() and live.exists()


async def test_session_start_returns_the_bot_it_already_has():
    """RT-6.10: a second session_start (or one after a take-over) must not
    register another bot and abandon this one."""
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json={"id": "uid10000000000000000000000", "username": INFO.username})

    c = _client(handler)
    c.token, c.reg_secret = INFO.bot_token, "s"
    c.register_url = "https://mm.example/register/v1/agents"
    info = await c.session_start("proj")
    assert info is INFO and c.reused
    assert ("POST", "/register/v1/agents") not in calls


# --- any registrar prefix (arc42 §7.1) ---------------------------------------

def test_agent_prefix_is_learned_from_the_registration():
    """MM_AGENT_PREFIX wins; else the username minus the registered name; else agent-."""
    ap = session_file.agent_prefix
    assert ap({}) == "agent-"
    assert ap({"MM_BOT_USERNAME": "bot-mac-1", "MM_REG_BOT_NAME": "mac-1"}) == "bot-"
    assert ap({"MM_BOT_USERNAME": "bot-mac-1", "MM_REG_BOT_NAME": "other"}) == "agent-"
    assert ap({"MM_BOT_USERNAME": "mac-1", "MM_REG_BOT_NAME": "mac-1"}) == "agent-"
    assert ap({"MM_AGENT_PREFIX": "ai-", "MM_BOT_USERNAME": "bot-mac-1",
               "MM_REG_BOT_NAME": "mac-1"}) == "ai-"


def test_agent_prefix_reads_the_environment(monkeypatch):
    monkeypatch.setenv("MM_BOT_USERNAME", "bot-mac-1")
    monkeypatch.setenv("MM_REG_BOT_NAME", "mac-1")
    assert session_file.agent_prefix() == "bot-"
    monkeypatch.setenv("MM_AGENT_PREFIX", "ai-")
    assert session_file.agent_prefix() == "ai-"


def test_a_registration_hands_its_prefix_to_the_hooks():
    c = MattermostClient(base_url="https://mm.example", token="t")
    c._take(SessionInfo(url=INFO.url, name="mac-1", username="bot-mac-1",
                        user_id="uid10000000000000000000000", bot_token="tok"))
    assert os.environ["MM_AGENT_PREFIX"] == "bot-"
    assert session_file.parse(session_file.session_file_path())["MM_AGENT_PREFIX"] == "bot-"


def test_shutdown_host_registered_session_with_another_prefix(monkeypatch):
    """The host hands the prefix over; the registrar gets the bare name."""
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        if req.url.path == "/api/v4/users/me":
            return httpx.Response(200, json={"id": "u9000000000000000000000000", "username": "bot-vtpm-b87c"})
        return httpx.Response(204)

    _patch_http(monkeypatch, handler)
    c, _ = _shutdown_client(handler, own_session=False)
    assert "left alone" in c.shutdown_sync()  # agent-: not a bus bot
    monkeypatch.setenv("MM_AGENT_PREFIX", "bot-")
    c, _ = _shutdown_client(handler, own_session=False)
    assert c.shutdown_sync() == "unregistered vtpm-b87c"
    assert seen[-1] == ("DELETE", "/register/v1/agents/vtpm-b87c")


async def test_session_end_sends_the_bare_name_with_another_prefix(monkeypatch):
    monkeypatch.setenv("MM_AGENT_PREFIX", "bot-")
    deleted = []

    def handler(req: httpx.Request) -> httpx.Response:
        deleted.append(req.url.path)
        return httpx.Response(204)

    c = MattermostClient(base_url="https://mm.example", token="t",
                         register_url="https://mm.example/register/v1/agents")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await c.session_end("bot-mac-1")
    assert deleted == ["/register/v1/agents/mac-1"]


async def test_reaping_strips_the_prefix_the_session_file_names(monkeypatch):
    """RT-6.10: each file's own prefix, whatever the registrar used."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-new")
    gone = _other_session("bot-old", pid="999999", age_sec=2 * 86400,
                          extra="MM_AGENT_PREFIX=bot-\n")
    deleted = []

    def handler(req: httpx.Request) -> httpx.Response:
        deleted.append(req.url.path)
        return httpx.Response(204)

    c = MattermostClient(base_url="https://mm.example",
                         register_url="https://mm.example/register/v1/agents")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await c.reap_stale_sessions() == ["bot-old"]
    assert deleted == ["/register/v1/agents/old"] and not gone.exists()


async def test_session_end_names_the_bot_it_ended(monkeypatch):
    """RT-6.10: session_end() without a name answers with the registered bot's
    name, not MM_REG_BOT_NAME (which a self-registered server never sets)."""
    from mattermost_ai_agent_bus import server

    monkeypatch.delenv("MM_REG_BOT_NAME", raising=False)
    c = MattermostClient(base_url="https://mm.example", token="t",
                         register_url="https://mm.example/register/v1/agents")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(204)))
    c.session = SessionInfo(url="https://mm.example", name="agent-codex-1a2b", username="agent-codex-1a2b",
                            user_id="u" * 26, bot_token="t")
    monkeypatch.setattr(server, "_client", c)
    monkeypatch.setattr(server, "_inbox", None)
    out = json.loads(await server.session_end())
    assert out["ended"] == "agent-codex-1a2b" and c.session is None

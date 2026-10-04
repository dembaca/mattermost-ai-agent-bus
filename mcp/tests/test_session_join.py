"""session_start side effects: project-channel join, suffix, hook hand-over."""

import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest

from mattermost_ai_agent_bus import session_file
from mattermost_ai_agent_bus.client import MattermostClient, SessionInfo, unique_name

REPO = Path(__file__).resolve().parents[2]
INFO = SessionInfo(
    url="https://mm.example", name="proj-ab12cd34", username="agent-proj-ab12cd34",
    user_id="uid1", bot_token="tok-secret",
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
            return httpx.Response(200, json={"id": "tid"})
        if p == "/api/v4/teams/tid/channels/name/proj-chan":
            return httpx.Response(200, json={"id": "cid"})
        if p == "/api/v4/channels/cid/members":
            assert json.loads(req.content) == {"user_id": "uid1"}
            return httpx.Response(join_status, json={})
        if p.startswith("/api/v4/users/me/teams/"):
            return httpx.Response(200, json=[])
        return httpx.Response(404, json={})

    return handler, calls


async def test_joins_project_channel():
    h, calls = _handler(201)
    out = await _client(h).finish_session_setup()
    assert out["project_channel"] == {"name": "proj-chan", "id": "cid", "joined": True}
    assert ("POST", "/api/v4/channels/cid/members") in calls


@pytest.mark.parametrize("status,needle", [(403, "private"), (404, "does not exist")])
async def test_join_failure_is_reported_not_raised(status, needle):
    h, _ = _handler(status)
    out = await _client(h).finish_session_setup()
    assert out["project_channel"]["joined"] is False
    assert needle in out["project_channel"]["error"]


async def test_missing_project_channel_is_reported():
    def h(req):
        if req.url.path == "/api/v4/teams/name/agents":
            return httpx.Response(200, json={"id": "tid"})
        return httpx.Response(404, json={})

    out = await _client(h).finish_session_setup()
    assert "does not exist" in out["project_channel"]["error"]


async def test_no_project_channel_no_join():
    h, calls = _handler(201)
    out = await _client(h, project="").finish_session_setup()
    assert "project_channel" not in out
    assert ("POST", "/api/v4/channels/cid/members") not in calls


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


def test_hook_loads_token_written_by_server():
    """Python and bash must derive the same file name — otherwise hooks stay inert."""
    session_file.write(INFO, "agents", "agents")
    r = _bash(
        f'source "{REPO}/bin/mm-agent-lib.sh"; mm_load_hook_session; echo "$MM_BOT_TOKEN"',
        MM_BOT_TOKEN="", MATTERMOST_TOKEN="",
    )
    assert r.stdout.strip() == "tok-secret", r.stderr


def test_hook_loader_does_not_override_existing_token():
    session_file.write(INFO, "agents", "agents")
    r = _bash(
        f'source "{REPO}/bin/mm-agent-lib.sh"; mm_load_hook_session; echo "$MM_BOT_TOKEN"',
        MM_BOT_TOKEN="host-token",
    )
    assert r.stdout.strip() == "host-token"


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
            "username": "agent-x", "user_id": "uid1", "bot_token": "t"})

    c = MattermostClient(base_url="https://mm.example", reg_secret="s",
                         register_url="https://mm.example/register/v1/agents")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await c.session_start("proj")
    st = c.session_file_status
    assert st["written"] is False and st["error"] and st["project_dir"]


async def test_session_file_success_is_reported():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "username": "agent-x", "user_id": "uid1", "bot_token": "t"})

    c = MattermostClient(base_url="https://mm.example", reg_secret="s",
                         register_url="https://mm.example/register/v1/agents")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await c.session_start("proj")
    assert c.session_file_status["written"] is True
    assert Path(c.session_file_status["path"]).exists()


def test_hook_in_subdirectory_finds_session_of_project_root(tmp_path, monkeypatch):
    """The agent cd'ed into a subdirectory: the hook walks up to the server's dir."""
    session_file.write(INFO, "agents", "agents")  # keyed on CLAUDE_PROJECT_DIR=tmp_path
    sub = tmp_path / "src" / "deep"
    sub.mkdir(parents=True)
    r = _bash(
        f'source "{REPO}/bin/mm-agent-lib.sh"; mm_load_hook_session; echo "$MM_BOT_TOKEN"',
        MM_BOT_TOKEN="", MATTERMOST_TOKEN="", CLAUDE_PROJECT_DIR=str(sub),
    )
    assert r.stdout.strip() == "tok-secret", r.stderr


def test_hook_outside_project_finds_nothing(tmp_path):
    other = tmp_path.parent / "elsewhere"
    other.mkdir(exist_ok=True)
    r = _bash(
        f'source "{REPO}/bin/mm-agent-lib.sh"; mm_load_hook_session; echo "[$MM_BOT_TOKEN]"',
        MM_BOT_TOKEN="", MATTERMOST_TOKEN="", CLAUDE_PROJECT_DIR=str(other),
    )
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


def test_shutdown_unregisters_own_session(monkeypatch):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        return httpx.Response(204)

    _patch_http(monkeypatch, handler)
    session_file.write(INFO, "agents", "agents")
    c, _ = _shutdown_client(handler, own_session=True)
    assert c.shutdown_sync() == "unregistered proj-ab12cd34"
    assert seen == [("DELETE", "/register/v1/agents/proj-ab12cd34")]
    assert not session_file.session_file_path().exists()
    assert c.shutdown_sync() == "no session"  # idempotent


def test_shutdown_host_registered_session(monkeypatch):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path))
        if req.url.path == "/api/v4/users/me":
            return httpx.Response(200, json={"id": "u9", "username": "agent-vtpm-b87c"})
        return httpx.Response(204)

    _patch_http(monkeypatch, handler)
    c, _ = _shutdown_client(handler, own_session=False)
    assert c.shutdown_sync() == "unregistered vtpm-b87c"
    assert ("PUT", "/api/v4/users/u9/status") in seen
    assert seen[-1] == ("DELETE", "/register/v1/agents/vtpm-b87c")


def test_shutdown_never_deletes_a_human(monkeypatch):
    seen = []

    def handler(req):
        seen.append(req.method)
        return httpx.Response(200, json={"id": "u1", "username": "andreas"})

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

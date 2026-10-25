"""The MCP server ends with its client (RT-6.10 error handling, RT-6.17) and
runs in channel mode for Claude Code only (RT-6.12).

Each test starts the real server over pipes, as a harness does, against the
fake Mattermost of conftest.py.
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from pathlib import Path

import pytest
from conftest import CHANNEL, ME, ROOT, TEAM

PY = ROOT / "mcp" / ".venv" / "bin" / "python"


def _env(mm, tmp_path: Path, **extra: str) -> dict[str, str]:
    return {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "XDG_STATE_HOME": str(tmp_path / "state"),
            "MM_CHAT_URL": mm.url, "MM_BOT_TOKEN": "bot-token-for-tests", "MM_BOT_USER_ID": ME,
            "MM_TEAM_ID": TEAM, "MM_CHANNEL_ID": CHANNEL, "MM_CHANNEL_PUSH": "0", **extra}


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir(exist_ok=True)
    return proj


def _readline(stream, timeout: float = 20) -> dict:
    deadline = time.monotonic() + timeout
    buf = b""
    while not buf.endswith(b"\n"):
        left = deadline - time.monotonic()
        assert left > 0, buf
        if select.select([stream], [], [], left)[0]:
            chunk = os.read(stream.fileno(), 65536)
            assert chunk, buf
            buf += chunk
    return json.loads(buf.splitlines()[0])


def _send(p, obj) -> None:
    p.stdin.write(json.dumps(obj).encode() + b"\n")
    p.stdin.flush()


def _handshake(p, client: str = "codex-mcp-client") -> dict:
    _send(p, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": client, "version": "0"}}})
    init = _readline(p.stdout)["result"]
    _send(p, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return init


def _endpoint_files(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "state").rglob("endpoint-*.env"))


def _wait_for(cond, timeout: float = 10) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture
def start(mm, tmp_path):
    procs: list[subprocess.Popen] = []

    def _start(**extra: str) -> subprocess.Popen:
        p = subprocess.Popen([str(PY), "-m", "mattermost_ai_agent_bus"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             cwd=_project(tmp_path), env=_env(mm, tmp_path, **extra))
        procs.append(p)
        return p

    yield _start
    for p in procs:
        if p.poll() is None:
            p.kill()
            p.wait()


@pytest.mark.parametrize("push", ["0", "1"])
def test_closing_stdin_ends_the_server_and_its_file(start, tmp_path, push):
    """RT-6.17: the client closes stdin — the exit work runs (endpoint file
    gone) and the process leaves, in channel mode as well."""
    p = start(MM_CHANNEL_PUSH=push)
    _handshake(p, "claude-code")
    assert _wait_for(lambda: _endpoint_files(tmp_path))
    p.stdin.close()
    assert p.wait(timeout=10) == 0
    assert _endpoint_files(tmp_path) == []


@pytest.mark.parametrize("push", ["0", "1"])
def test_sigterm_ends_the_server_within_seconds(start, tmp_path, push):
    """RT-6.10 error handling: SIGTERM with stdin still open — before 0.40.0
    the process removed its file and then hung on the SDK's stdin thread."""
    p = start(MM_CHANNEL_PUSH=push)
    _handshake(p, "claude-code")
    assert _wait_for(lambda: _endpoint_files(tmp_path))
    t = time.monotonic()
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=6)
    assert time.monotonic() - t < 3.5
    assert _endpoint_files(tmp_path) == []


def test_the_server_ends_when_its_client_dies(mm, tmp_path):
    """RT-6.10 error handling: the client (here a shell) is killed while
    something else keeps the server's stdin open, so no EOF comes — the
    server notices its parent is gone and leaves, file and all."""
    fifo = tmp_path / "stdin"
    script = (f'mkfifo "{fifo}"; sleep 60 >"{fifo}" & SL=$!; '
              f'"{PY}" -m mattermost_ai_agent_bus <"{fifo}" >/dev/null 2>&1 & echo "$! $SL"; '
              'while ! ls "$XDG_STATE_HOME"/mm-agent-bus/endpoint-*.env >/dev/null 2>&1; do sleep 0.1; done; '
              'exit 0')
    shell = subprocess.run(["bash", "-c", script], cwd=_project(tmp_path), env=_env(mm, tmp_path),
                           stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=30)
    pid, holder = (int(x) for x in shell.stdout.split())
    try:
        def gone() -> bool:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            # a zombie of an orphan is reaped by init; ps shows whether it still runs
            ps = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True)
            return ps.returncode != 0 or ps.stdout.strip().startswith("Z")

        assert _wait_for(gone, timeout=8), "server outlived its client"
        assert _endpoint_files(tmp_path) == []
    finally:
        for left in (pid, holder):
            try:
                os.kill(left, signal.SIGKILL)
            except ProcessLookupError:
                pass


def _tools(p) -> set[str]:
    _send(p, {"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {}})
    return {t["name"] for t in _readline(p.stdout)["result"]["tools"]}


def test_channel_push_is_claude_codes_alone(start):
    """RT-6.12: an MM_CHANNEL_PUSH=1 inherited from the user's shell puts no
    other client into channel mode: no capability, no channel_ok, and the
    instructions to check wait_for_events itself."""
    p = start(MM_CHANNEL_PUSH="1")
    init = _handshake(p, "codex-mcp-client")
    assert "claude/channel" not in (init["capabilities"].get("experimental") or {})
    assert "Check wait_for_events" in init["instructions"]
    assert "Started as a channel" not in init["instructions"]
    assert "channel_ok" not in _tools(p)
    p = start(MM_CHANNEL_PUSH="1")
    init = _handshake(p, "claude-code")
    assert "claude/channel" in init["capabilities"]["experimental"]
    assert "channel_ok" in _tools(p)


def test_another_client_ignores_claude_codes_session_id(start, tmp_path):
    """RT-6.17 step 2: a harness started from a Claude Code shell inherits
    CLAUDE_CODE_SESSION_ID; its server keys on the workspace instead, so it
    takes neither the Claude session's endpoint file nor its bot."""
    p = start(CLAUDE_CODE_SESSION_ID="cc-parent", CLAUDE_PROJECT_DIR=str(tmp_path / "elsewhere"))
    _handshake(p, "opencode")

    def keyed_on_workspace() -> bool:
        names = [f.name for f in _endpoint_files(tmp_path)]
        return len(names) == 1 and not names[0].startswith("endpoint-sid-")

    assert _wait_for(keyed_on_workspace)
    q = start(CLAUDE_CODE_SESSION_ID="cc-own")
    _handshake(q, "claude-code")
    assert _wait_for(lambda: any(f.name.startswith("endpoint-sid-") for f in _endpoint_files(tmp_path)))

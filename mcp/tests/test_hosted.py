"""RT-6.18: keyless host-managed session — the MCP server on the host, a relay
in the sandbox (docs/arc42/06-runtime-view.md)."""

from __future__ import annotations

import io
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import httpx
import pytest

from mattermost_ai_agent_bus import hosted, session_file, stats

KEY = "k" * 64
SRC = Path(__file__).resolve().parents[1] / "src"


async def _echo(read_stream, write_stream) -> None:
    async with write_stream:
        async for item in read_stream:
            if not isinstance(item, Exception):
                await write_stream.send(item)


class _Conn:
    def __init__(self, stream) -> None:
        self.stream = stream
        self.buf = b""

    async def line(self) -> dict:
        while b"\n" not in self.buf:
            self.buf += await self.stream.receive()
        line, _, self.buf = self.buf.partition(b"\n")
        return json.loads(line)

    async def send(self, obj) -> None:
        await self.stream.send(json.dumps(obj).encode() + b"\n")


async def _hello(port: int, key: str = KEY, session: dict | None = None) -> tuple[_Conn, dict]:
    conn = _Conn(await anyio.connect_tcp("127.0.0.1", port))
    await conn.send({"key": key, "session": session or {"CLAUDE_CODE_SESSION_ID": "s1"}})
    return conn, await conn.line()


async def _with_host(host: hosted.Host, tmp_path: Path, body, **kw) -> None:
    ready = tmp_path / "ready.json"
    async with anyio.create_task_group() as tg:
        async def run() -> None:
            await hosted.run_host(host, ready_file=ready, signals=(), **kw)

        tg.start_soon(run)
        with anyio.fail_after(5):
            while not ready.exists():
                await anyio.sleep(0.01)
        try:
            await body(json.loads(ready.read_text()))
        finally:
            tg.cancel_scope.cancel()


async def test_wrong_key_refused(tmp_path):
    """RT-6.18 step 4: anything without the session key is refused before the server loads."""
    applied, served = [], []

    async def serve(r, w):
        served.append(1)

    host = hosted.Host(KEY, serve=serve, apply=applied.append)

    async def body(ready):
        conn, answer = await _hello(ready["port"], key="x" * 64)
        assert answer == {"ok": False, "error": "wrong key"}
        await conn.stream.aclose()
        conn = _Conn(await anyio.connect_tcp("127.0.0.1", ready["port"]))
        await conn.stream.send(b"not json\n")
        with pytest.raises((anyio.EndOfStream, anyio.BrokenResourceError)):
            await conn.line()

    await _with_host(host, tmp_path, body, with_services=False)
    assert applied == [] and served == []


async def test_handshake_names_only_then_mcp_messages_pass(tmp_path):
    """RT-6.18 steps 4-5: only the naming values reach the host (HOME,
    XDG_STATE_HOME and the rest are dropped), then JSON-RPC lines both ways."""
    order = []
    host = hosted.Host(KEY, serve=lambda r, w: (order.append("serve"), _echo(r, w))[1],
                       apply=lambda v: order.append(dict(v)))

    async def body(ready):
        session = {"CLAUDE_CODE_SESSION_ID": "s1", "HOME": "/sandbox/home",
                   "XDG_STATE_HOME": "/sandbox/state", "cwd": "/w", "MM_CHANNEL_PUSH": "0",
                   "PYTHONPATH": "/w"}
        conn, answer = await _hello(ready["port"], session=session)
        assert answer == {"ok": True}
        await conn.send({"jsonrpc": "2.0", "id": 7, "method": "ping"})
        await conn.send({"jsonrpc": "2.0", "method": "notifications/claude/channel",
                         "params": {"content": "hi"}})
        assert (await conn.line())["id"] == 7
        assert (await conn.line())["params"] == {"content": "hi"}
        await conn.stream.aclose()

    await _with_host(host, tmp_path, body, with_services=False)
    assert order == [{"CLAUDE_CODE_SESSION_ID": "s1", "cwd": "/w", "MM_CHANNEL_PUSH": "0"}, "serve"]


@pytest.mark.parametrize("session, error", [
    ({"cwd": "relative/dir"}, "absolute path"),
    ({"CLAUDE_PROJECT_DIR": "../up"}, "absolute path"),
    ({"CLAUDE_CODE_SESSION_ID": 7}, "one line"),
    ({"CLAUDE_CODE_SESSION_ID": "x" * 257}, "one line"),
    ({"cwd": "/w\n/x"}, "one line"),
    ({"MM_CHANNEL_PUSH": "2"}, "0 or 1"),
    ([], "JSON object"),
])
async def test_handshake_values_are_checked(tmp_path, session, error):
    """RT-6.18 step 4: wrong types, lengths or relative paths refuse the relay."""
    host = hosted.Host(KEY, serve=_echo, apply=lambda v: pytest.fail("applied"))

    async def body(ready):
        conn = _Conn(await anyio.connect_tcp("127.0.0.1", ready["port"]))
        await conn.send({"key": KEY, "session": session})
        answer = await conn.line()
        assert answer["ok"] is False and error in answer["error"]
        await conn.stream.aclose()

    await _with_host(host, tmp_path, body, with_services=False)


async def test_one_relay_at_a_time_and_reconnect(tmp_path):
    """RT-6.18 step 6: a second relay is refused while one is connected; after it
    closed, a new one is served (MCP reconnect) and the session goes on."""
    host = hosted.Host(KEY, serve=_echo, apply=lambda v: None)

    async def body(ready):
        first, answer = await _hello(ready["port"])
        assert answer["ok"]
        second, answer = await _hello(ready["port"])
        assert answer == {"ok": False, "error": "another relay is connected"}
        await first.stream.aclose()
        with anyio.fail_after(5):
            while host.active:
                await anyio.sleep(0.01)
        third, answer = await _hello(ready["port"])
        assert answer["ok"]
        await third.send({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert (await third.line())["id"] == 1
        await third.stream.aclose()

    await _with_host(host, tmp_path, body, with_services=False)
    assert host.served == 2


def test_apply_session_sets_names_and_never_changes_directory(tmp_path, monkeypatch):
    """RT-6.18 step 4: names for the server's files; the host's cwd stays."""
    env = {"HOME": "/host/home", "XDG_STATE_HOME": "/host/state", "CLAUDE_PROJECT_DIR": "/old",
           "MM_BOT_TOKEN": "t"}
    cwd = os.getcwd()
    hosted.apply_session(hosted.check_session(
        {"CLAUDE_CODE_SESSION_ID": "s1", "cwd": "/w/./x/..", "MM_CHANNEL_PUSH": "0",
         "HOME": str(tmp_path), "evil": "x"}), environ=env)
    assert env == {"HOME": "/host/home", "XDG_STATE_HOME": "/host/state", "MM_BOT_TOKEN": "t",
                   "CLAUDE_CODE_SESSION_ID": "s1", "MM_CHANNEL_PUSH": "0",
                   "CLAUDE_PROJECT_DIR": "/w"}
    assert os.getcwd() == cwd


def test_hosted_state_dir_ignores_the_sessions_home(tmp_path, monkeypatch):
    """RT-6.18 (security): the host chooses the state dir; HOME and
    XDG_STATE_HOME (the sandbox's, or anyone's) do not move it."""
    own = session_file.use_hosted_dir(tmp_path / "host" / "hosted" / "sess-1")
    assert (own.stat().st_mode & 0o777) == 0o700
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "sandbox" / "state"))
    monkeypatch.setenv("HOME", str(tmp_path / "sandbox"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "sandbox" / "w"))
    assert session_file.hosted() and session_file.state_dir() == own
    assert session_file.keyed_path("mcp").parent == own
    assert session_file.project_dir() == str(tmp_path / "sandbox" / "w")  # a name, not resolved


def test_hosted_state_dir_refuses_a_symlink(tmp_path):
    """RT-6.18 (security): a symlink in the state dir's place is never followed."""
    target = tmp_path / "elsewhere"
    target.mkdir()
    (tmp_path / "hosted").mkdir(mode=0o700)
    (tmp_path / "hosted" / "sess-1").symlink_to(target)
    with pytest.raises(OSError):
        session_file.use_hosted_dir(tmp_path / "hosted" / "sess-1")
    assert not session_file.hosted()


def test_state_files_refuse_a_symlinked_target(tmp_path):
    """RT-6.18 (security): no state-file write lands in the file a symlink points to."""
    victim = tmp_path / "zshenv"
    victim.write_text("original\n")
    d = session_file.use_hosted_dir(tmp_path / "hosted" / "sess-1")
    uid = "a" * 26
    for name in (f"last-{uid}.env", f"stats-{uid}.jsonl", f"asks-{uid}.env"):
        (d / name).symlink_to(victim)
    with pytest.raises(OSError):
        session_file.write_private(d / f"last-{uid}.env", "ID=x\n")
    with pytest.raises(OSError):
        session_file.append_private(d / f"stats-{uid}.jsonl", "{}\n")
    from mattermost_ai_agent_bus import asks, ownpost

    ownpost.record(uid, "c" * 26, {"id": "p1", "message": "hi", "create_at": 1})
    stats.set_user(uid)
    stats.record("turn", by="human")
    with pytest.raises(OSError):
        asks.add(uid, "p" * 26)
    assert victim.read_text() == "original\n"
    session_file.write_private(d / "fresh.env", "A=1\n")
    assert (d / "fresh.env").read_text() == "A=1\n"
    assert ((d / "fresh.env").stat().st_mode & 0o777) == 0o600
    assert not [p for p in d.iterdir() if p.name.startswith(".")]  # no temp file left


def test_transcript_only_under_the_hosts_claude_projects(tmp_path, monkeypatch):
    """RT-6.18 (security, RT-6.9): the host reads a transcript the agent named
    only under its own ~/.claude/projects/, never through a symlink at the end."""
    monkeypatch.setenv("HOME", str(tmp_path))
    projects = tmp_path / ".claude" / "projects" / "p"
    projects.mkdir(parents=True)
    t = projects / "s.jsonl"
    t.write_text("")
    assert stats.transcript("/etc/passwd") == "/etc/passwd"  # not hosted: as before
    session_file.use_hosted_dir(tmp_path / "state" / "hosted" / "s")
    assert stats.transcript(str(t)) == os.path.realpath(t)
    assert stats.transcript("/etc/passwd") is None
    assert stats.transcript(str(projects / ".." / ".." / ".." / "x")) is None
    link = projects / "link.jsonl"
    link.symlink_to("/etc/passwd")
    assert stats.transcript(str(link)) is None


def test_endpoint_is_loopback_only():
    assert hosted.parse_endpoint("tcp://127.0.0.1:4711") == ("127.0.0.1", 4711)
    for bad in ("http://127.0.0.1:1", "tcp://10.0.0.1:1", "tcp://127.0.0.1:x", "tcp://127.0.0.1:0",
                "tcp://localhost.evil:1"):
        with pytest.raises(ValueError):
            hosted.parse_endpoint(bad)


def test_start_line_takes_key_and_bus_settings_only():
    """RT-6.18 step 1: key and token on stdin; no registration secret, no
    interpreter or loader settings, nothing multi-line."""
    line = json.dumps({"key": KEY, "env": {
        "MM_BOT_TOKEN": "tok", "MM_CHAT_URL": "https://mm", "MM_REG_SECRET": "s",
        "MM_REG_SECRET_GOPASS": "g", "PYTHONPATH": "/w", "DYLD_INSERT_LIBRARIES": "/w/x",
        "BASH_ENV": "/w/x", "UV_PROJECT": "/w", "HTTPS_PROXY": "http://p", "MM_X": "a\nb",
        "MM_BUS_KEY": "other"}}).encode() + b"\n"
    key, env = hosted.read_start(io.BytesIO(line))
    assert key == KEY
    assert env == {"MM_BOT_TOKEN": "tok", "MM_CHAT_URL": "https://mm", "HTTPS_PROXY": "http://p"}
    for bad in (b"", b'{"key": "short"}\n', b'{"key": "' + b"k" * 64 + b'"}', b"[]\n"):
        with pytest.raises(ValueError):
            hosted.read_start(io.BytesIO(bad))


def _fake_host(answer: dict, seen: list) -> int:
    srv = socket.create_server(("127.0.0.1", 0))

    def run():
        conn, _ = srv.accept()
        with conn:
            f = conn.makefile("rb")
            seen.append(json.loads(f.readline()))
            conn.sendall(json.dumps(answer).encode() + b"\n")
            if answer.get("ok"):
                while data := f.read1(65536):  # echo until the relay half-closes
                    conn.sendall(data)
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def _relay(port: int, monkeypatch, **extra):
    monkeypatch.setenv("MM_CHANNEL_PUSH", "0")
    env = {"MM_BUS_MCP_ENDPOINT": f"tcp://127.0.0.1:{port}", "MM_BUS_KEY": KEY,
           "CLAUDE_CODE_SESSION_ID": "s1", "CLAUDE_PROJECT_DIR": "/p", "HOME": "/sandbox",
           "XDG_STATE_HOME": "/sandbox/state", **extra}
    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()
    result = []
    t = threading.Thread(target=lambda: result.append(
        hosted.relay(env, stdin_fd=in_r, stdout_fd=out_w)), daemon=True)
    t.start()
    return t, result, in_w, out_r, out_w


def test_relay_pipes_stdio_after_the_handshake(monkeypatch):
    """RT-6.18 step 3: key and naming values in one line, then bytes unchanged both ways."""
    seen: list = []
    port = _fake_host({"ok": True}, seen)
    t, result, in_w, out_r, out_w = _relay(port, monkeypatch)
    msg = b'{"jsonrpc":"2.0","method":"notifications/claude/channel/permission","params":{}}\n'
    os.write(in_w, msg)
    got = b""
    while len(got) < len(msg):
        got += os.read(out_r, 65536)
    assert got == msg
    os.close(in_w)  # Claude Code closes stdin: the relay ends cleanly
    t.join(5)
    assert result == [0]
    hello = seen[0]
    assert hello["key"] == KEY
    assert hello["session"] == {"CLAUDE_CODE_SESSION_ID": "s1", "CLAUDE_PROJECT_DIR": "/p",
                                "MM_CHANNEL_PUSH": "0", "cwd": os.getcwd()}


def test_relay_fails_clearly_without_the_host(monkeypatch, capsys):
    """RT-6.18 error handling: no token-less server in the sandbox, a message and exit 1."""
    s = socket.create_server(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    t, result, *_ = _relay(port, monkeypatch)
    t.join(10)
    assert result == [1]
    assert "unreachable" in capsys.readouterr().err

    seen: list = []
    port = _fake_host({"ok": False, "error": "wrong key"}, seen)
    t, result, *_ = _relay(port, monkeypatch)
    t.join(10)
    assert result == [1]
    assert "refused the relay: wrong key" in capsys.readouterr().err


def test_entry_point_relays_when_the_endpoint_is_set(monkeypatch):
    calls = []
    monkeypatch.setattr(hosted, "relay", lambda env: calls.append(env["MM_BUS_MCP_ENDPOINT"]) or 3)
    monkeypatch.setenv("MM_BUS_MCP_ENDPOINT", "tcp://127.0.0.1:1")
    with pytest.raises(SystemExit) as exc:
        hosted.main([])
    assert exc.value.code == 3 and calls == ["tcp://127.0.0.1:1"]


async def test_hook_api_with_the_session_key_and_no_file(tmp_path):
    """RT-6.18 step 2: the host serves the hook API (RT-6.17) with the same key
    on a port of its own, before any relay, and writes no endpoint file."""
    session_file.use_hosted_dir(tmp_path / "hosted" / "s")

    class Client:
        token = "t"
        project_channel_name = ""

        async def get_me(self):
            return {"id": "a" * 26, "username": "agent-x"}

    host = hosted.Host(KEY, client=Client(), serve=_echo)

    async def body(ready):
        assert ready["hook"].startswith("http://127.0.0.1:")
        assert ready["hook"] != f"http://127.0.0.1:{ready['port']}"
        async with httpx.AsyncClient(base_url=ready["hook"]) as h:
            assert (await h.get("/v1/me")).status_code == 401
            r = await h.get("/v1/me", headers={"Authorization": f"Bearer {KEY}"})
            assert r.status_code == 200 and r.json()["username"] == "agent-x"

    await _with_host(host, tmp_path, body, key=KEY)
    assert not list((tmp_path / "hosted" / "s").glob("endpoint-*"))
    assert not list((tmp_path / "state").rglob("endpoint-*"))


def test_watchdog_ends_the_session_with_exit_work(tmp_path, monkeypatch):
    """RT-6.18 step 8: corral gone → exit work, readiness file and state dir removed."""
    work = []
    monkeypatch.setattr(hosted, "exit_work", work.append)
    d = session_file.use_hosted_dir(tmp_path / "hosted" / "s")
    checks = iter([True, False])
    why = hosted.serve_host(hosted.Host(KEY, serve=_echo, client="c"), ready_file=tmp_path / "r.json",
                            with_services=False, watch=lambda: next(checks), interval=0.01, signals=())
    assert why.startswith("watchdog") and work == ["c"]
    assert not (tmp_path / "r.json").exists() and not d.exists()


def test_sigterm_ends_the_session_with_exit_work(tmp_path, monkeypatch):
    """RT-6.18 step 7: postEnd's SIGTERM → exit work (here SIGUSR1 stands in)."""
    work = []
    monkeypatch.setattr(hosted, "exit_work", work.append)
    ready = tmp_path / "r.json"

    def kill_when_ready():
        for _ in range(500):
            if ready.exists():
                os.kill(os.getpid(), signal.SIGUSR1)
                return
            time.sleep(0.01)

    threading.Thread(target=kill_when_ready, daemon=True).start()
    why = hosted.serve_host(hosted.Host(KEY, serve=_echo), ready_file=ready, with_services=False,
                            signals=(signal.SIGUSR1,))
    assert why == "signal SIGUSR1" and work == [None]


def test_exit_work_runs_the_servers_cleanup(monkeypatch):
    """RT-6.18 step 7: the same exit work as the stdio server (BYE, stats, unregister),
    also when no relay ever loaded the server."""
    calls = []

    class Fake:
        _client = None

        @staticmethod
        def _cleanup_on_exit():
            calls.append(Fake._client)

    monkeypatch.setitem(sys.modules, "mattermost_ai_agent_bus.server", Fake)
    import mattermost_ai_agent_bus

    monkeypatch.setattr(mattermost_ai_agent_bus, "server", Fake, raising=False)
    hosted.exit_work("client")
    assert calls == ["client"]


def test_watcher_tells_a_gone_or_reused_pid():
    p = subprocess.Popen(["sleep", "30"])
    try:
        start = hosted.proc_start(p.pid)
        if start is None:
            pytest.skip("no start times on this platform")
        assert hosted.watcher(p.pid, start)()
        assert not hosted.watcher(p.pid, "sysctl:0.000000")()
    finally:
        p.kill()
        p.wait()
    assert not hosted.watcher(p.pid, start)()


def _server_text(env: dict, hosted_dir: Path | None = None) -> dict:
    code = ("import json, sys\nfrom pathlib import Path\n"
            "from mattermost_ai_agent_bus import session_file\n"
            + (f"session_file.use_hosted_dir(Path({str(hosted_dir)!r}))\n" if hosted_dir else "")
            + "from mattermost_ai_agent_bus import server\nserver._hide_lifecycle_tools()\n"
            "import asyncio\ntools = sorted(t.name for t in asyncio.run(server.mcp.list_tools()))\n"
            "print(json.dumps({'managed': server.HOST_MANAGED, 'text': server.mcp.instructions,"
            " 'tools': tools, 'newer': server._newer_installed()}))\n")
    base = {"PATH": os.environ["PATH"], "HOME": env.pop("HOME", "/nonexistent"),
            "PYTHONPATH": str(SRC), "MM_CHANNEL_PUSH": "0"}
    out = subprocess.run([sys.executable, "-c", code], env={**base, **env}, capture_output=True,
                         text=True, timeout=60, check=True)
    return json.loads(out.stdout.splitlines()[-1])


def test_host_managed_without_a_token_in_its_environment(tmp_path):
    """RT-6.18: on the host side HOST_MANAGED keys on the session, not on a
    token in the environment; the text says the token stays on the host."""
    got = _server_text({}, hosted_dir=tmp_path / "hosted" / "s")
    assert got["managed"] is True and "token stays on the host" in got["text"]
    assert "session_start" not in got["tools"] and "post_message" in got["tools"]
    assert got["newer"] is None
    older = _server_text({"MM_BOT_TOKEN": "t"})  # an older host install's session
    assert older["managed"] is True and "token stays on the host" not in older["text"]


def test_current_plugin_says_why_when_the_host_kept_the_bus_off(tmp_path):
    """RT-6.18 error handling: preStart failed closed; the plugin in the
    sandbox has no token and no relay, and says so instead of offering to register."""
    got = _server_text({"MM_BUS_UNAVAILABLE": "the MCP server did not start on the host",
                        "MM_PROJECT_CHANNEL": "proj-x"})
    assert got["managed"] is False
    assert "The bus is off in this corral session: the MCP server did not start" in got["text"]
    assert "session_start" not in got["text"] and "session_start" not in got["tools"]

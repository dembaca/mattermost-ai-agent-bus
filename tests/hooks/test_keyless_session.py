"""RT-6.18 keyless host-managed session (docs/arc42/06-runtime-view.md).

preStart starts the session's MCP server on the host and contributes its two
endpoints and a key instead of the bot token; the relay and the hooks in the
sandbox reach it with nothing else; postEnd stops it, which runs the exit work
there. Uses the real host process from mcp/.venv, so it runs under
`uv run --directory mcp pytest ../tests/hooks`.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import signal
import socket
import subprocess
import time

import pytest
from conftest import ME, PROJECT, ROOT  # noqa: F401

START = "adapters/corral/10-mm-session-start.sh"
STOP = "adapters/corral/90-mm-session-stop.sh"
PY = ROOT / "mcp" / ".venv" / "bin" / "python"
TOKEN = "bot-token-for-tests"
HOST = {"MM_BOT_TOKEN": None, "MM_BOT_USER_ID": None, "MM_TEAM_ID": None,
        "MM_CHANNEL_ID": None, "MM_TEAM": "team", "MM_CHANNEL": "agents",
        "MM_REG_SECRET": "test-secret", "MM_PROJECT_CHANNEL": "proj-x"}
# postEnd with the bot's own token from the session file, as the fake registrar wants.
END = {**HOST, "MM_REG_SECRET": None}

pytestmark = pytest.mark.skipif(not PY.exists(), reason="needs mcp/.venv: uv run --directory mcp")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _field(path, key: str) -> str:
    return next((line.split("=", 1)[1] for line in path.read_text().splitlines()
                 if line.startswith(f"{key}=")), "")


def _start(run, sid: str, **env) -> tuple[dict, int]:
    r = run(START, CORRAL_SESSION_ID=sid, **{**HOST, **env})
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert "MM_BUS_UNAVAILABLE" not in out["env"], r.stderr
    return out, int(_field(run.state_dir / f"{sid}.env", "MM_BUS_HOST_PID"))


def _wait_gone(pid: int) -> bool:
    for _ in range(100):
        if not _alive(pid):
            return True
        time.sleep(0.1)
    return False


def test_prestart_keeps_the_token_on_the_host(mm, run):
    """RT-6.18 steps 1-2: two endpoints and a key instead of the token; pid recorded."""
    out, pid = _start(run, "sess-key1")
    env = out["env"]
    assert "MM_BOT_TOKEN" not in env and "MATTERMOST_TOKEN" not in env
    assert TOKEN not in json.dumps(out)
    assert env["MM_BOT_USERNAME"].startswith("agent-") and env["MM_CHANNEL_ID"]
    port = int(env["MM_BUS_MCP_ENDPOINT"].removeprefix("tcp://127.0.0.1:"))
    assert env["MM_BUS_ENDPOINT"].startswith("http://127.0.0.1:")
    assert len(env["MM_BUS_KEY"]) == 64
    assert "token stays on the host" in out["status"][0]
    assert "no token here" in out["agentNotes"][0]
    assert _alive(pid)
    hosted = run.state_dir / "hosted" / "sess-key1"
    assert hosted.is_dir() and (hosted.stat().st_mode & 0o777) == 0o700

    # Step 4: anything without the key is refused.
    with socket.create_connection(("127.0.0.1", port), timeout=5) as s:
        s.sendall(b'{"key": "guess", "session": {}}\n')
        assert json.loads(s.makefile().readline()) == {"ok": False, "error": "wrong key"}


def _shims(tmp_path, extra: dict[str, str] | None = None) -> tuple[str, object]:
    """Every command on PATH, wrapped: it logs its argv and dumps its own
    initial environment (what another sandbox reads with KERN_PROCARGS2 on
    macOS, /proc/<pid>/environ on Linux), then runs the real one. Helper
    scripts start through `#!/usr/bin/env bash`, so their bash is wrapped, too;
    the hook itself (started by the test) is not, nor the sweep: its own
    environment is the operator's. `extra`: more commands."""
    shim = tmp_path / "shim"
    shim.mkdir()
    fake = tmp_path / "fake-bin"
    fake.mkdir()
    for name, body in (extra or {}).items():
        (fake / name).write_text(body)
        (fake / name).chmod(0o755)
    dump = tmp_path / "exec.log"
    dirs = [str(fake), *os.environ["PATH"].split(os.pathsep)]
    for d in dirs:
        try:
            names = os.listdir(d)
        except OSError:
            continue
        for name in names:
            real = os.path.join(d, name)
            if (shim / name).exists() or not name.replace(".", "").replace("-", "").replace(
                    "_", "").replace("+", "").isalnum() or not os.access(real, os.X_OK):
                continue
            (shim / name).write_text(
                '#!/bin/sh\ncase "$1" in */adapters/corral/*-mm-session-*.sh|*/bin/mm-agent-sweep.sh) ;;\n'
                f'*) {{ echo "== {name} $*"; /usr/bin/env; }} >>"{dump}" ;;\nesac\n'
                f'exec "{real}" "$@"\n')
            (shim / name).chmod(0o755)
    return os.pathsep.join([str(shim), *dirs]), dump


def _initial_env(pid: int) -> bytes | None:
    """A process's initial argv and environment as another same-user process
    reads them: /proc/<pid>/cmdline and environ, or sysctl KERN_PROCARGS2 on macOS."""
    proc = f"/proc/{pid}"
    if os.path.exists(proc):
        with open(f"{proc}/cmdline", "rb") as c, open(f"{proc}/environ", "rb") as e:
            return c.read() + e.read()
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
        size = ctypes.c_size_t(1 << 20)
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        return buf.raw[: size.value]
    except (OSError, AttributeError):
        return None


@pytest.mark.parametrize("secret_from", ["gopass", "env"])
def test_no_credential_in_any_process_environment_or_argv(mm, run, tmp_path, secret_from):
    """RT-6.18 (security, arc42 §7.3): every process preStart and postEnd start
    — jq, curl, od, the helper scripts and what they start — gets neither the
    bot token, the registration secret nor the key in its environment or argv;
    the helpers read credentials from fd 3. Also the host process's initial
    environment, read the way another sandbox would, holds none of them."""
    secret = "test-secret"
    extra = {"gopass": f"#!/bin/sh\nprintf '%s\\n' '{secret}'\n"}
    path, dump = _shims(tmp_path, extra)
    env = {**HOST, "PATH": path}
    if secret_from == "gopass":
        env |= {"MM_REG_SECRET": None, "MM_REG_SECRET_GOPASS": "bus/registrar"}
    sid = f"sess-env-{secret_from}"
    out, pid = _start(run, sid, **env)
    key = out["env"]["MM_BUS_KEY"]
    assert any(m == "POST" and p == "/register/v1/agents" for m, p, _ in mm.writes)
    assert "== jq " in dump.read_text() and "== curl " in dump.read_text()
    assert "== bash " in dump.read_text()  # the helper scripts ran, wrapped
    if secret_from == "gopass":
        assert "== gopass show -o bus/registrar" in dump.read_text()
    procargs = _initial_env(pid)
    if procargs is not None:
        assert b"mattermost_ai_agent_bus" in procargs
        for v in (TOKEN, key, secret):
            assert v.encode() not in procargs
    r = run(STOP, CORRAL_SESSION_ID=sid, **env)
    assert r.code == 0, r.stderr
    assert any(m == "DELETE" and p.startswith("/register/v1/agents/") for m, p, _ in mm.writes)
    text = dump.read_text()
    for v in (TOKEN, key, secret):
        assert v not in text
    assert "Bearer" not in text
    for f in tmp_path.rglob("*"):
        if f.is_file() and not f.is_relative_to(run.state_dir) and f.parent.name != "fake-bin":
            assert TOKEN not in f.read_text(errors="replace"), f
    assert TOKEN not in (run.state_dir / f"{sid}.host.log").read_text()


def test_planted_modules_in_the_workdir_never_run_on_the_host(mm, run, tmp_path):
    """RT-6.18 (security): preStart runs in the agent's workdir; the host
    process starts isolated (python -I, from the host tree), so a planted
    module or package there is not imported."""
    work = tmp_path / "work"
    marker = tmp_path / "pwned"
    for mod in ("argparse.py", "json.py", "sitecustomize.py", "usercustomize.py", "anyio.py"):
        (work / mod).parent.mkdir(parents=True, exist_ok=True)
        (work / mod).write_text(f"open({str(marker)!r}, 'a').write({mod!r})\n")
    pkg = work / "mattermost_ai_agent_bus"
    pkg.mkdir()
    for mod in ("__init__.py", "__main__.py", "hosted.py"):
        (pkg / mod).write_text(f"open({str(marker)!r}, 'a').write({mod!r})\n")
    out, pid = _start(run, "sess-plant", cwd=str(work), CORRAL_WORKDIR=str(work),
                      PYTHONPATH=str(work), PYTHONSTARTUP=str(work / "json.py"),
                      PYTHONUSERBASE=str(work), UV_PROJECT=str(work))
    assert _alive(pid) and out["env"]["MM_BUS_KEY"]
    assert not marker.exists()


def _tree(tmp_path, python: str | None):
    """The hooks as installed, with a stand-in for the host's Python environment."""
    tree = tmp_path / "hosttree"
    for d in ("adapters/corral", "bin", ".claude-plugin"):
        shutil.copytree(ROOT / d, tree / d)
    if python is not None:
        py = tree / "mcp" / ".venv" / "bin" / "python"
        py.parent.mkdir(parents=True)
        py.write_text(python)
        py.chmod(0o755)
    return tree


def test_fail_closed_when_the_host_process_cannot_start(mm, run, tmp_path):
    """RT-6.18 error handling: no token in the contribution, ever; the bot goes
    again and the sandbox learns why. The host process got key and token on
    stdin only, with a scrubbed environment."""
    rec = tmp_path / "rec"
    tree = _tree(tmp_path, f'#!/bin/sh\necho "$@" >"{rec}.argv"\npwd >"{rec}.cwd"\n'
                           f'env >"{rec}.env"\ncat >"{rec}.stdin"\nexit 1\n')
    r = run(str(tree / START), CORRAL_SESSION_ID="sess-fail", PYTHONPATH="/w", UV_PROJECT="/w",
            DYLD_LIBRARY_PATH="/w", MM_LEASE_SEC="600", HTTPS_PROXY="http://u:p@proxy",
            **HOST)
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert set(out["env"]) == {"MM_BUS_UNAVAILABLE"}
    assert TOKEN not in r.stdout and "MM_BUS_KEY" not in r.stdout
    assert "did not start on the host" in out["env"]["MM_BUS_UNAVAILABLE"]
    assert "bus is off" in out["agentNotes"][0] and "no bus" in out["status"][0]
    assert any(m == "DELETE" and p.startswith("/register/v1/agents/") for m, p, _ in mm.writes)
    assert not (run.state_dir / "sess-fail.env").exists()

    argv = rec.with_suffix(".argv").read_text()
    assert argv.startswith("-I -m mattermost_ai_agent_bus --host") and TOKEN not in argv
    assert rec.with_suffix(".cwd").read_text().strip() == str(tree / "mcp")
    env = dict(line.split("=", 1) for line in rec.with_suffix(".env").read_text().splitlines()
               if "=" in line)
    assert not [k for k in env if k.startswith(("PYTHON", "UV_", "DYLD_", "MM_", "BASH_ENV"))]
    assert TOKEN not in rec.with_suffix(".env").read_text()
    assert env["PATH"] == "/usr/bin:/bin:/usr/sbin:/sbin" and env["HOME"] == str(tmp_path)
    start = json.loads(rec.with_suffix(".stdin").read_text())
    assert len(start["key"]) == 64 and start["env"]["MM_BOT_TOKEN"] == TOKEN
    assert start["env"]["MM_LEASE_SEC"] == "600" and start["env"]["HTTPS_PROXY"] == "http://u:p@proxy"
    assert "MM_REG_SECRET" not in start["env"] and "test-secret" not in rec.with_suffix(".stdin").read_text()


def test_fail_closed_without_a_python_environment(mm, run, tmp_path):
    """RT-6.18 error handling: no host environment, no bot at all, and why."""
    tree = _tree(tmp_path, None)
    r = run(str(tree / START), CORRAL_SESSION_ID="sess-novenv", **HOST)
    out = json.loads(r.stdout)
    assert set(out["env"]) == {"MM_BUS_UNAVAILABLE"} and TOKEN not in r.stdout
    assert "mm-agent-host-install.sh" in out["env"]["MM_BUS_UNAVAILABLE"]
    assert not any(p == "/register/v1/agents" for _, p, _ in mm.writes)


def test_postend_stops_the_host_process(mm, run):
    """RT-6.18 step 7: SIGTERM, bounded wait; then the bot is unregistered as before."""
    _, pid = _start(run, "sess-key2")
    recorded = _field(run.state_dir / "sess-key2.env", "MM_BUS_HOST_START")
    r = run(STOP, CORRAL_SESSION_ID="sess-key2", **END)
    assert r.code == 0, r.stderr
    if recorded:
        assert _wait_gone(pid)
    else:  # ps denied (a sandbox): no start time, so no kill on a bare pid; the watchdog stops it
        assert "no start time recorded" in r.stderr and _alive(pid)
    assert not (run.state_dir / "sess-key2.host.json").exists()
    assert not (run.state_dir / "sess-key2.env").exists()
    assert not (run.state_dir / "hosted" / "sess-key2").exists()


def _readline(stream, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    buf = b""
    while not buf.endswith(b"\n"):
        left = deadline - time.monotonic()
        assert left > 0, buf
        if select.select([stream], [], [], left)[0]:
            chunk = os.read(stream.fileno(), 65536)
            assert chunk, buf
            buf += chunk
    return json.loads(buf.splitlines()[-1])


def test_relay_reaches_the_host_and_its_exit_work_unregisters(mm, run, tmp_path):
    """RT-6.18 steps 3-7: the plugin's entry point relays; the host server
    answers, keeps its files in its own state dir (a symlink planted in the
    sandbox's state dir is never followed); postEnd's SIGTERM runs the exit
    work on the host."""
    out, pid = _start(run, "sess-key3")
    sandbox = tmp_path / "sandbox-home"
    sstate = sandbox / ".local" / "state" / "mm-agent-bus"
    sstate.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_text("original\n")
    for name in (f"last-{ME}.env", f"stats-{ME}.jsonl", f"coord-{ME}.env"):
        (sstate / name).symlink_to(victim)
    relay = subprocess.Popen(
        [str(PY), "-m", "mattermost_ai_agent_bus"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, cwd=tmp_path,
        env={"PATH": os.environ["PATH"], "HOME": str(sandbox), "CLAUDE_CODE_SESSION_ID": "cc-1",
             "MM_CHANNEL_PUSH": "0", **{k: out["env"][k] for k in out["env"]}})
    try:
        def send(obj):
            relay.stdin.write(json.dumps(obj).encode() + b"\n")
            relay.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}})
        init = _readline(relay.stdout)
        assert "Its token stays on the host" in init["result"]["instructions"]
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = {t["name"] for t in _readline(relay.stdout)["result"]["tools"]}
        assert "post_message" in tools and "session_start" not in tools
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "post_message", "arguments": {"message": "HELLO from the relay"}}})
        assert _readline(relay.stdout)["id"] == 3
        relay.stdin.close()
        assert relay.wait(10) == 0, relay.stderr.read()
    finally:
        if relay.poll() is None:
            relay.kill()
    assert _alive(pid)  # a closed relay connection is not the end of the session
    assert ("POST", "/api/v4/posts") in [(m, p) for m, p, _ in mm.writes]
    assert (run.state_dir / "hosted" / "sess-key3" / f"last-{ME}.env").exists()
    assert victim.read_text() == "original\n"
    assert [p.name for p in sstate.iterdir() if not p.is_symlink()] == []

    if not _field(run.state_dir / "sess-key3.env", "MM_BUS_HOST_START"):
        # ps denied (a sandbox): postEnd does not kill a bare pid; SIGTERM as its watchdog would.
        os.kill(pid, signal.SIGTERM)
        assert _wait_gone(pid)
    r = run(STOP, CORRAL_SESSION_ID="sess-key3", **END)
    assert r.code == 0, r.stderr
    # The host server unregistered with the bot's own token (it knows the bot as
    # agent-test from /users/me); postEnd then removed the registered name again.
    assert ("DELETE", "/register/v1/agents/test") in [(m, p) for m, p, _ in mm.writes]


def test_keyless_hooks_reach_the_host_process(mm, run, tmp_path):
    """RT-6.18 step 2 (P2b): turn hooks, listener, status and status line run
    with only the contributed environment — no token, the sandbox's own state
    dir — and get everything from the host process's hook API."""
    out, pid = _start(run, "sess-hooks")
    sandbox_state = tmp_path / "sandbox-state"
    env = {**out["env"], "MM_BOT_TOKEN": None, "XDG_STATE_HOME": str(sandbox_state),
           "CORRAL_WORKDIR": "/x/myproj", "CLAUDE_CODE_SESSION_ID": "cc-hooks"}
    prompt = json.dumps({"session_id": "cc-hooks", "transcript_path": "/x/t.jsonl", "prompt": "go"})
    r = run("hooks/turn-status.sh", stdin=prompt, bus=None, **env)
    assert r.code == 0, r.stderr
    assert mm.custom_status()["text"] == "working · myproj · ~proj-x"

    mm.unread_dm(1)
    r = run("hooks/stop-inbox-check.sh", bus=None, **env)
    assert r.blocked and "wait_for_events" in r.reason
    mm.members.clear()
    mm.channels.clear()

    mm.own_post("HELLO @agent-test\nproject: x", 1000)
    mm.own_post("PROGRESS · building", 2000)
    r = run("hooks/stop-inbox-check.sh", bus=None, **env)
    assert r.blocked and "Signing off" in r.banner  # the last post, from the host

    r = run("hooks/after-compact.sh", bus=None, **env)
    assert "PROGRESS · building" in r.stdout

    mm.unread_dm(1)
    r = run("bin/mm-agent-listen.sh", "--until", str(int(time.time()) + 20), bus=None,
            MM_LISTEN_POLL_SEC="1", **env)
    assert "1 unread mention(s) or DM(s)" in r.stdout

    assert run("bin/mm-agent-status.sh", "bus", "tool", bus=None, **env).code == 0
    assert mm.custom_status()["text"] == "on the bus · tool · ~proj-x"

    hosted = run.state_dir / "hosted" / "sess-hooks"
    (hosted / f"coord-{ME}.env").write_text(f"CROWN_UNTIL={int(time.time()) + 600}\n")
    r = run("bin/mm-agent-statusline.sh", **env)
    assert r.stdout == f"👑 @{out['env']['MM_BOT_USERNAME']}"

    # The counters went to the host process's own state dir, none to the sandbox's.
    stats = (hosted / f"stats-{ME}.jsonl").read_text()
    assert '"k": "turn"' in stats and '"k": "transcript"' in stats and '"k": "wake"' in stats
    assert not list(sandbox_state.rglob("stats-*"))
    for f in tmp_path.rglob("*"):
        if f.is_file() and not f.is_relative_to(run.state_dir):
            assert TOKEN not in f.read_text(errors="replace"), f


def test_prestart_sweeps_what_ended_sessions_left(mm, run):
    """RT-6.18 error handling: readiness files and state dirs of gone sessions
    are removed; the host process of a session whose corral is gone is stopped
    (needs ps for its start time)."""
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / "gone.host.json").write_text('{"pid": 1, "port": 1}')
    old_dir = run.state_dir / "hosted" / "gone"
    old_dir.mkdir(parents=True)
    os.utime(old_dir, (time.time() - 7200, time.time() - 7200))
    orphan = subprocess.Popen(["sleep", "60"])
    dead = subprocess.Popen(["true"])
    dead.wait()
    try:
        ps = subprocess.run(["ps", "-o", "lstart=", "-p", str(orphan.pid)],
                            capture_output=True, text=True)
        start = " ".join(ps.stdout.split()) if ps.returncode == 0 else ""
    except OSError:  # a sandbox may deny ps
        start = ""
    (run.state_dir / "old.env").write_text(
        f"MM_REG_BOT_NAME=old\nMM_SESSION_PID={dead.pid}\n"
        f"MM_BUS_HOST_PID={orphan.pid}\nMM_BUS_HOST_START={start}\n")
    try:
        _start(run, "sess-key4")
        assert not (run.state_dir / "gone.host.json").exists()
        assert not old_dir.exists()
        if not start:
            pytest.skip("ps cannot read start times here (sandbox)")
        assert orphan.wait(5) == -signal.SIGTERM
    finally:
        if orphan.poll() is None:
            orphan.kill()

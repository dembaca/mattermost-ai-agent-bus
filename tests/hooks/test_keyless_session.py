"""RT-6.18 keyless host-managed session (docs/arc42/06-runtime-view.md).

preStart starts the session's MCP server on the host and contributes its two
endpoints and a key instead of the bot token; the relay and the hooks in the
sandbox reach it with nothing else; postEnd stops it, which runs the exit work
there. Uses the real host process from mcp/.venv, so it runs under
`uv run --directory mcp pytest ../tests/hooks`.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import threading
import time

import pytest
from conftest import ME, PROJECT, ROOT, VSEG  # noqa: F401

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
    environment, read the way another sandbox would, holds none of them. The
    same for a bot kept and taken over (steps 9–10): postEnd keeps it, a start
    with --continue takes it over, a plain end unregisters it."""
    secret = "test-secret"
    argf = tmp_path / "corral-args"
    argf.write_text("corral run --\n")
    extra = {"gopass": f"#!/bin/sh\nprintf '%s\\n' '{secret}'\n", "ps": _ps_script(argf)}
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

    work = tmp_path / "work"
    work.mkdir()
    corral = {"CORRAL_AGENT": "claude", "CORRAL_WORKDIR": str(work)}
    out, pid = _start(run, f"{sid}-k", **env, **corral)
    keys = [key, out["env"]["MM_BUS_KEY"]]
    _handshake(out, "cc-1")
    r = run(STOP, CORRAL_SESSION_ID=f"{sid}-k", **env, **corral)
    assert r.code == 0 and "kept " in r.stderr, r.stderr
    assert _wait_gone(pid)
    argf.write_text("corral run -- --continue\n")
    out, pid = _start(run, f"{sid}-t", **env, **corral)
    keys.append(out["env"]["MM_BUS_KEY"])
    assert out["status"][0].startswith("took over ")
    procargs = _initial_env(pid)
    if procargs is not None:
        for v in (TOKEN, *keys, secret):
            assert v.encode() not in procargs
    r = run(STOP, CORRAL_SESSION_ID=f"{sid}-t", **env)
    assert r.code == 0, r.stderr
    assert "== ps -ww -o args= -p " in dump.read_text()  # the takeover read corral's argv, wrapped

    text = dump.read_text()
    for v in (TOKEN, *keys, secret):
        assert v not in text
    assert "Bearer" not in text
    for f in tmp_path.rglob("*"):
        if f.is_file() and not f.is_relative_to(run.state_dir) and f.parent.name != "fake-bin":
            assert TOKEN not in f.read_text(errors="replace"), f
    for s in (sid, f"{sid}-k", f"{sid}-t"):
        assert TOKEN not in (run.state_dir / f"{s}.host.log").read_text()


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


def test_prestart_records_its_release_and_postend_removes_it(mm, run):
    """RT-6.28 step 8: the release the session runs, for postEnd and the installer."""
    _start(run, "sess-rel")
    assert (run.state_dir / "sess-rel.release").read_text() == f"{ROOT}\n"
    assert run(STOP, CORRAL_SESSION_ID="sess-rel", **END).code == 0
    assert not (run.state_dir / "sess-rel.release").exists()


OLD = "0.80.0-dev.0123456789ab"
DISPATCH = "adapters/corral/mm-hook-dispatch.sh"


def _releases(run, tmp_path):
    """A host copy in the RT-6.28 layout: current the real hooks, an older
    release whose hooks only leave a marker, and corral's dispatcher in
    current.hooks/."""
    host = tmp_path / "host"
    cur = host / "releases" / "0.81.0-dev.aaaaaaaaaaaa"
    for d in ("adapters/corral", "bin", ".claude-plugin"):
        shutil.copytree(ROOT / d, cur / d)
    (host / "current").symlink_to("releases/0.81.0-dev.aaaaaaaaaaaa")
    (host / "current.hooks").mkdir()
    for name in ("mm-session-start.sh", "mm-session-stop.sh"):
        shutil.copy2(ROOT / DISPATCH, host / "current.hooks" / name)
    marker = tmp_path / "old-hook.ran"
    stub = f'#!/bin/sh\necho "$0 $MM_BUS_HOOK_DISPATCH" >>"{marker}"\n'
    old = host / "releases" / OLD
    for t in (old, tmp_path / "elsewhere" / OLD, host / "releases" / "evil"):
        (t / "adapters" / "corral").mkdir(parents=True)
        for hook in ("10-mm-session-start.sh", "90-mm-session-stop.sh"):
            (t / "adapters" / "corral" / hook).write_text(stub)
            (t / "adapters" / "corral" / hook).chmod(0o755)
    (host / "releases" / "0.79.0-dev.bbbbbbbbbbbb").symlink_to(OLD)
    run.state_dir.mkdir(parents=True, exist_ok=True)
    return host.resolve(), marker


def test_postend_runs_the_release_the_session_started_with(mm, run, tmp_path):
    """RT-6.28 step 9: current moved on; corral's postEnd, the dispatcher,
    runs the stop hook of the release preStart recorded."""
    host, marker = _releases(run, tmp_path)
    rec = run.state_dir / "sess-old.release"
    rec.write_text(f"{host / 'releases' / OLD}\n")
    r = run(str(host / "current.hooks" / "mm-session-stop.sh"), CORRAL_SESSION_ID="sess-old", **END)
    assert r.code == 0, r.stderr
    assert f"with release {OLD}" in r.stderr
    assert marker.read_text() == f"{host / 'releases' / OLD}/adapters/corral/90-mm-session-stop.sh 1\n"


@pytest.mark.parametrize("recorded", ["outside", "name", "symlink", "relative", "garbage"])
def test_postend_falls_back_to_current_for_a_bad_record(mm, run, tmp_path, recorded):
    """RT-6.28 step 9: only a release directory of this layout, by its own
    name, no symlink; anything else and the current release ends it, which
    removes the record."""
    host, marker = _releases(run, tmp_path)
    path = {"outside": str(tmp_path.resolve() / "elsewhere" / OLD),
            "name": str(host / "releases" / "evil"),
            "symlink": str(host / "releases" / "0.79.0-dev.bbbbbbbbbbbb"),
            "relative": f"releases/{OLD}",
            "garbage": f"{host}/releases/{OLD}/../{OLD}"}[recorded]
    rec = run.state_dir / "sess-bad.release"
    rec.write_text(f"{path}\n")
    r = run(str(host / "current.hooks" / "mm-session-stop.sh"), CORRAL_SESSION_ID="sess-bad", **END)
    assert r.code == 0, r.stderr
    assert "ending it with the current one" in r.stderr and not marker.exists()
    assert not rec.exists()


def test_prestart_dispatches_to_current_and_fails_closed_without_a_release(mm, run, tmp_path):
    """RT-6.28 step 8: corral's preStart, the dispatcher, starts current's
    release; current pointing at no release of the layout is no bus."""
    host, marker = _releases(run, tmp_path)
    (host / "current").unlink()
    (host / "current").symlink_to(f"releases/{OLD}")
    start = str(host / "current.hooks" / "mm-session-start.sh")
    r = run(start, CORRAL_SESSION_ID="sess-d", **HOST)
    assert r.code == 0 and r.stdout == "", r.stderr
    assert marker.read_text() == f"{host / 'releases' / OLD}/adapters/corral/10-mm-session-start.sh 1\n"
    for bad in ("releases/evil", "releases/0.79.0-dev.bbbbbbbbbbbb", f"../elsewhere/{OLD}"):
        (host / "current").unlink()
        (host / "current").symlink_to(bad)
        r = run(start, CORRAL_SESSION_ID="sess-d", **HOST)
        assert r.code == 0, r.stderr
        assert set(json.loads(r.stdout)["env"]) == {"MM_BUS_UNAVAILABLE"}, bad
    assert marker.read_text().count("\n") == 1


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
    assert mm.custom_status()["text"] == f"working · myproj{VSEG} · ~proj-x"

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
    assert mm.custom_status()["text"] == f"on the bus · tool{VSEG} · ~proj-x"

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


# --- kept for `claude --continue` (RT-6.18 steps 9–10, RT-6.1 step 7) ----------

LSTART = "Mon Nov  2 10:00:00 2026"


def _fake_ps(tmp_path, args: str | None = "corral run --") -> str:
    """PATH with a `ps` that tells corral's command line (`-o args=`) from a
    file the test changes, and one fixed start time (`-o lstart=`), so these
    tests need no real ps (a sandbox denies it). args=None: unreadable."""
    d = tmp_path / "fake-ps"
    d.mkdir(exist_ok=True)
    argf = tmp_path / "corral-args"
    if args is None:
        argf.unlink(missing_ok=True)
    else:
        argf.write_text(args + "\n")
    (d / "ps").write_text(_ps_script(argf))
    (d / "ps").chmod(0o755)
    return f"{d}{os.pathsep}{os.environ['PATH']}"


def _ps_script(argf) -> str:
    return (f'#!/bin/sh\ncase "$*" in\n*args=*) exec cat "{argf}" 2>/dev/null ;;\n'
            f'*lstart=*) echo "{LSTART}" ;;\n*) exit 1 ;;\nesac\n')


def _key(value: str) -> str:
    return hashlib.sha1(value.encode()).hexdigest()[:16]


def _handshake(out: dict, claude_sid: str) -> None:
    """A relay's first line, as the plugin's entry point in the sandbox sends it."""
    port = int(out["env"]["MM_BUS_MCP_ENDPOINT"].removeprefix("tcp://127.0.0.1:"))
    with socket.create_connection(("127.0.0.1", port), timeout=10) as s:
        s.sendall(json.dumps({"key": out["env"]["MM_BUS_KEY"],
                              "session": {"CLAUDE_CODE_SESSION_ID": claude_sid}}).encode() + b"\n")
        assert json.loads(s.makefile().readline()) == {"ok": True}


def _kept(run, sid: str, *, name: str, workdir, claude_sid: str, ended_ago: float = 60,
          token: str = TOKEN, attestation: str = ""):
    """A session file postEnd kept (step 9), with its state directory."""
    run.state_dir.mkdir(parents=True, exist_ok=True)
    f = run.state_dir / f"{sid}.env"
    f.write_text(
        f"MM_REG_BOT_NAME={name}\nMM_BOT_USERNAME=agent-{name}\n"
        f"MM_AGENT_PREFIX=agent-\nMM_BOT_USER_ID={ME}\n"
        + (f"MM_BOT_ATTESTATION={attestation}\n" if attestation else "")
        + f"MM_BOT_TOKEN={token}\nMATTERMOST_TOKEN={token}\nMM_TEAM=team\nMM_CHANNEL=agents\n"
        f"MM_SERVER_ENDED={int(time.time() - ended_ago)}\n"
        f"MM_KEPT_WORKDIR={_key(os.path.realpath(workdir))}\nMM_KEPT_CLAUDE_SID={_key(claude_sid)}\n")
    f.chmod(0o600)
    d = run.state_dir / "hosted" / sid
    d.mkdir(parents=True, mode=0o700)
    (d / "asks-x.env").write_text(f"kept {sid}\n")
    return f


def _registrations(mm) -> int:
    return sum(1 for m, p, _ in mm.writes if m == "POST" and p == "/register/v1/agents")


def _deletes(mm) -> list[str]:
    return [p for m, p, _ in mm.writes if m == "DELETE" and p.startswith("/register/")]


@pytest.mark.parametrize("args, want", [
    ("corral run --", ""),
    ("corral run -- --continue", "continue"),
    ("corral run -- -c --model opus", "continue"),
    ("corral run -- --resume 4f1c-9a", "resume 4f1c-9a"),
    ("corral run -- -r 4f1c-9a", "resume 4f1c-9a"),
    ("corral run -- --resume=4f1c-9a", "resume 4f1c-9a"),
    ("corral run -- --resume", ""),                    # the picker
    ("corral run -- --resume --model opus", ""),       # the picker
    ("corral run -- --continue --fork-session", ""),   # a new session id
    ("corral run -- -c -r 4f1c-9a", ""),               # unclear
    ("corral -c run", ""),                             # corral's own flags, not the agent's
    (None, "unreadable"),
])
def test_corrals_command_line_tells_continue_and_resume(tmp_path, args, want):
    """RT-6.18 step 10: the agent's flags are the words after corral's first
    `--`; anything unclear means a fresh registration, unreadable too."""
    r = subprocess.run(
        ["bash", "-c", 'source "$1" && { mm_corral_resume 4242 || echo unreadable; }', "_",
         str(ROOT / "bin" / "mm-agent-lib.sh")],
        env={"PATH": _fake_ps(tmp_path, args), "HOME": str(tmp_path)},
        capture_output=True, text=True, timeout=30)
    assert r.stdout.strip() == want, r.stderr


def test_postend_keeps_the_bot_and_continue_takes_it_over(mm, run, tmp_path):
    """RT-6.18 steps 9–10, RT-6.1 step 7: a Claude Code session that ended by
    itself keeps its bot — SIGUSR1 to the host process (offline only), no
    unregister, the session file marked, the state directory kept. The next
    start in the workdir with --continue takes it over: same name, no
    registration, the state directory renamed, the Claude session handed to
    the host process. A later end without Claude unregisters as before."""
    work = tmp_path / "work"
    work.mkdir()
    corral = {"CORRAL_AGENT": "claude", "CORRAL_WORKDIR": str(work)}
    out, pid = _start(run, "sess-a", PATH=_fake_ps(tmp_path), **corral)
    assert "registered agent-" in out["status"][0]
    _handshake(out, "cc-1")
    hosted = run.state_dir / "hosted" / "sess-a"
    assert (hosted / "claude-sid.env").read_text() == f"MM_CLAUDE_SID={_key('cc-1')}\n"

    name = _field(run.state_dir / "sess-a.env", "MM_REG_BOT_NAME")
    r = run(STOP, CORRAL_SESSION_ID="sess-a", PATH=_fake_ps(tmp_path), **corral, **END)
    assert r.code == 0, r.stderr
    assert f"kept {name} for claude --continue" in r.stderr
    assert _wait_gone(pid)
    assert not _deletes(mm)
    assert ("PUT", f"/api/v4/users/{ME}/status") in [(m, p) for m, p, _ in mm.writes]
    f = run.state_dir / "sess-a.env"
    assert (f.stat().st_mode & 0o777) == 0o600
    assert _field(f, "MM_KEPT_WORKDIR") == _key(os.path.realpath(work))
    assert _field(f, "MM_KEPT_CLAUDE_SID") == _key("cc-1")
    assert int(_field(f, "MM_SERVER_ENDED")) <= time.time()
    assert not _field(f, "MM_BUS_HOST_PID") and not _field(f, "MM_SESSION_PID")
    assert not (run.state_dir / "sess-a.host.json").exists()
    assert hosted.is_dir()
    (hosted / "marker").write_text("from sess-a\n")

    out2, pid2 = _start(run, "sess-b", PATH=_fake_ps(tmp_path, "corral run -- --continue"), **corral)
    assert out2["status"][0].startswith(f"took over {out['env']['MM_BOT_USERNAME']}, ")
    assert out2["env"]["MM_BOT_USERNAME"] == out["env"]["MM_BOT_USERNAME"]
    assert "kept this bot from the session you resumed" in out2["agentNotes"][0]
    assert TOKEN not in json.dumps(out2)
    assert _registrations(mm) == 1
    assert not f.exists() and not hosted.exists()
    assert (run.state_dir / "hosted" / "sess-b" / "marker").read_text() == "from sess-a\n"
    f2 = run.state_dir / "sess-b.env"
    assert _field(f2, "MM_BUS_HOST_PID") == str(pid2) and not _field(f2, "MM_SERVER_ENDED")
    assert _field(f2, "MM_REG_BOT_NAME") == name and _field(f2, "MM_SESSION_PID")
    assert not list(run.state_dir.glob(".*taken*"))
    _handshake(out2, "cc-1")
    assert "took over the bot kept for this Claude session" in (
        run.state_dir / "sess-b.host.log").read_text()

    r = run(STOP, CORRAL_SESSION_ID="sess-b", PATH=_fake_ps(tmp_path), **END)
    assert r.code == 0, r.stderr
    assert _wait_gone(pid2)
    assert f"/register/v1/agents/{name}" in _deletes(mm)
    assert not f2.exists() and not (run.state_dir / "hosted" / "sess-b").exists()


@pytest.mark.parametrize("case", ["aborted", "other-agent", "no-claude-session"])
def test_postend_unregisters_when_the_session_cannot_be_resumed(mm, run, tmp_path, case):
    """RT-6.18 step 9 / RT-6.1 step 7: an aborted launch, another agent, or a
    host process that never saw a Claude session (no claude-sid.env): the bot
    is unregistered and everything goes, as before."""
    work = tmp_path / "work"
    work.mkdir()
    corral = {"CORRAL_AGENT": "pi" if case == "other-agent" else "claude",
              "CORRAL_WORKDIR": str(work)}
    out, pid = _start(run, "sess-n", PATH=_fake_ps(tmp_path), **corral)
    if case != "no-claude-session":
        _handshake(out, "cc-1")
    if case == "aborted":
        corral["CORRAL_AGENT_EXIT"] = "aborted"
    r = run(STOP, CORRAL_SESSION_ID="sess-n", PATH=_fake_ps(tmp_path), **corral, **END)
    assert r.code == 0, r.stderr
    assert "kept" not in r.stderr
    assert _wait_gone(pid)
    assert "/register/v1/agents/test" in _deletes(mm)
    assert not (run.state_dir / "sess-n.env").exists()
    assert not (run.state_dir / "hosted" / "sess-n").exists()


def test_resume_takes_over_the_bot_of_that_session(mm, run, tmp_path):
    """RT-6.18 step 10: --resume <id> takes the bot kept for that Claude
    session in this workdir, not the newest one, nor one of another workdir;
    its token and attestation reach the host process on stdin only, with the
    kept session's key."""
    work = tmp_path / "work"
    work.mkdir()
    _kept(run, "old-x", name="mac-proj-x", workdir=work, claude_sid="cc-x", ended_ago=3600,
          attestation="aaa.bbb.ccc")
    _kept(run, "old-y", name="mac-proj-y", workdir=work, claude_sid="cc-y", ended_ago=60)
    _kept(run, "old-z", name="mac-proj-z", workdir=tmp_path, claude_sid="cc-x", ended_ago=10)
    rec = tmp_path / "rec"
    tree = _tree(tmp_path, f'#!/bin/sh\ncat >"{rec}.stdin"\nexit 1\n')
    r = run(str(tree / START), CORRAL_SESSION_ID="sess-r", CORRAL_AGENT="claude",
            CORRAL_WORKDIR=str(work), PATH=_fake_ps(tmp_path, "corral run -- --resume cc-x"), **HOST)
    assert r.code == 0, r.stderr
    assert "taking over agent-mac-proj-x, kept for claude resume" in r.stderr
    start = json.loads(rec.with_suffix(".stdin").read_text())
    assert start["env"]["MM_BOT_TOKEN"] == TOKEN
    assert start["env"]["MM_BOT_ATTESTATION"] == "aaa.bbb.ccc"
    assert start["env"]["MM_BUS_KEPT_SID"] == _key("cc-x")
    assert start["env"]["MM_BOT_USERNAME"] == "agent-mac-proj-x"
    assert _registrations(mm) == 0
    assert not (run.state_dir / "old-x.env").exists()
    assert (run.state_dir / "old-y.env").exists() and (run.state_dir / "old-z.env").exists()
    # The host process did not come up: fail closed, the taken-over bot goes.
    assert "/register/v1/agents/mac-proj-x" in _deletes(mm)


@pytest.mark.parametrize("args", ["corral run --", "corral run -- --resume", None])
def test_without_continue_or_resume_a_new_bot_is_registered(mm, run, tmp_path, args):
    """RT-6.18 step 10: a plain start, the resume picker or a command line that
    cannot be read register a new bot; a kept one stays for its session."""
    work = tmp_path / "work"
    work.mkdir()
    kept = _kept(run, "old-k", name="mac-proj-k", workdir=work, claude_sid="cc-k")
    out, _ = _start(run, "sess-f", CORRAL_AGENT="claude", CORRAL_WORKDIR=str(work),
                    PATH=_fake_ps(tmp_path, args))
    assert out["status"][0].startswith("registered ")
    assert _registrations(mm) == 1
    assert kept.exists() and (run.state_dir / "hosted" / "old-k").is_dir()


def test_a_rejected_kept_token_means_a_new_bot(mm, run, tmp_path):
    """RT-6.18 step 10, error handling: the kept bot's token is rejected (401):
    the bot is gone, its file and state directory go, a new one registers."""
    work = tmp_path / "work"
    work.mkdir()
    kept = _kept(run, "old-g", name="mac-proj-g", workdir=work, claude_sid="cc-g",
                 token="revoked-token")
    out, _ = _start(run, "sess-g", CORRAL_AGENT="claude", CORRAL_WORKDIR=str(work),
                    PATH=_fake_ps(tmp_path, "corral run -- --continue"))
    assert out["status"][0].startswith("registered ")
    assert _registrations(mm) == 1
    assert not kept.exists() and not (run.state_dir / "hosted" / "old-g").exists()
    assert not list(run.state_dir.glob(".*taken*"))


def test_two_starts_at_once_only_one_takes_the_kept_bot(mm, run, tmp_path):
    """RT-6.18 step 10, error handling: the claim is one rename; of two starts
    with --continue at once, one takes the bot over, the other registers."""
    work = tmp_path / "work"
    work.mkdir()
    _kept(run, "old-c", name="mac-proj-c", workdir=work, claude_sid="cc-c")
    path = _fake_ps(tmp_path, "corral run -- --continue")
    results = {}

    def start(sid):
        results[sid] = run(START, CORRAL_SESSION_ID=sid, CORRAL_AGENT="claude",
                           CORRAL_WORKDIR=str(work), PATH=path, **HOST)

    threads = [threading.Thread(target=start, args=(sid,)) for sid in ("sess-c1", "sess-c2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for r in results.values():
        assert r.code == 0, r.stderr
    statuses = sorted(json.loads(r.stdout)["status"][0].split(",")[0] for r in results.values())
    assert statuses[0].startswith("registered agent-")
    assert statuses[1] == "took over agent-mac-proj-c"
    assert _registrations(mm) == 1
    assert not (run.state_dir / "old-c.env").exists()
    assert not list(run.state_dir.glob(".*taken*"))


def test_prestart_reaps_kept_bots_past_their_grace(mm, run, tmp_path):
    """RT-6.18 step 10 / RT-6.1 step 7: a kept bot whose session did not
    resume within MM_SESSION_KEEP_SEC is unregistered with its own token by
    the next start, its file and state directory removed; one within the
    grace period stays."""
    work = tmp_path / "work"
    work.mkdir()
    old = _kept(run, "old-r", name="mac-proj-r", workdir=work, claude_sid="cc-r",
                ended_ago=2 * 86400)
    fresh = _kept(run, "old-s", name="mac-proj-s", workdir=work, claude_sid="cc-s")
    _start(run, "sess-r")
    assert "/register/v1/agents/mac-proj-r" in _deletes(mm)
    assert not old.exists() and not (run.state_dir / "hosted" / "old-r").exists()
    assert fresh.exists() and (run.state_dir / "hosted" / "old-s").is_dir()


def test_a_state_dir_a_sandbox_can_write_is_refused(mm, run, tmp_path):
    """RT-6.18 step 9, security: the session files hold the token, a kept one
    for up to a day: both hooks refuse a state directory in the workdir."""
    work = tmp_path / "work"
    (work / "state").mkdir(parents=True)
    r = run(START, CORRAL_SESSION_ID="sess-w", CORRAL_WORKDIR=str(work),
            XDG_STATE_HOME=str(work / "state"), **HOST)
    out = json.loads(r.stdout)
    assert set(out["env"]) == {"MM_BUS_UNAVAILABLE"}
    assert "state directory" in out["env"]["MM_BUS_UNAVAILABLE"]
    assert _registrations(mm) == 0
    r = run(STOP, CORRAL_SESSION_ID="sess-w", CORRAL_WORKDIR=str(work),
            XDG_STATE_HOME=str(work / "state"), **END)
    assert r.code == 0 and "state directory" in r.stderr


def _versioned(tmp_path, version: str | None):
    """A host copy whose Python environment says it is `version` (None: it says nothing)."""
    say = f"echo {version}" if version else "true"
    return _tree(tmp_path, f'#!/bin/sh\ncase "$1 $2" in "-I -c") {say};; *) cat >/dev/null; exit 1;; esac\n')


def test_a_stale_host_environment_is_no_bus(mm, run, tmp_path):
    """RT-6.10 step 1 (ADR-007 amendment step 3), RT-6.18 step 1: the host copy's
    Python environment is not the host copy's version: no bot is registered, the
    sandbox learns why, and the reinstall."""
    tree = _versioned(tmp_path, "0.42.2")
    r = run(str(tree / START), CORRAL_SESSION_ID="sess-stale", **HOST)
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert set(out["env"]) == {"MM_BUS_UNAVAILABLE"} and TOKEN not in r.stdout
    why = out["env"]["MM_BUS_UNAVAILABLE"]
    assert why.startswith("stale: the Python environment of the host copy") and "is 0.42.2" in why
    assert "mm-agent-host-install.sh" in why
    assert not any(p == "/register/v1/agents" for _, p, _ in mm.writes)


@pytest.mark.parametrize("says", ["current", "newer", None], ids=["current", "newer", "unreadable"])
def test_a_current_or_unreadable_host_environment_is_not_refused(mm, run, tmp_path, says):
    """RT-6.18 step 1: an environment of the copy's version, or one that says nothing (no check), goes on."""
    version = None
    if says in ("current", "newer"):
        version = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]
        if says == "newer":
            version = "99.0.0"  # only an older environment is stale
    tree = _versioned(tmp_path, version)
    r = run(str(tree / START), CORRAL_SESSION_ID="sess-ok", **HOST)
    why = json.loads(r.stdout)["env"].get("MM_BUS_UNAVAILABLE", "")
    assert not why.startswith("stale:") and "did not start on the host" in why  # the stub's host process exits at once

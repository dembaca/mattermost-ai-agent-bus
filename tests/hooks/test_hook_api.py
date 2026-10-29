"""RT-6.17 the turn hooks and the listener ask the MCP server, not Mattermost
(docs/arc42/06-runtime-view.md). The fixture runs them without the bot token
in their environment, so every hook test checks that they need none."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import CHANNEL, ME, PROJECT, ROOT

STOP = "hooks/stop-inbox-check.sh"
PROMPT = "hooks/turn-status.sh"
LISTEN = "bin/mm-agent-listen.sh"
COMPACT = "hooks/after-compact.sh"


def test_hooks_find_the_server_by_its_file(mm, run):
    """RT-6.17 step 2: without MM_BUS_ENDPOINT / MM_BUS_KEY a hook reads the
    server's endpoint file, keyed on the Claude Code session."""
    mm.unread_dm(2)
    r = run(STOP, bus="file", CLAUDE_CODE_SESSION_ID="sess-17")
    assert r.blocked and "2 unread message(s)" in r.reason
    files = sorted(p.name for p in run.state_dir.glob("endpoint-*"))
    assert files == []  # the server removed it when it ended
    assert run(STOP, bus=None, CLAUDE_CODE_SESSION_ID="sess-17").passed


def test_a_wrong_key_is_no_session(mm, run):
    """RT-6.17 step 1: a request without the session's key gets 401; the hook
    treats it as no bus session and stays quiet."""
    mm.unread_dm(1)
    r = run(STOP, MM_BUS_ENDPOINT="http://127.0.0.1:9", MM_BUS_KEY="x", bus=None)
    assert r.passed and r.stdout == ""


def test_the_key_never_shows_in_curls_argv(mm, run, tmp_path):
    """RT-6.17 step 1: the key goes to curl on stdin, so `ps` never shows it."""
    shim = tmp_path / "shim"
    shim.mkdir()
    log = tmp_path / "argv.log"
    real = next(os.path.join(d, "curl") for d in os.environ["PATH"].split(os.pathsep)
                if os.access(os.path.join(d, "curl"), os.X_OK))
    (shim / "curl").write_text(f'#!/bin/sh\necho "$@" >>"{log}"\nexec "{real}" "$@"\n')
    (shim / "curl").chmod(0o755)
    mm.own_post("PROGRESS · building")
    r = run(STOP, PATH=f"{shim}{os.pathsep}{os.environ['PATH']}")
    assert r.blocked and "Signing off" in r.banner
    # The hook's own calls; the server's shell CLI runs (with its token) share the PATH here.
    hook = [line for line in log.read_text().splitlines() if "/v1/" in line]
    assert any("/v1/me" in line for line in hook) and any("/v1/last-post" in line for line in hook)
    assert not any("Bearer" in line for line in hook)


def test_listener_and_prompt_hook_through_the_server(mm, run):
    """RT-6.17 step 3: presence and the listener's wake-ups go through the
    server, which holds the token."""
    assert run(PROMPT, CORRAL_WORKDIR="/x/myproj").code == 0
    assert mm.custom_status()["text"] == "working · myproj · ~agents"
    mm.unread_dm(1)
    r = run(LISTEN, "--until", str(int(time.time()) + 20), MM_LISTEN_POLL_SEC="1",
            MM_PROJECT_CHANNEL_ID=PROJECT)
    assert "1 unread mention(s) or DM(s)" in r.stdout
    assert (run.state_dir / f"listen-{ME}.env").exists()


def _curl_shim(tmp_path):
    """A curl that logs its argv, then runs the real one: what `ps` would show."""
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    log = tmp_path / "argv.log"
    real = next(os.path.join(d, "curl") for d in os.environ["PATH"].split(os.pathsep)
                if os.access(os.path.join(d, "curl"), os.X_OK))
    (shim / "curl").write_text(f'#!/bin/sh\necho "$@" >>"{log}"\nexec "{real}" "$@"\n')
    (shim / "curl").chmod(0o755)
    return f"{shim}{os.pathsep}{os.environ['PATH']}", log


def test_the_bot_token_never_shows_in_curls_argv(mm, run, tmp_path):
    """RT-6.17 step 1: the shell CLI hands the bot token to curl on stdin too
    (mm_curl_auth), like mm_bus the key: mm_api, and mm-agent-post.sh holds
    only the hook API's key (RT-6.17)."""
    path, log = _curl_shim(tmp_path)
    script = tmp_path / "api.sh"
    script.write_text(f'source "{ROOT}/bin/mm-agent-lib.sh"\n'
                      'mm_api GET /api/v4/users/me; echo "status=$MM_API_STATUS"\n')
    r = run(str(script), PATH=path)
    assert "status=200" in r.stdout   # the header got through
    r = run("bin/mm-agent-post.sh", CHANNEL, "hello", PATH=path)
    assert r.code == 0 and '"message": "hello"' in r.stdout and ("POST", "/api/v4/posts") in [w[:2] for w in mm.writes]
    argv = log.read_text()
    assert "/api/v4/users/me" in argv and "/v1/post" in argv
    assert "bot-token-for-tests" not in argv and "Bearer" not in argv


# --- a hook API that misbehaves (RT-6.17 error handling) ------------------------

IN_PROJECT = {"MM_PROJECT_CHANNEL_ID": PROJECT}
HOOKS = (STOP, PROMPT, COMPACT)


@contextlib.contextmanager
def bad_api(mode: str):
    """A hook API answering 502 / 503, garbage, or slowly ("slow": every
    route; "slow-but-me": all but /v1/me)."""
    me = json.dumps({"user_id": ME, "username": "agent-me", "project": True, "crown_until": 0})

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _answer(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                self.rfile.read(n)
            is_me = self.path == "/v1/me"
            if mode == "slow" or (mode == "slow-but-me" and not is_me):
                time.sleep(15)
            if mode in ("502", "503"):
                code, body = int(mode), '{"error": "x"}'
            elif mode == "garbage":
                code, body = 200, "{nope"
            else:   # garbage-but-me, slow-but-me
                code, body = 200, me if is_me else "{nope"
            data = body.encode()
            try:
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError:
                pass

        do_GET = do_POST = _answer

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        yield {"MM_BUS_ENDPOINT": f"http://127.0.0.1:{srv.server_address[1]}", "MM_BUS_KEY": "k"}
    finally:
        srv.shutdown()


@pytest.mark.parametrize("mode", ["502", "503", "garbage", "garbage-but-me"])
def test_a_failing_server_leaves_every_hook_quiet(mm, run, mode):
    """RT-6.17 error handling, RT-6.4: 502, 503 or malformed JSON — every hook
    exits 0 and prints nothing."""
    mm.unread_dm(1)
    mm.own_post("PROGRESS · building")
    with bad_api(mode) as ep:
        for hook in HOOKS:
            r = run(hook, bus=None, **ep, **IN_PROJECT)
            assert r.code == 0 and r.stdout == "", (hook, r.stdout, r.stderr)


def test_a_slow_server_costs_each_hook_at_most_its_budget(mm, run):
    """RT-6.17 error handling: each call ends after MM_BUS_MAX_TIME (default
    3 s), and all calls of one hook share a deadline within its timeout
    (prompt hook 4 s of 5, Stop hook 8 s of 10) — however many it makes."""
    with bad_api("slow") as ep:
        for hook, budget in ((PROMPT, 4), (COMPACT, 8)):
            t = time.monotonic()
            r = run(hook, bus=None, **ep, **IN_PROJECT)
            assert r.code == 0 and r.stdout == "" and time.monotonic() - t < budget + 1.5, hook
    with bad_api("slow-but-me") as ep:
        t = time.monotonic()
        r = run(STOP, bus=None, MM_BUS_MAX_TIME="20", **ep, **IN_PROJECT)
        assert r.code == 0 and r.stdout == "" and time.monotonic() - t < 9.5


# --- the prompt hook's local steps need no server (RT-6.17 error handling) ------

def _turn_with_listener(run):
    d = run.state_dir
    d.mkdir(parents=True, exist_ok=True)
    sleeper = subprocess.Popen(["sleep", "30"])
    (d / f"turn-{ME}.env").write_text("DRAINS=1\nSIGNOFF_ASKED=1\nBY_BUS=0\n")
    (d / f"listen-{ME}.env").write_text(
        f"UNTIL={int(time.time()) + 300}\nSINCE=0\nASKED_AT=0\nSTARTED=1\nPID={sleeper.pid}\nENDED=\n")
    return sleeper


@pytest.mark.parametrize("uid_from", ["env", "endpoint file"])
def test_the_prompt_hook_clears_the_turn_without_a_server(mm, run, uid_from):
    """RT-6.17 error handling, RT-6.3 step 1: with the MCP server gone the
    prompt hook still stops the listener and removes the last turn's state —
    by the bot id from the environment, else from the server's endpoint file —
    so the next Stop hook does not skip drain and sign-off."""
    sleeper = _turn_with_listener(run)
    env = {"CLAUDE_CODE_SESSION_ID": "sess-gone"}
    if uid_from == "endpoint file":
        key = hashlib.sha1(b"sess-gone").hexdigest()[:16]
        (run.state_dir / f"endpoint-sid-{key}.env").write_text(
            f"MM_BUS_ENDPOINT=http://127.0.0.1:9\nMM_BUS_KEY=x\nMM_BUS_PID=999999\nMM_BOT_USER_ID={ME}\n")
        env["MM_BOT_USER_ID"] = None
    r = run(PROMPT, bus=None, **env)
    assert r.code == 0 and r.stdout == ""
    assert sleeper.wait(timeout=5) != 0
    assert not (run.state_dir / f"turn-{ME}.env").exists()
    assert not (run.state_dir / f"listen-{ME}.env").exists()
    assert mm.writes == []

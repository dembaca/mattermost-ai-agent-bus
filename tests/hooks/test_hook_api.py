"""RT-6.17 the turn hooks and the listener ask the MCP server, not Mattermost
(docs/arc42/06-runtime-view.md). The fixture runs them without the bot token
in their environment, so every hook test checks that they need none."""

from __future__ import annotations

import os
import time

from conftest import ME, PROJECT

STOP = "hooks/stop-inbox-check.sh"
PROMPT = "hooks/turn-status.sh"
LISTEN = "bin/mm-agent-listen.sh"


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
    assert mm.custom_status()["text"] == "working · myproj"
    mm.unread_dm(1)
    r = run(LISTEN, "--until", str(int(time.time()) + 20), MM_LISTEN_POLL_SEC="1",
            MM_PROJECT_CHANNEL_ID=PROJECT)
    assert "1 unread mention(s) or DM(s)" in r.stdout
    assert (run.state_dir / f"listen-{ME}.env").exists()

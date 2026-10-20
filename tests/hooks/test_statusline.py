"""mm-agent-statusline.sh: the session's bot name for the status line (RT-6.10 step 3)."""

from __future__ import annotations

import hashlib
import json
import time

from conftest import ME

SCRIPT = "bin/mm-agent-statusline.sh"


def test_host_managed_session_shows_its_name(run):
    r = run(SCRIPT, MM_BOT_USERNAME="agent-x-1")
    assert r.stdout == "@agent-x-1"


def test_agent_managed_session_is_found_by_session_id(run):
    d = run.state_dir
    d.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(b"sess-1").hexdigest()[:16]
    (d / f"mcp-sid-{key}.env").write_text(
        f"MM_BOT_USERNAME=agent-y-2\nMM_BOT_USER_ID={ME}\nMM_BOT_TOKEN=secret\n")
    (d / f"coord-{ME}.env").write_text(f"CROWN_UNTIL={int(time.time()) + 600}\n")
    r = run(SCRIPT, stdin=json.dumps({"session_id": "sess-1"}),
            MM_BOT_USERNAME=None, MM_BOT_USER_ID=None)
    assert r.stdout == "👑 @agent-y-2" and "secret" not in r.stdout


def test_off_the_bus_prints_nothing(run):
    r = run(SCRIPT, stdin=json.dumps({"session_id": "none"}),
            MM_BOT_USERNAME=None, MM_BOT_USER_ID=None)
    assert r.code == 0 and r.stdout == ""

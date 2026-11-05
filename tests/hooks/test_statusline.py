"""mm-agent-statusline.sh: the session's bot name for the status line (RT-6.10 step 3)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
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


# --- the tap: the numbers of the status JSON go to the MCP server (RT-6.10 step 3, ADR-008) ---

class _Usage:
    """A fake hook API that records the POST /v1/usage bodies it gets."""

    def __init__(self, *, hang: bool = False):
        self.bodies: list[dict] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
                if hang:
                    time.sleep(5)
                outer.bodies.append({"path": self.path, "auth": self.headers.get("Authorization"), **body})
                self.send_response(200)
                self.send_header("Content-Length", "11")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.env = {"MM_BUS_ENDPOINT": f"http://127.0.0.1:{self.srv.server_address[1]}",
                    "MM_BUS_KEY": "k" * 8}
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def wait(self, n: int, timeout: float = 4.0) -> list[dict]:
        end = time.time() + timeout
        while len(self.bodies) < n and time.time() < end:
            time.sleep(0.05)
        return self.bodies

    def quiet(self, n: int) -> None:
        time.sleep(0.6)  # long enough for a detached curl to have arrived
        assert len(self.bodies) == n


@pytest.fixture
def api():
    fake = _Usage()
    yield fake
    fake.srv.shutdown()


def _status(ctx=40.0, five=None, seven=None, size=200000):
    out = {"session_id": "s", "context_window": {"used_percentage": ctx, "context_window_size": size},
           "model": {"id": "m"}, "cost": {"total_cost_usd": 1.5}}
    if five is not None:
        out["rate_limits"] = {"five_hour": {"used_percentage": five, "resets_at": 1900000000}}
        if seven is not None:
            out["rate_limits"]["seven_day"] = {"used_percentage": seven, "resets_at": 1900500000}
    return json.dumps(out)


def test_tap_reports_numbers_only_and_output_is_unchanged(run, api):
    """RT-6.10 step 3: name and 👑 as before; ctx, windows and the cost sent, not the model."""
    r = run(SCRIPT, stdin=_status(62.5, 40, 12), MM_BOT_USERNAME="agent-x-1", **api.env)
    assert r.code == 0 and r.stdout == "@agent-x-1" and r.stderr == ""
    (got,) = api.wait(1)
    assert got["path"] == "/v1/usage" and got["auth"] == "Bearer " + "k" * 8
    assert {k: v for k, v in got.items() if k not in ("path", "auth")} == {
        "ctx": 62.5, "ctx_size": 200000, "cost_usd": 1.5,
        "five_hour": {"used": 40, "resets_at": 1900000000},
        "seven_day": {"used": 12, "resets_at": 1900500000}}


def test_tap_without_rate_limits_sends_ctx_only(run, api):
    """RT-6.10 step 3: null and missing fields are left out."""
    stdin = json.dumps({"context_window": {"used_percentage": 10, "context_window_size": None},
                        "rate_limits": {"five_hour": {"used_percentage": None}}})
    run(SCRIPT, stdin=stdin, MM_BOT_USERNAME="agent-x-1", **api.env)
    (got,) = api.wait(1)
    assert {k: v for k, v in got.items() if k not in ("path", "auth")} == {"ctx": 10}


def test_tap_sends_the_cost_where_there_are_no_limits(run, api):
    """RT-6.10 step 3 (ADR-008 amendment): behind a gateway the JSON has the cost and no
    rate_limits; the cumulative cost goes along, a negative or missing one does not."""
    stdin = json.dumps({"context_window": {"used_percentage": 10}, "cost": {"total_cost_usd": 4.2}})
    run(SCRIPT, stdin=stdin, MM_BOT_USERNAME="agent-x-1", **api.env)
    (got,) = api.wait(1)
    assert {k: v for k, v in got.items() if k not in ("path", "auth")} == {"ctx": 10, "cost_usd": 4.2}


def test_tap_drops_a_negative_cost(run, api):
    stdin = json.dumps({"context_window": {"used_percentage": 10}, "cost": {"total_cost_usd": -1}})
    run(SCRIPT, stdin=stdin, MM_BOT_USERNAME="agent-x-1", **api.env)
    (got,) = api.wait(1)
    assert "cost_usd" not in got


def test_tap_throttle_ten_cents(run, api):
    """RT-6.10 step 3: a cost that moved by less than $0.10 sends nothing within 60 s, $0.10 does."""
    env = {"MM_BOT_USERNAME": "agent-x-1", **api.env}
    def at(cost):
        return json.dumps({"context_window": {"used_percentage": 10}, "cost": {"total_cost_usd": cost}})
    run(SCRIPT, stdin=at(1.00), **env)
    api.wait(1)
    run(SCRIPT, stdin=at(1.05), **env)
    api.quiet(1)
    run(SCRIPT, stdin=at(1.12), **env)
    assert api.wait(2)[1]["cost_usd"] == 1.12


def test_tap_sends_nothing_without_numbers(run, api):
    run(SCRIPT, stdin=json.dumps({"session_id": "s"}), MM_BOT_USERNAME="agent-x-1", **api.env)
    api.quiet(0)


def test_usage_only_prints_nothing_and_still_reports(run, api):
    """RT-6.10 step 3: --usage-only, the line a status line script of its own calls."""
    r = run(SCRIPT, "--usage-only", stdin=_status(33), MM_BOT_USERNAME="agent-x-1", **api.env)
    assert r.code == 0 and r.stdout == "" and r.stderr == ""
    assert api.wait(1)[0]["ctx"] == 33


def test_tap_throttle_one_point_or_a_minute(run, api):
    """RT-6.10 step 3: a change under one point sends nothing within 60 s, one of a
    point or more sends, and so does a minute without."""
    env = {"MM_BOT_USERNAME": "agent-x-1", **api.env}
    run(SCRIPT, stdin=_status(40, 10), **env)
    api.wait(1)
    run(SCRIPT, stdin=_status(40.6, 10.4), **env)
    api.quiet(1)
    run(SCRIPT, stdin=_status(41.2, 10.4), **env)  # 1.2 from the last SENT value
    assert len(api.wait(2)) == 2 and api.bodies[1]["ctx"] == 41.2
    run(SCRIPT, stdin=_status(41.2, 11.4), **env)  # a window moved a point
    assert len(api.wait(3)) == 3
    f = run.state_dir / "usage-sent-agent-x-1.json"
    state = json.loads(f.read_text())
    assert state["ctx"] == 41.2 and "token" not in f.read_text()
    state["at"] -= 61
    f.write_text(json.dumps(state))
    run(SCRIPT, stdin=_status(41.2, 11.4), **env)
    assert len(api.wait(4)) == 4


def test_tap_sends_nothing_when_the_throttle_file_cannot_be_written(run, api):
    """Review of #161: no state file, no throttle: not one call a second."""
    run.state_dir.mkdir(parents=True, exist_ok=True)
    run.state_dir.chmod(0o500)  # nothing can be written there
    try:
        r = run(SCRIPT, stdin=_status(40, 10), MM_BOT_USERNAME="agent-x-1", **api.env)
        assert r.code == 0 and r.stdout == "@agent-x-1"
        api.quiet(0)
    finally:
        run.state_dir.chmod(0o700)


def test_tap_never_waits_for_a_dead_endpoint(run):
    """RT-6.10 step 3: a server that does not answer, or is not there, costs
    the status line nothing: prompt, exit 0, the usual output."""
    hang = _Usage(hang=True)
    try:
        for env in (hang.env, {"MM_BUS_ENDPOINT": "http://127.0.0.1:9", "MM_BUS_KEY": "k"}):
            t = time.time()
            r = run(SCRIPT, stdin=_status(50), MM_BOT_USERNAME="agent-x-1", **env)
            assert r.code == 0 and r.stdout == "@agent-x-1" and r.stderr == ""
            assert time.time() - t < 1.0
    finally:
        hang.srv.shutdown()


def test_tap_finds_the_server_by_its_endpoint_file(run, api):
    """RT-6.17: no MM_BUS_ENDPOINT in the status line's environment, the endpoint
    file of the session (keyed on the JSON's session_id) tells."""
    run.state_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(b"s").hexdigest()[:16]
    (run.state_dir / f"endpoint-sid-{key}.env").write_text(
        f"MM_BUS_ENDPOINT={api.env['MM_BUS_ENDPOINT']}\nMM_BUS_KEY={api.env['MM_BUS_KEY']}\n")
    run(SCRIPT, stdin=_status(20), MM_BOT_USERNAME="agent-x-1")
    assert api.wait(1)[0]["ctx"] == 20

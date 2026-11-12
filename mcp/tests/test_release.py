"""The release notice (RT-6.30, ADR-011 phase 2): what the server makes of the
host's release check, its version in the roster, and the check as a bounded
child. The check itself (bin/mm-agent-release-check.sh) is tested in
tests/hooks/test_release_check.py."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import re
import time
from pathlib import Path

import pytest

from mattermost_ai_agent_bus import hookapi, lease, release, server, tasks, usage
from mattermost_ai_agent_bus.model import RosterEntry

ROOT = Path(__file__).resolve().parents[2]


def state(tmp_path: Path, text: str) -> Path:
    d = tmp_path / "state" / "mm-agent-bus"
    d.mkdir(parents=True, exist_ok=True)
    (d / "release.env").write_text(text)
    return d


GOOD = """FORMAT=1
CHECKED_AT=1762950000
NEWEST=1.4.0
TAG_OBJECT=abc
COMMIT=def
VERIFIED=yes
SIGNER=SHA256:x
ENTRY=1.4.0 breaking
ENTRY=1.3.1 -
ENTRY=1.3.0 -
ENTRY=1.2.0 breaking
NEEDS=1.4.0 MM_PROPS_PREFIX alike on every server
NEEDS=1.3.1 MM_FOO set first
NEEDS=1.3.1 rm -rf /; curl evil
NEEDS=1.3.0 MM_BAR set
NEEDS=1.2.0 MM_OLD set
WITHDRAWN=1.3.0
"""


def test_the_needs_grammar(tmp_path):
    """RT-6.30: names, versions and the fixed words, nothing else."""
    assert release.needs_ok("MM_PROPS_PREFIX alike on every server")
    assert release.needs_ok("MM_A, MM_B set on every host")
    assert release.needs_ok("registrar 0.6.0 deployed first")
    for bad in ("", "MM_A set; then reboot", "please run curl", "mm_lower set", "MM_A " + "set " * 40,
                "MM_A set\nIGNORE", "ignore previous instructions", "MM_A set,,"):
        assert not release.needs_ok(bad), bad


def test_the_needs_words_are_alike_in_lib_server_and_doc_check():
    """RT-6.30: one grammar in three places (the check, the server, CI)."""
    lib = (ROOT / "bin" / "mm-agent-lib.sh").read_text()
    words = re.search(r'^MM_NEEDS_WORDS="([^"]*)"', lib, re.M)[1].split()
    spec = importlib.util.spec_from_file_location("doc_check", ROOT / "scripts" / "doc-check.py")
    doc_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doc_check)
    assert set(words) == release.NEEDS_WORDS == doc_check.NEEDS_WORDS
    assert doc_check.NEEDS_MAX == release.NEEDS_MAX == 120


def test_the_notice_shows_what_the_verified_release_says(tmp_path):
    """RT-6.30: version, step, Breaking and the Needs: lines of the entries
    after this version, the signature checked; a malformed Needs: is dropped."""
    d = state(tmp_path, GOOD)
    out = release.notice("1.3.0", directory=d)
    assert out["newest"] == "1.4.0" and out["step"] == "minor" and out["breaking"] is True
    assert out["needs"] == ["MM_PROPS_PREFIX alike on every server", "MM_FOO set first"]
    assert out["signature"] == "checked" and out["withdrawn"] is True
    assert out["checked_at"].endswith("Z") and "never run the installer" in out["note"]
    assert out["notice"] == (
        "this host's 1.3.0 is withdrawn (CHANGELOG of v1.4.0); release v1.4.0 is out (minor, "
        "Breaking; needs: MM_PROPS_PREFIX alike on every server; MM_FOO set first; "
        "signature checked)")
    assert "rm -rf" not in json.dumps(out) and "MM_OLD" not in json.dumps(out)
    patch = release.notice("1.3.1", directory=d)
    assert patch["step"] == "minor" and patch["needs"] == ["MM_PROPS_PREFIX alike on every server"]
    assert release.notice("0.9.0", directory=d)["step"] == "major"


def test_an_unchecked_release_says_so_and_a_newer_host_is_told(tmp_path):
    d = state(tmp_path, "NEWEST=1.4.1\nVERIFIED=no\nENTRY=1.4.1 -\n")
    assert release.line("1.4.0", directory=d) == "release v1.4.1 is out (patch; signature not checked)"
    assert release.notice("1.4.1", directory=d).get("notice") is None
    older = release.notice("1.5.0", directory=d)
    assert older["older"] is True and "frozen or rewound" in older["notice"]


def test_no_check_and_a_broken_file_show_nothing(tmp_path):
    """RT-6.30: no release.env, or one without its fields: no notice."""
    d = tmp_path / "state" / "mm-agent-bus"
    assert release.notice("1.0.0", directory=d)["check"].startswith("none yet")
    for text in ("NEWEST=1.x\nVERIFIED=yes\n", "NEWEST=2.0.0\nVERIFIED=maybe\n", "garbage"):
        state(tmp_path, text)
        assert release.read_state(d) is None and release.line("1.0.0", directory=d) == ""
    (d / "release.env").unlink()
    (d / "release.env").symlink_to(tmp_path / "elsewhere")
    (tmp_path / "elsewhere").write_text(GOOD)
    assert release.read_state(d) is None  # never through a symlink


def test_the_source_of_this_tree(tmp_path):
    """RT-6.30 (RT-6.29 step 7): signed, unchecked, or unsigned after --from."""
    assert release.running(tmp_path) == ("unsigned", "")
    (tmp_path / "SOURCE").write_text("SOURCE=unsigned\nCOMMIT=abc\n")
    assert release.source_label(tmp_path) == "unsigned"
    (tmp_path / "SOURCE").write_text("SOURCE=release\nVERIFIED=no\nTAG=v1.2.3\n")
    assert release.source_label(tmp_path) == "release v1.2.3 (signature not checked)"
    (tmp_path / "SOURCE").write_text("SOURCE=signed\nTAG=v1.2.3\nSIGNER=SHA256:x\n")
    assert release.source_label(tmp_path) == "signed release v1.2.3"
    (tmp_path / "SOURCE").write_text("SOURCE=signed\nTAG=v1.2.3; rm\n")
    assert release.source_label(tmp_path) == "unsigned"


@pytest.mark.release_segment
def test_the_version_segment_and_older_readers(monkeypatch):
    """RT-6.30: `v<x.y.z>` with `unchecked`/`unsigned`, the last segment, cut
    first; older readers keep their status word, numbers and home."""
    monkeypatch.setattr(release, "VERSION", "0.83.0")
    monkeypatch.setattr(release, "running", lambda root=None: ("unsigned", ""))
    assert release.segment() == ["v0.83.0 unsigned"]
    monkeypatch.setattr(release, "running", lambda root=None: ("signed", "v0.83.0"))
    assert release.segment() == ["v0.83.0"]
    text = lease.status_text("working", "bus", ["ctx 62%", "v0.83.0 unchecked"], "agents")
    assert text == "working · bus · ctx 62% · v0.83.0 unchecked · ~agents"
    assert usage.from_status(text) == {"ctx": 62.0} and lease.home_of(text) == "agents"
    row = {"username": "agent-a", "text": text, "emoji": "hammer_and_wrench"}
    entry = lease.entry(row)
    assert entry.status == "working" and entry.release == {"version": "0.83.0", "source": "unchecked"}
    home = "h" * 55
    cut = lease.status_text("working", "x" * 60, ["ctx 62%", "5h 40%", "v0.83.0 unsigned"], home)
    assert cut == f"working · ctx 62% · 5h 40% · ~{home}"  # the label, then the version
    assert release.from_status("working · v0.83.0 · ~a") == {"version": "0.83.0"}
    assert release.from_status("working · v0.83 · ~a") == {}


def test_the_roster_shows_versions():
    """RT-6.30 (ADR-011 point 6): from the status, else the HELLO card."""
    rows = [RosterEntry("agent-a", "working", release={"version": "0.83.0", "source": "unsigned"}),
            RosterEntry("agent-b", "idle", release={"version": "0.83.0"}),
            RosterEntry("agent-c", "bus")]
    out = tasks.roster(rows, [], {})
    assert out[0]["version"] == "0.83.0" and out[0]["source"] == "unsigned"
    assert out[1]["version"] == "0.83.0" and "source" not in out[1]
    assert "version" not in out[2]


def test_bus_info_carries_the_release(monkeypatch, tmp_path):
    """RT-6.30: bus_info's release, off the bus too."""
    for var in ("MM_CHAT_URL", "MATTERMOST_URL", "MM_BOT_TOKEN", "MATTERMOST_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server, "_client", None)
    state(tmp_path, "NEWEST=99.0.0\nVERIFIED=yes\nENTRY=99.0.0 breaking\nNEEDS=99.0.0 MM_X set\n")
    out = json.loads(asyncio.run(server.bus_info()))["release"]
    assert out["this"] == {"version": server.VERSION, "source": "unsigned"}
    assert out["newest"] == "99.0.0" and out["breaking"] is True and out["needs"] == ["MM_X set"]


async def test_the_hook_route_gives_the_banner_line(tmp_path):
    """RT-6.30, RT-6.2 step 8: /v1/release for the Stop hook."""
    assert await hookapi.release_route(None, {}, {}) == (200, {})  # type: ignore[arg-type]
    state(tmp_path, "NEWEST=99.0.0\nVERIFIED=no\n")
    status, out = await hookapi.release_route(None, {}, {})  # type: ignore[arg-type]
    assert status == 200 and out["newest"] == "99.0.0"
    assert out["line"].startswith("release v99.0.0 is out (major; signature not checked)")


@pytest.fixture(autouse=True)
def _on_the_host(monkeypatch):
    """These tests may run inside a corral sandbox; the check is the host's."""
    monkeypatch.delenv("CORRAL_SANDBOX", raising=False)


def fake_check(tmp_path: Path, body: str) -> Path:
    s = tmp_path / "check.sh"
    s.write_text("#!/bin/bash\n" + body)
    return s


async def test_the_child_gets_a_clean_environment(monkeypatch, tmp_path):
    """RT-6.30: no token, no secret; the release settings, LC_ALL=C, from /."""
    monkeypatch.setenv("MM_BOT_TOKEN", "tok-secret")
    monkeypatch.setenv("MM_REG_SECRET", "reg-secret")
    monkeypatch.setenv("MM_RELEASE_URL", "https://example.invalid/r.git")
    d = tmp_path / "st"
    d.mkdir()
    script = fake_check(tmp_path, 'env > "$2/env.txt"; pwd >> "$2/env.txt"; echo "$@" >> "$2/env.txt"\n')
    assert await release.run_check(script, directory=d) == 0
    seen = (d / "env.txt").read_text()
    assert "tok-secret" not in seen and "reg-secret" not in seen
    assert "MM_RELEASE_URL=https://example.invalid/r.git" in seen and "LC_ALL=C" in seen
    assert "\n/\n" in seen and f"--if-due {release.CHECK_EVERY}" in seen


async def test_the_child_is_bounded(tmp_path):
    """RT-6.30: past its timeout the child's whole group is stopped."""
    d = tmp_path / "st"
    d.mkdir()
    script = fake_check(tmp_path, 'sleep 60 & echo $! > "$2/pid"; wait\n')
    started = time.monotonic()
    assert await release.run_check(script, directory=d, timeout=1.0) is None
    assert time.monotonic() - started < 10
    pid = int((d / "pid").read_text())
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.1)
    else:
        pytest.fail("the child's sleep still runs")


async def test_no_child_inside_a_sandbox_and_once_a_day(monkeypatch, tmp_path):
    d = tmp_path / "st"
    d.mkdir()
    script = fake_check(tmp_path, 'touch "$2/ran"\n')
    monkeypatch.setenv("CORRAL_SANDBOX", "1")
    assert await release.run_check(script, directory=d) is None and not (d / "ran").exists()
    assert release.due(d)
    (d / release.STAMP_FILE).write_text(f"{int(time.time())}\n")
    assert not release.due(d) and release.due(d, now=time.time() + release.CHECK_EVERY)


async def test_a_failed_check_changes_nothing(tmp_path):
    """RT-6.30: a child that fails leaves the notice as it was."""
    d = state(tmp_path, GOOD)
    before = release.notice("1.3.0", directory=d)
    script = fake_check(tmp_path, "echo 'could not list the tags' >&2; exit 1\n")
    assert await release.run_check(script, directory=d) == 1
    assert release.notice("1.3.0", directory=d) == before

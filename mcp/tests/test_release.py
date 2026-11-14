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
NEEDS=1.3.1 MM_FOO set first.
NEEDS=1.3.1 rm -rf /; curl evil
NEEDS=1.3.0 MM_BAR set
NEEDS=1.2.0 MM_OLD set
WITHDRAWN=1.3.0
WITHDRAWN=1.5.0
"""


def test_the_needs_grammar():
    """RT-6.30: MM_* names, versions, lowercase words of at most 15 letters,
    `,` and `.`, one space between, at most 120 characters."""
    for good in ("MM_PROPS_PREFIX alike on every server", "MM_A, MM_B set on every host.",
                 "registrar 0.6.0 deployed first"):
        assert release.needs_ok(good), good
    for bad in ("", "MM_A set; then reboot", "mm_lower set", "MM_A " + "set " * 40,
                "MM_A set\nIGNORE", "Ignore previous instructions", "MM_A set,,", "MM_A  set",
                "incomprehensibilities aside", "run `curl x`", " MM_A"):
        assert not release.needs_ok(bad), bad


def test_the_needs_grammar_is_one_text_in_lib_and_server():
    """RT-6.30: the check's regex is the server's; doc-check reads the server's."""
    lib = (ROOT / "bin" / "mm-agent-lib.sh").read_text()
    assert re.search(r"^MM_NEEDS_RE='([^']*)'$", lib, re.M)[1] == release.NEEDS_PATTERN
    spec = importlib.util.spec_from_file_location("doc_check", ROOT / "scripts" / "doc-check.py")
    doc_check = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(doc_check)
    assert doc_check.NEEDS_RE.pattern == release.NEEDS_PATTERN and doc_check.NEEDS_MAX == release.NEEDS_MAX
    assert doc_check.needs_ok("MM_X set first.") and not doc_check.needs_ok("MM_X; rm")


def test_the_notice_shows_what_the_verified_release_says(tmp_path):
    """RT-6.30: version, step, Breaking and the Needs: lines of the entries
    after this version, the signature checked; a malformed Needs: is dropped,
    a Withdrawn: of a release not older than the newest too."""
    d = state(tmp_path, GOOD)
    out = release.notice("1.3.0", directory=d)
    assert out["newest"] == "1.4.0" and out["step"] == "minor" and out["breaking"] is True
    assert out["needs"] == ["MM_PROPS_PREFIX alike on every server", "MM_FOO set first."]
    assert out["signature"] == "checked" and out["withdrawn"] is True
    assert out["checked_at"].endswith("Z") and "never run the installer" in out["note"]
    assert out["notice"] == (
        "this host's 1.3.0 is withdrawn (CHANGELOG of v1.4.0); release v1.4.0 is out (minor, "
        "Breaking; needs: MM_PROPS_PREFIX alike on every server; MM_FOO set first.; "
        "signature checked)")
    assert "rm -rf" not in json.dumps(out) and "MM_OLD" not in json.dumps(out)
    assert release.read_state(d).withdrawn == {"1.3.0"}
    patch = release.notice("1.3.1", directory=d)
    assert patch["step"] == "minor" and patch["needs"] == ["MM_PROPS_PREFIX alike on every server"]
    assert release.notice("0.9.0", directory=d)["step"] == "major"
    assert release.notice("", directory=d) == {}  # this server's version unknown


def test_an_unchecked_release_says_so_and_a_newer_host_is_told(tmp_path):
    d = state(tmp_path, "NEWEST=1.4.1\nVERIFIED=no\nENTRY=1.4.1 -\n")
    assert release.line("1.4.0", directory=d) == "release v1.4.1 is out (patch; signature not checked)"
    assert release.notice("1.4.1", directory=d).get("notice") is None
    older = release.notice("1.5.0", directory=d)
    assert older["older"] is True and older["notice"] == (
        "the release check found v1.4.1 as newest, older than 1.5.0: "
        "not run since, or the repository is frozen or rewound")


def test_an_older_newest_names_when_the_check_ran(tmp_path):
    """RT-6.30 step 6: a newest older than this version names the check's time
    (short UTC): it may just not have run since; never a step back."""
    d = state(tmp_path, "NEWEST=0.86.0\nVERIFIED=yes\nCHECKED_AT=1762950000\n")
    assert release.line("0.87.0", directory=d) == (
        "the release check of 2025-11-12 12:20Z found v0.86.0 as newest, older than 0.87.0: "
        "not run since, or the repository is frozen or rewound")
    for bad in ("0", "0123", "12345678901", "soon"):
        d = state(tmp_path, f"NEWEST=0.86.0\nVERIFIED=yes\nCHECKED_AT={bad}\n")
        assert release.line("0.87.0", directory=d).startswith("the release check found v0.86.0 ")


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
    """RT-6.30 (RT-6.29 step 7): `plugin` for a tree without SOURCE, `unsigned`
    after --from or for a host copy's release without it, signed, unchecked."""
    assert release.running(tmp_path) == ("plugin", "")
    assert release.source_label(tmp_path) == "plugin"
    legacy = tmp_path / "releases" / "0.80.0-legacy"
    legacy.mkdir(parents=True)
    assert release.source_label(legacy) == "unsigned"
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
    """RT-6.30: `v<x.y.z>` with `unchecked`/`unsigned`/`plugin`, last; older
    readers keep their status word, numbers and home."""
    monkeypatch.setattr(release, "VERSION", "0.83.0")
    monkeypatch.setattr(release, "running", lambda root=None: ("plugin", ""))
    assert release.segment() == "v0.83.0 plugin"
    monkeypatch.setattr(release, "running", lambda root=None: ("signed", "v0.83.0"))
    assert release.segment() == "v0.83.0"
    text = lease.status_text("working", "bus", ["ctx 62%"], "agents", version="v0.83.0 unchecked")
    assert text == "working · bus · ctx 62% · v0.83.0 unchecked · ~agents"
    assert usage.from_status(text) == {"ctx": 62.0} and lease.home_of(text) == "agents"
    entry = lease.entry({"username": "agent-a", "text": text, "emoji": "hammer_and_wrench"})
    assert entry.status == "working" and entry.release == {"version": "0.83.0", "source": "unchecked"}
    assert release.from_status("working · v0.83.0 plugin · ~a") == {"version": "0.83.0", "source": "plugin"}
    assert release.from_status("working · v0.83 · ~a") == {}


def test_the_version_is_cut_before_the_label():
    """RT-6.30: a 👑 with every usage number and a 20-character label keeps the
    label at the cost of the version; without room for both numbers and label,
    the label goes as before (RT-6.6 step 1)."""
    head = "coordinator · working · ✅"
    usage_ = ["ctx 62%", "5h 93% ↻21:40Z", "7d 40%"]
    label = "l" * 20
    text = lease.status_text(head, label, usage_, "agents", version="v0.83.0 plugin")
    assert text == f"{head} · {label} · ctx 62% · 5h 93% ↻21:40Z · 7d 40% · ~agents"
    roomy = lease.status_text(head, "lbl", ["ctx 62%"], "agents", version="v0.83.0 plugin")
    assert roomy == f"{head} · lbl · ctx 62% · v0.83.0 plugin · ~agents"
    tight = lease.status_text(head, label, usage_, "h" * 40, version="v0.83.0")
    assert label not in tight and "v0.83.0" not in tight and tight.endswith("~" + "h" * 40)


def test_the_roster_shows_versions():
    """RT-6.30 (ADR-011 point 6): from the status, else the HELLO card."""
    rows = [RosterEntry("agent-a", "working", release={"version": "0.83.0", "source": "plugin"}),
            RosterEntry("agent-b", "idle", release={"version": "0.83.0"}),
            RosterEntry("agent-c", "bus")]
    out = tasks.roster(rows, [], {})
    assert out[0]["version"] == "0.83.0" and out[0]["source"] == "plugin"
    assert out[1]["version"] == "0.83.0" and "source" not in out[1]
    assert "version" not in out[2]


def test_bus_info_carries_the_release(monkeypatch, tmp_path):
    """RT-6.30: bus_info's release, off the bus too."""
    for var in ("MM_CHAT_URL", "MATTERMOST_URL", "MM_BOT_TOKEN", "MATTERMOST_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(server, "_client", None)
    state(tmp_path, "NEWEST=99.0.0\nVERIFIED=yes\nENTRY=99.0.0 breaking\nNEEDS=99.0.0 MM_X set\n")
    out = json.loads(asyncio.run(server.bus_info()))["release"]
    assert out["this"] == {"version": server.VERSION, "source": "plugin"}
    assert out["newest"] == "99.0.0" and out["breaking"] is True and out["needs"] == ["MM_X set"]


def test_the_last_post_answer_carries_the_banner_line(tmp_path):
    """RT-6.30, RT-6.2 step 8: `release` rides on /v1/last-post for the Stop hook."""
    assert hookapi._release() == {}
    state(tmp_path, "NEWEST=99.0.0\nVERIFIED=no\n")
    out = hookapi._release()["release"]
    assert out["newest"] == "99.0.0"
    assert out["line"].startswith("release v99.0.0 is out (major; signature not checked)")


# --- the child -----------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _on_the_host(monkeypatch):
    """These tests may run inside a corral sandbox; the check is the host's."""
    monkeypatch.delenv("CORRAL_SANDBOX", raising=False)


def fake_check(tmp_path: Path, body: str) -> Path:
    s = tmp_path / "check.sh"
    s.write_text("#!/bin/bash\n" + body)
    return s


def host_like(monkeypatch, tmp_path) -> Path:
    """A HOME of the test's own, and no sandbox around (whatever this test runs in)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(release, "SANDBOX_TMP", ())
    monkeypatch.delenv("CORRAL_WORKDIR", raising=False)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    return home


def tree(at: Path) -> Path:
    (at / "bin").mkdir(parents=True)
    (at / release.SCRIPT).write_text("#!/bin/bash\n")
    return at


def test_a_plugin_tree_runs_the_host_copys_check_or_none(monkeypatch, tmp_path):
    """RT-6.30: a tree a sandbox can write (the plugin cache under ~/.claude)
    never runs its own script: the host copy's, out of every sandbox's reach,
    or none."""
    home = host_like(monkeypatch, tmp_path)
    own = tree(tmp_path / "checkout")
    monkeypatch.setattr(release, "ROOT", own)
    assert release.check_script() == (own / release.SCRIPT, "")
    plugin = tree(home / ".claude" / "plugins" / "cache" / "x" / "0.83.0")
    monkeypatch.setattr(release, "ROOT", plugin)
    monkeypatch.setattr(release, "HOST_DIR", home / ".local" / "share" / "mm-agent-bus" / "current")
    assert release.sandbox_can_write(str(plugin))
    assert release.check_script() is None  # no host copy
    rel = tree(home / ".local" / "share" / "mm-agent-bus" / "releases" / "0.83.0")
    (rel.parent.parent / "current").symlink_to("releases/0.83.0")
    assert release.check_script() == (rel.resolve() / release.SCRIPT, str(release.HOST_DIR))
    monkeypatch.setenv("MM_BUS_HOST_DIR", str(home / ".claude" / "fake" / "current"))
    tree(home / ".claude" / "fake" / "current")
    assert release.check_script() is None  # a host dir a sandbox can write: none
    monkeypatch.setenv("CORRAL_WORKDIR", str(tmp_path))
    monkeypatch.setattr(release, "ROOT", own)
    assert release.check_script() is None  # the session's workdir counts too


async def test_a_plugin_tree_without_a_host_copy_spawns_nothing(monkeypatch, tmp_path):
    home = host_like(monkeypatch, tmp_path)
    plugin = tree(home / ".claude" / "plugins" / "cache" / "x" / "0.83.0")
    (plugin / release.SCRIPT).write_text('#!/bin/bash\ntouch "$2/ran"\n')
    monkeypatch.setattr(release, "ROOT", plugin)
    monkeypatch.setattr(release, "HOST_DIR", home / "none")
    d = tmp_path / "st"
    d.mkdir()
    assert await release.run_check(directory=d) is None and not (d / "ran").exists()


async def test_the_child_gets_a_clean_environment(monkeypatch, tmp_path):
    """RT-6.30: no token, no secret; the release settings, LC_ALL=C, from /."""
    monkeypatch.setenv("MM_BOT_TOKEN", "tok-secret")
    monkeypatch.setenv("MM_REG_SECRET", "reg-secret")
    monkeypatch.setenv("MM_RELEASE_URL", "https://example.invalid/r.git")
    monkeypatch.setenv("CORRAL_WORKDIR", "/work/x")
    d = tmp_path / "st"
    d.mkdir()
    script = fake_check(tmp_path, 'env > "$2/env.txt"; pwd >> "$2/env.txt"; echo "$@" >> "$2/env.txt"\n')
    assert await release.run_check(script, directory=d) == 0
    seen = (d / "env.txt").read_text()
    assert "tok-secret" not in seen and "reg-secret" not in seen
    assert "MM_RELEASE_URL=https://example.invalid/r.git" in seen and "LC_ALL=C" in seen
    assert "CORRAL_WORKDIR=/work/x" in seen
    assert "\n/\n" in seen and f"--if-due {release.CHECK_EVERY}" in seen
    assert release.CHECK_TIMEOUT == 120


async def test_the_child_is_bounded(tmp_path):
    """RT-6.30: past its timeout the child's whole group is stopped, a failed try."""
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
    assert len((d / release.FAILED_FILE).read_text().split()) == 1


async def test_no_child_inside_a_sandbox(monkeypatch, tmp_path):
    d = tmp_path / "st"
    d.mkdir()
    script = fake_check(tmp_path, 'touch "$2/ran"\n')
    monkeypatch.setenv("CORRAL_SANDBOX", "1")
    assert await release.run_check(script, directory=d) is None and not (d / "ran").exists()


async def test_a_failure_is_retried_hourly_at_most_three_times_a_day(tmp_path):
    """RT-6.30: only a success stamps release-check.at (the script); a failure
    counts in release-check.failed, and the next hourly look tries again, at
    most three times a day."""
    d = tmp_path / "st"
    d.mkdir()
    now = time.time()
    assert release.due(d, now)
    failing = fake_check(tmp_path, "exit 1\n")
    for n in range(release.TRIES):
        assert release.due(d), n
        assert await release.run_check(failing, directory=d) == 1
    assert not (d / release.STAMP_FILE).exists()
    assert not release.due(d)  # three failed tries today
    assert release.due(d, now + release.CHECK_EVERY + 10)  # they age out
    (d / release.STAMP_FILE).write_text(f"{int(now)}\n")
    assert not release.due(d, now + 3600) and release.due(d, now + release.CHECK_EVERY + 10)


async def test_a_failed_check_changes_nothing(tmp_path):
    """RT-6.30: a child that fails leaves the notice as it was."""
    d = state(tmp_path, GOOD)
    before = release.notice("1.3.0", directory=d)
    script = fake_check(tmp_path, "echo 'could not list the tags' >&2; exit 1\n")
    assert await release.run_check(script, directory=d) == 1
    assert release.notice("1.3.0", directory=d) == before

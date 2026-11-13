"""Release keys pinned on the host, and withdrawn versions (RT-6.29, RT-6.30;
ADR-011 decisions (4) and (5) of 2026-11-12).

A `--release` run with MM_RELEASE_SIGNERS stores the keys that verified the tag
in release-signers beside releases/ (MM_BUS_HOST_DIR's parent, not the state
directory, which XDG_STATE_HOME could move); later runs and the release check
use that file when the variable is unset; other keys in the variable are
refused unless --replace-signers; `--forget-signers` removes it. The release
check copies NEWEST and the withdrawn versions to release-withdrawn.env beside
releases/; `--release` and `--rollback` refuse a version listed there, unless
`--withdrawn-ok`. Keys and repositories are made in the test's own directory,
as in test_host_install_release.py.
"""

from __future__ import annotations

import os
from pathlib import Path

import test_host_install_release as signed
import test_release_check as checked
from test_host_install import INSTALL, VERSION, clone, releases, uv
from test_host_install_release import bump, g, install, pub, set_version, sign, unchanged

keys, host, origin, check = signed.keys, signed.host, checked.origin, checked.check
pytestmark = signed.pytestmark


def body(key: Path) -> str:
    """`<type> <base64>` of a public key: what release-signers holds, no comment."""
    return " ".join(pub(key).split()[:2])


def layout_dir(tmp_path) -> Path:
    """MM_BUS_HOST_DIR's parent in these tests: releases/, src.git, the pins."""
    return tmp_path / "host"


def pinned(tmp_path) -> Path:
    return layout_dir(tmp_path) / "release-signers"


def keys_in(f: Path) -> list[str]:
    return [ln for ln in f.read_text().splitlines() if not ln.startswith("#")]


def pin(tmp_path, *ks: Path, mode: int = 0o600) -> Path:
    """release-signers as a keyed run writes it."""
    f = pinned(tmp_path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# pinned\n" + "".join(body(k) + "\n" for k in ks))
    f.chmod(mode)
    return f


def test_a_keyed_run_pins_its_keys_for_later_runs(run, tmp_path, keys):
    """RT-6.29 (decision (4)): a run with MM_RELEASE_SIGNERS stores the keys
    (type and base64 only, 0600) beside releases/; a run without the variable
    verifies against the file and says so; a key not in it is refused; other
    keys in the variable, a rotation (old plus new) included, are refused
    unless --replace-signers, which replaces and says so; the same keys again
    change nothing."""
    origin = clone(tmp_path)
    sign(origin, f"v{VERSION}", keys["owner"])
    dest = tmp_path / "host" / "current"
    r = install(run, tmp_path, dest, f"v{VERSION}",
                pub(keys["owner"]) + " owner@laptop\n" + pub(keys["backup"]))
    assert r.code == 0, r.stderr
    f = pinned(tmp_path)
    assert f"Pinned the release keys of MM_RELEASE_SIGNERS in {f}" in r.stdout
    assert f.stat().st_mode & 0o777 == 0o600
    assert keys_in(f) == [body(keys["owner"]), body(keys["backup"])]
    assert "release keys from MM_RELEASE_SIGNERS" in r.stderr
    assert not (run.state_dir / "release-signers").exists()

    nxt = bump(VERSION)
    set_version(origin, nxt)
    sign(origin, f"v{nxt}", keys["backup"])
    r = install(run, tmp_path, dest, f"v{nxt}", None)
    assert r.code == 0, r.stderr
    assert "WARNING" not in r.stderr and f"release keys from {f}" in r.stderr
    assert f"a key pinned in {f}" in r.stdout
    assert (dest / "SOURCE").read_text().startswith("SOURCE=signed\n")

    third = bump(nxt)
    set_version(origin, third)
    sign(origin, f"v{third}", keys["stranger"])
    before, names = os.readlink(dest), releases(dest)
    unchanged(install(run, tmp_path, dest, f"v{third}", None), dest, before, names,
              f"signed by a key not pinned in {f}")
    # Other keys, or the pinned ones plus a new one: refused, the file kept.
    for other in (pub(keys["stranger"]), pub(keys["owner"]) + "\n" + pub(keys["backup"]) + "\n" + pub(keys["stranger"])):
        unchanged(install(run, tmp_path, dest, f"v{third}", other), dest, before, names,
                  "differ from MM_RELEASE_SIGNERS: mm-agent-host-install.sh --forget-signers first, or --release with --replace-signers")
        assert keys_in(f) == [body(keys["owner"]), body(keys["backup"])]
    # The same keys in another order and with comments: silent.
    same = install(run, tmp_path, dest, f"v{nxt}", pub(keys["backup"]) + " x\n" + pub(keys["owner"]))
    assert same.code == 0 and "Pinned" not in same.stdout and "Replaced" not in same.stdout

    r = install(run, tmp_path, dest, f"v{third}", pub(keys["stranger"]), "--replace-signers")
    assert r.code == 0, r.stderr
    assert f"Replaced the release keys pinned in {f} with those of MM_RELEASE_SIGNERS (--replace-signers)" in r.stdout
    assert keys_in(f) == [body(keys["stranger"])]
    again = install(run, tmp_path, dest, f"v{third}", pub(keys["stranger"]))
    assert again.code == 0 and "Pinned" not in again.stdout and "Replaced" not in again.stdout
    alone = run(INSTALL, "--replace-signers", MM_BUS_HOST_DIR=str(dest))
    assert alone.code != 0 and "--replace-signers goes with --release" in alone.stderr


def test_forget_signers_removes_the_pinned_keys(run, tmp_path, keys):
    """RT-6.29 (decision (4)): --forget-signers removes release-signers beside
    releases/; the next run without the variable is unchecked with the
    WARNING; it takes no other option and removes no symlink."""
    dest = tmp_path / "host" / "current"
    f = pin(tmp_path, keys["owner"])
    r = run(INSTALL, "--forget-signers", MM_BUS_HOST_DIR=str(dest), MM_RELEASE_SIGNERS=None)
    assert r.code == 0, r.stderr
    assert not f.exists() and f"Forgot the release keys pinned in {f}" in r.stdout
    assert "No release keys pinned" in run(INSTALL, "--forget-signers", MM_BUS_HOST_DIR=str(dest)).stdout
    bad = run(INSTALL, "--forget-signers", "--rollback", MM_BUS_HOST_DIR=str(dest))
    assert bad.code != 0 and "--forget-signers takes no other option" in bad.stderr
    target = tmp_path / "elsewhere"
    target.write_text(body(keys["owner"]) + "\n")
    f.symlink_to(target)
    r = run(INSTALL, "--forget-signers", MM_BUS_HOST_DIR=str(dest))
    assert r.code != 0 and "is a symlink or no regular file; refused, nothing removed" in r.stderr
    assert f.is_symlink() and target.exists()
    f.unlink()

    origin = clone(tmp_path)
    g(origin, "tag", "-a", "-m", "unsigned", f"v{VERSION}")
    r = install(run, tmp_path, dest, f"v{VERSION}", None)
    assert r.code == 0, r.stderr
    assert "WARNING: MM_RELEASE_SIGNERS is not set and no release keys are pinned" in r.stderr
    assert "signature NOT checked" in r.stdout


def test_a_bad_pinned_file_is_refused_never_skipped(run, tmp_path, keys, host):
    """RT-6.29 (decision (4)): a symlinked file, one others can write, one in a
    directory others or a sandbox can write, one with a line that is no key or
    with no key refuses the run, with or without the variable; the variable set
    but empty is refused even with a good file."""
    origin = clone(tmp_path)
    set_version(origin, "90.0.0")
    sign(origin, "v90.0.0", keys["owner"])
    before, names = os.readlink(host), releases(host)
    f = pinned(tmp_path)
    real = tmp_path / "elsewhere"
    real.write_text(body(keys["owner"]) + "\n")
    real.chmod(0o600)
    f.symlink_to(real)
    unchanged(install(run, tmp_path, host, "v90.0.0", None), host, before, names, "is a symlink")
    unchanged(install(run, tmp_path, host, "v90.0.0", pub(keys["owner"])), host, before, names, "is a symlink")
    f.unlink()
    pin(tmp_path, keys["owner"], mode=0o666)
    unchanged(install(run, tmp_path, host, "v90.0.0", None), host, before, names, "others can write it")
    f.chmod(0o600)
    layout_dir(tmp_path).chmod(0o777)
    try:
        unchanged(install(run, tmp_path, host, "v90.0.0", None), host, before, names,
                  "the directory of release-signers, is not this user's own or others can write it")
    finally:
        layout_dir(tmp_path).chmod(0o755)
    f.write_text("ssh-rsa AAAAB3NzaC1yc2E user@host\n")
    unchanged(install(run, tmp_path, host, "v90.0.0", None), host, before, names,
              "has a line that is no Ed25519 public key")
    f.write_text("# nothing\n")
    unchanged(install(run, tmp_path, host, "v90.0.0", None), host, before, names, "holds no public key")
    pin(tmp_path, keys["owner"])
    unchanged(install(run, tmp_path, host, "v90.0.0", ""), host, before, names,
              "MM_RELEASE_SIGNERS is set but holds no public key")
    # The layout directory where a sandbox writes (a session's workdir).
    unchanged(install(run, tmp_path, host, "v90.0.0", None, CORRAL_WORKDIR=str(layout_dir(tmp_path))),
              host, before, names, "lies where a corral sandbox can write")


def test_the_check_uses_the_pinned_keys(run, tmp_path, keys, origin):
    """RT-6.30 (decision (4)): without MM_RELEASE_SIGNERS the check verifies
    against release-signers beside releases/ and says so; it copies NEWEST and
    the withdrawn versions there for the installer; a key not in the file
    fails the check; a symlinked file fails it too."""
    sign(origin, "v90.1.0", keys["owner"])
    f = pin(tmp_path, keys["owner"])
    r = check(run, tmp_path, origin, None)
    assert r.code == 0, r.stderr
    assert "its signature checked (signed by " in r.stdout and f"keys from {f})" in r.stdout
    assert (run.state_dir / "release.env").read_text().count("VERIFIED=yes") == 1
    w = layout_dir(tmp_path) / "release-withdrawn.env"
    # TAG: the tags the check tried, for `latest` (RT-6.32 step 4).
    assert w.read_text() == "NEWEST=90.1.0\nWITHDRAWN=90.0.1\nTAG=90.1.0\n"
    assert w.stat().st_mode & 0o777 == 0o600
    pin(tmp_path, keys["stranger"])
    r = check(run, tmp_path, origin, None)
    assert r.code != 0 and "none of the newest tags passes" in r.stderr
    f.unlink()
    f.symlink_to(tmp_path / "nowhere")
    r = check(run, tmp_path, origin, None)
    assert r.code != 0 and "is a symlink; refused" in r.stderr


def withdrawn_env(tmp_path, *withdrawn: str, newest: str = "90.1.0") -> Path:
    """release-withdrawn.env as the host's release check writes it (RT-6.30 step 4)."""
    f = layout_dir(tmp_path) / "release-withdrawn.env"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"NEWEST={newest}\n" + "".join(f"WITHDRAWN={w}\n" for w in withdrawn))
    f.chmod(0o600)
    return f


def test_release_and_rollback_refuse_a_withdrawn_version(run, tmp_path, host):
    """RT-6.29 (decision (5)), RT-6.28 step 6: --release v<x> refuses a version
    the check listed as withdrawn, naming the release whose CHANGELOG withdrew
    it; --withdrawn-ok installs it on purpose and says so; --rollback to a
    withdrawn version, a -dev build included, is refused the same way; without
    the file nothing is refused, and that is said."""
    origin = clone(tmp_path)
    set_version(origin, "90.0.1")
    g(origin, "tag", "-a", "-m", "v90.0.1", "v90.0.1")
    env = withdrawn_env(tmp_path, "90.0.1", VERSION)
    before, names = os.readlink(host), releases(host)
    unchanged(install(run, tmp_path, host, "v90.0.1", None), host, before, names,
              "90.0.1 is withdrawn: the CHANGELOG of v90.1.0 withdrew it")
    r = install(run, tmp_path, host, "v90.0.1", None, "--withdrawn-ok")
    assert r.code == 0, r.stderr
    assert "90.0.1 is withdrawn (by the CHANGELOG of v90.1.0" in r.stderr and "--withdrawn-ok" in r.stderr
    assert os.readlink(host) == "releases/90.0.1"
    rb = dict(MM_BUS_HOST_DIR=str(host), **uv(tmp_path))
    before, names = os.readlink(host), releases(host)
    # previous is the -dev build of VERSION from the clone, withdrawn as well.
    unchanged(run(INSTALL, "--rollback", **rb), host, before, names, f"{VERSION} is withdrawn")

    set_version(origin, "90.0.2")
    g(origin, "tag", "-a", "-m", "v90.0.2", "v90.0.2")
    assert install(run, tmp_path, host, "v90.0.2", None).code == 0
    before, names = os.readlink(host), releases(host)
    unchanged(run(INSTALL, "--rollback", **rb), host, before, names, "90.0.1 is withdrawn")
    r = run(INSTALL, "--rollback", "--withdrawn-ok", **rb)
    assert r.code == 0, r.stderr
    assert os.readlink(host) == "releases/90.0.1"
    assert run(INSTALL, "--rollback", **rb).code == 0  # back to 90.0.2, not withdrawn
    env.unlink()
    r = run(INSTALL, "--rollback", **rb)  # no list: nothing refused, and said
    assert r.code == 0, r.stderr
    assert "withdrawn versions unknown: no release check on this host yet; run --check" in r.stderr
    assert os.readlink(host) == "releases/90.0.1"
    alone = run(INSTALL, "--withdrawn-ok", **rb)
    assert alone.code != 0 and "--withdrawn-ok goes with --release or --rollback" in alone.stderr


def test_a_redirected_state_home_neither_downgrades_nor_empties_the_list(run, tmp_path, keys, host):
    """RT-6.29 (decisions (4) and (5)): XDG_STATE_HOME pointed elsewhere (an
    .envrc in a workdir) finds the same pinned keys and the same withdrawn
    list, both beside releases/; --forget-signers there removes the right file."""
    origin = clone(tmp_path)
    set_version(origin, "90.0.1")
    sign(origin, "v90.0.1", keys["stranger"])
    f = pin(tmp_path, keys["owner"])
    withdrawn_env(tmp_path, "90.0.2")
    moved = tmp_path / "moved-state"
    moved.mkdir()
    before, names = os.readlink(host), releases(host)
    unchanged(install(run, tmp_path, host, "v90.0.1", None, XDG_STATE_HOME=str(moved)), host, before, names,
              f"signed by a key not pinned in {f}")
    set_version(origin, "90.0.2")
    sign(origin, "v90.0.2", keys["owner"])
    unchanged(install(run, tmp_path, host, "v90.0.2", None, XDG_STATE_HOME=str(moved)), host, before, names,
              "90.0.2 is withdrawn")
    r = run(INSTALL, "--forget-signers", MM_BUS_HOST_DIR=str(host), XDG_STATE_HOME=str(moved))
    assert r.code == 0 and not f.exists() and f"Forgot the release keys pinned in {f}" in r.stdout


def test_a_version_not_withdrawn_or_a_bad_list_refuses_nothing(run, tmp_path, host):
    """RT-6.29 (decision (5)): a WITHDRAWN not older than NEWEST, or a
    symlinked list, refuse nothing."""
    origin = clone(tmp_path)
    set_version(origin, "90.0.1")
    g(origin, "tag", "-a", "-m", "v90.0.1", "v90.0.1")
    env = withdrawn_env(tmp_path, "90.0.1", newest="90.0.1")  # not older than NEWEST: dropped
    r = install(run, tmp_path, host, "v90.0.1", None)
    assert r.code == 0, r.stderr
    target = tmp_path / "other.env"
    target.write_text("NEWEST=90.1.0\nWITHDRAWN=90.0.3\n")
    env.unlink()
    env.symlink_to(target)
    set_version(origin, "90.0.3")
    g(origin, "tag", "-a", "-m", "v90.0.3", "v90.0.3")
    r = install(run, tmp_path, host, "v90.0.3", None)
    assert r.code == 0, r.stderr

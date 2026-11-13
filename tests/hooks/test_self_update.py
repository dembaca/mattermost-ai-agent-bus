"""Self-update between sessions (RT-6.32, ADR-011 phase 3).

corral's end hook (the dispatcher, then the recorded release's
90-mm-session-stop.sh) runs `mm-agent-host-install.sh --auto` after the bot is
gone. It reads the registrar's signed release record (GET
/register/v1/release, verified with MM_REGISTRAR_KEYS by the host copy's own
Python), pins the release keys it offers when the host has none, refuses a
record with other keys, and installs the version it names as `--release`
would: only forward, only a tag signed by a pinned key, not withdrawn, not
held (--hold), and only when no other session of the host copy runs. Keys,
repositories and the registrar's signing key are made in the test's own
directory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

import test_host_install_release as signed
from conftest import ROOT
from mattermost_ai_agent_bus import release
from test_host_install import INSTALL, VERSION, clone, uv
from test_host_install_release import bump, g, set_version, sign
from test_release_signers import body, pin, pinned
from test_turn_end import STOP

keys = signed.keys
pytestmark = signed.pytestmark
SESSION = "sess-a"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _raw(k: Ed25519PrivateKey) -> bytes:
    return k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


REGISTRAR = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"registrar").digest())
STRANGER = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"not the registrar").digest())
REGISTRAR_KEYS = _b64(_raw(REGISTRAR))


def record(signers: list[str], version: str | None = None, *, key: Ed25519PrivateKey = REGISTRAR,
           typ: str = "mm-agent-release+jwt", iat: int | None = None) -> dict:
    """The registrar's answer: {"record": <compact JWS>} as it signs it."""
    iat = int(time.time()) if iat is None else iat
    claims = {"signers": signers, "set_at_ms": 1_700_000_000_000, "iat": iat, "exp": iat + 3600}
    if version:
        claims["version"] = version
    kid = _b64(hashlib.sha256(_raw(key)).digest())[:16]
    head = _b64(json.dumps({"alg": "EdDSA", "typ": typ, "kid": kid}).encode())
    pay = _b64(json.dumps(claims).encode())
    sig = _b64(key.sign(f"{head}.{pay}".encode()))
    return {"record": f"{head}.{pay}.{sig}"}


@pytest.fixture
def host(run, tmp_path):
    """A host copy installed from the clone (RT-6.28), its release's Python a
    wrapper of this test's interpreter (it runs the registrar read), and a
    newer version committed in the clone, not yet tagged."""
    origin = clone(tmp_path)
    dest = tmp_path / "host" / "current"
    r = run(INSTALL, "--from", str(origin), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0, r.stderr
    rel = dest.resolve()
    py = rel / "mcp" / ".venv" / "bin" / "python"
    py.unlink()
    py.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    py.chmod(0o755)
    nxt = bump(VERSION)
    (origin / "CHANGELOG.md").write_text(f"# Changelog\n\n## {nxt}\n\n- next\n")
    g(origin, "add", "CHANGELOG.md")
    set_version(origin, nxt)
    # A release check passed before on this host: the withdrawn list exists.
    (dest.parent / "release-withdrawn.env").write_text(f"NEWEST={VERSION}\n")
    return SimpleNamespace(dest=dest, rel=rel, origin=origin, nxt=nxt, layout=dest.parent)


def end(run, tmp_path, h, *, registrar_keys: str | None = REGISTRAR_KEYS, **env):
    """corral's postEnd for session SESSION, through the dispatcher, with the
    record preStart wrote."""
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / f"{SESSION}.release").write_text(f"{h.rel}\n")
    env.setdefault("MM_RELEASE_URL", str(h.origin))
    return run(str(h.layout / "current.hooks" / "mm-session-stop.sh"), **uv(tmp_path),
               CORRAL_SESSION_ID=SESSION, MM_REGISTRAR_KEYS=registrar_keys,
               MM_RELEASE_SIGNERS=None, **env)


def log(run) -> str:
    f = run.state_dir / "release-auto.log"
    return f.read_text() if f.exists() else ""


def auto_env(run) -> dict[str, str]:
    f = run.state_dir / "release-auto.env"
    return dict(ln.split("=", 1) for ln in f.read_text().splitlines()) if f.exists() else {}


def unchanged(h) -> None:
    assert h.dest.resolve() == h.rel, "current moved"
    assert not (h.layout / ".install.lock").exists()


def bash_auto_notice(run, h, have: str) -> str:
    return subprocess.run(
        ["bash", "-c", f'source "{ROOT}/bin/mm-agent-lib.sh"; mm_release_auto_notice "$1" "$2" "$3"',
         "_", have, str(run.state_dir), str(h.layout)], capture_output=True, text=True).stdout.strip()


def test_first_contact_pins_the_keys_and_updates_at_session_end(mm, run, tmp_path, keys, host):
    """RT-6.32 steps 2–6: no pin yet, a verified record: its keys are pinned,
    and the newer version it names, signed by one of them, is installed when
    the session ends; the record went first, so this session is not "another"."""
    sign(host.origin, f"v{host.nxt}", keys["owner"])
    mm.registrar_release = (200, record([body(keys["owner"]), body(keys["backup"])], f"v{host.nxt}"))
    r = end(run, tmp_path, host)
    assert r.code == 0, r.stderr
    assert f"self-update: updated to v{host.nxt}" in r.stderr, r.stderr
    assert sorted(ln for ln in pinned(tmp_path).read_text().splitlines() if not ln.startswith("#")) == \
        sorted([body(keys["owner"]), body(keys["backup"])])
    assert pinned(tmp_path).stat().st_mode & 0o777 == 0o600
    new = host.layout / "releases" / host.nxt
    assert host.dest.resolve() == new and (host.layout / "previous").resolve() == host.rel
    assert "SOURCE=signed" in (new / "SOURCE").read_text()
    text = log(run)
    assert "pinned the 2 release key(s) the registrar offers" in text and f"updated to v{host.nxt}" in text
    assert auto_env(run)["RESULT"] == "updated" and auto_env(run)["KEYS"] == "match"
    assert not (run.state_dir / f"{SESSION}.release").exists()
    # The registrar names the same version again: nothing to do, never down.
    r = end(run, tmp_path, host)
    assert host.dest.resolve() == new and f"not newer than {host.nxt}" in log(run)


@pytest.mark.parametrize("case", ["other registrar key", "wrong type", "garbage", "no registrar keys"])
def test_an_unverified_record_or_no_registrar_keys_pins_nothing(mm, run, tmp_path, keys, host, case):
    """RT-6.32 step 2: only a record the pinned registrar keys verify counts;
    without pinned registrar keys nothing is read. A host without release
    keys never updates by itself (unchecked mode)."""
    sign(host.origin, f"v{host.nxt}", keys["owner"])
    signers = [body(keys["owner"])]
    answer = {"other registrar key": record(signers, f"v{host.nxt}", key=STRANGER),
              "wrong type": record(signers, f"v{host.nxt}", typ="mm-agent-debug+jwt"),
              "garbage": {"record": "a.b.c"},
              "no registrar keys": record(signers, f"v{host.nxt}")}[case]
    mm.registrar_release = (200, answer)
    r = end(run, tmp_path, host, registrar_keys=None if case == "no registrar keys" else REGISTRAR_KEYS)
    assert r.code == 0, r.stderr
    unchanged(host)
    assert not pinned(tmp_path).exists()
    want = "nothing taken from the registrar" if case == "no registrar keys" else "refused"
    assert want in log(run), log(run)
    assert "updates itself" not in bash_auto_notice(run, host, VERSION)


def test_a_different_key_set_is_refused_and_the_notice_says_so(mm, run, tmp_path, keys, host):
    """RT-6.32 step 3: keys are pinned once; a record naming another set is
    refused (no update), logged and shown; changing keys is a person's step.
    The same set in another order and with comments is the same set."""
    sign(host.origin, f"v{host.nxt}", keys["owner"])
    pin(tmp_path, keys["owner"], keys["backup"])
    before = pinned(tmp_path).read_text()
    mm.registrar_release = (200, record([body(keys["owner"]), body(keys["stranger"])], f"v{host.nxt}"))
    r = end(run, tmp_path, host)
    assert r.code == 0 and "self-update: refused" in r.stderr, r.stderr
    unchanged(host)
    assert pinned(tmp_path).read_text() == before
    assert auto_env(run)["KEYS"] == "mismatch"
    want = "the registrar names a release key this host doesn't trust: no self-update"
    assert bash_auto_notice(run, host, VERSION) == want
    assert release.auto_line(VERSION, run.state_dir, host.layout) == want
    # The same keys, another order: they match, and the update goes through.
    mm.registrar_release = (200, record([body(keys["backup"]), body(keys["owner"])], f"v{host.nxt}"))
    r = end(run, tmp_path, host)
    assert host.dest.resolve() == host.layout / "releases" / host.nxt, log(run)


def test_the_notice_names_the_update_before_the_session_ends(mm, run, tmp_path, keys, host):
    """RT-6.32 step 7: the release check reads the record for the notice only
    (nothing pinned, nothing installed); the notice says what will happen."""
    mm.registrar_release = (200, record([body(keys["owner"])], f"v{host.nxt}"))
    sign(host.origin, f"v{host.nxt}", keys["owner"])
    py = host.rel / "mcp" / ".venv" / "bin" / "python"
    r = run(str(host.rel / "bin" / "mm-agent-release-check.sh"), MM_BUS_HOST_DIR=str(host.dest),
            MM_RELEASE_URL=str(host.origin), MM_RELEASE_SIGNERS=None, MM_REGISTRAR_KEYS=REGISTRAR_KEYS)
    assert py.exists() and "the registrar names v" in r.stderr, r.stderr
    assert auto_env(run)["TARGET"] == host.nxt and auto_env(run)["KEYS"] == "none"
    assert not pinned(tmp_path).exists() and host.dest.resolve() == host.rel
    want = f"updates itself to v{host.nxt} when this session ends"
    assert bash_auto_notice(run, host, VERSION) == want
    assert release.auto_line(VERSION, run.state_dir, host.layout) == want
    info = release.auto_info(VERSION, run.state_dir, host.layout)
    assert info["applies"] and info["target"] == host.nxt and info["will"] == want
    assert release.auto_info(VERSION, run.state_dir, None)["applies"] is False


@pytest.mark.parametrize("case", ["equal", "lower", "withdrawn", "unsigned", "unpinned signer",
                                  "held", "another session", "registrar 404", "other 404",
                                  "registrar 500", "keys only"])
def test_no_update(mm, run, tmp_path, keys, host, case):
    """RT-6.32: no self-update when the version is not newer, withdrawn, its
    tag unsigned or signed by a key not pinned, the host held, another session
    running, or the registrar answering 404 or an error; current stays."""
    version = {"equal": VERSION, "lower": "0.0.1"}.get(case, host.nxt)
    if case == "unsigned":
        g(host.origin, "tag", "-a", "-m", f"v{host.nxt}", f"v{host.nxt}")
    else:
        sign(host.origin, f"v{host.nxt}", keys["stranger" if case == "unpinned signer" else "owner"])
    mm.registrar_release = (200, record([body(keys["owner"])], None if case == "keys only" else f"v{version}"))
    if case == "registrar 404":
        mm.registrar_release = (404, {"error": "no release set"})
    elif case == "other 404":
        mm.registrar_release = (404, {"message": "not found"})
    elif case == "registrar 500":
        mm.registrar_release = (500, {"error": "boom"})
    elif case == "withdrawn":
        # A later release withdrew it; the release check --auto runs first finds that.
        later = bump(host.nxt)
        (host.origin / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## {later}\n\nWithdrawn: v{host.nxt}\n\n## {host.nxt}\n\n- next\n")
        set_version(host.origin, later)
        sign(host.origin, f"v{later}", keys["owner"])
    elif case == "held":
        assert run(INSTALL, "--hold", MM_BUS_HOST_DIR=str(host.dest)).code == 0
    elif case == "another session":
        run.state_dir.mkdir(parents=True, exist_ok=True)
        (run.state_dir / "sess-b.release").write_text(f"{host.rel}\n")
        (run.state_dir / "sess-b.env").write_text("MM_REG_BOT_NAME=b\n")
    r = end(run, tmp_path, host)
    assert r.code == 0, r.stderr
    unchanged(host)
    want = {"equal": "not newer", "lower": "not newer", "withdrawn": "withdrew",
            "unsigned": "carries no signature", "unpinned signer": "not pinned",
            "held": "held", "another session": "sess-b", "registrar 404": "names no release",
            "other 404": "no release endpoint", "registrar 500": "HTTP 500",
            "keys only": "names no version"}[case]
    assert want in log(run), log(run)
    # The terminal hears of what happened or waits, not of "nothing to do".
    quiet = case in ("equal", "lower", "registrar 404", "keys only")
    assert ("self-update: " in r.stderr) is not quiet, r.stderr


def test_without_a_withdrawn_list_nothing_is_installed(mm, run, tmp_path, keys, host):
    """RT-6.32 step 4: --auto runs the release check first; when no withdrawn
    list exists after it (no check ever passed), no self-update."""
    (host.layout / "release-withdrawn.env").unlink()
    sign(host.origin, f"v{host.nxt}", keys["owner"])
    for bad in ("99.0.0", "99.0.1", "99.0.2"):  # the check's three newest fail: it fails
        g(host.origin, "tag", f"v{bad}")
    mm.registrar_release = (200, record([body(keys["owner"])], f"v{host.nxt}"))
    r = end(run, tmp_path, host)
    assert r.code == 0 and "withdrawn versions are unknown" in r.stderr, r.stderr
    unchanged(host)


def test_a_stale_installer_lock_is_taken_over(mm, run, tmp_path, keys, host):
    """RT-6.32 step 5, RT-6.28 step 1: the lock holds the installer's pid; one
    whose installer is gone is taken over, one whose installer lives is not."""
    sign(host.origin, f"v{host.nxt}", keys["owner"])
    mm.registrar_release = (200, record([body(keys["owner"])], f"v{host.nxt}"))
    lock = host.layout / ".install.lock"
    lock.mkdir()
    (lock / "pid").write_text(f"{os.getpid()}\n")
    r = end(run, tmp_path, host)
    assert "another installer runs" in log(run) and "self-update: not updated" in r.stderr
    unchanged_lock = lock.exists()
    (lock / "pid").write_text("999999\n")
    r = end(run, tmp_path, host)
    assert unchanged_lock and "took over the lock of an installer that is gone (pid 999999)" in log(run)
    assert host.dest.resolve() == host.layout / "releases" / host.nxt, log(run)
    assert not lock.exists()


def test_the_end_hook_bounds_the_installer(tmp_path):
    """RT-6.32 step 5: corral runs hooks without a timeout; mm_bounded stops a
    command after its seconds and leaves no watchdog behind."""
    t = time.time()
    p = subprocess.run(["bash", "-c", f'source "{ROOT}/bin/mm-agent-lib.sh"; mm_bounded 1 sleep 30; echo "rc=$?"'],
                       capture_output=True, text=True, timeout=20)
    assert p.stdout.strip() == "rc=124" and time.time() - t < 10
    p = subprocess.run(["bash", "-c", f'source "{ROOT}/bin/mm-agent-lib.sh"; mm_bounded 5 sh -c "exit 3"; echo "rc=$?"'],
                       capture_output=True, text=True, timeout=20)
    assert p.stdout.strip() == "rc=3"


def test_the_bot_goes_even_when_the_update_fails(mm, run, tmp_path, keys, host):
    """RT-6.32 step 5: the update runs after the bot's removal, on every way
    out of the stop hook, and never fails it."""
    g(host.origin, "tag", "-a", "-m", f"v{host.nxt}", f"v{host.nxt}")  # unsigned
    mm.registrar_release = (200, record([body(keys["owner"])], f"v{host.nxt}"))
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / f"{SESSION}.env").write_text(
        "MM_REG_BOT_NAME=gone\nMM_BOT_USERNAME=agent-gone\nMM_BOT_TOKEN=bot-token-for-tests\n")
    r = end(run, tmp_path, host)
    assert r.code == 0 and "unregistered gone" in r.stderr, r.stderr
    assert ("DELETE", "/register/v1/agents/gone") in [(m, p) for m, p, _ in mm.writes]
    assert "not updated" in log(run) and "self-update: not updated" in r.stderr
    unchanged(host)


def test_the_start_hook_says_what_will_happen(mm, run, tmp_path, host):
    """RT-6.32 step 7: a session starting from a release of the host copy is
    told, in plain words, that the host updates itself when it ends."""
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / "release-auto.env").write_text(f"REGISTRAR=ok\nTARGET={host.nxt}\nKEYS=match\n")
    r = run(str(host.layout / "current.hooks" / "mm-session-start.sh"), CORRAL_SESSION_ID="sess-s",
            MM_REG_SECRET="test-secret", MM_PROJECT_CHANNEL="proj-x", MM_TEAM="team", MM_BOT_TOKEN=None)
    assert f"updates itself to v{host.nxt} when this session ends (RT-6.32)" in r.stderr, r.stderr


def test_the_turn_end_banner_says_it_updates_itself(mm, run, tmp_path, monkeypatch):
    """RT-6.32 step 7, RT-6.2 step 8: the banner names the self-update and
    leaves out the command for a human."""
    layout = tmp_path / "layout"
    layout.mkdir()
    monkeypatch.setattr(release, "layout_dir", lambda root=None: layout)
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / "release.env").write_text(f"NEWEST={VERSION}\nVERIFIED=yes\n")
    (run.state_dir / "release-auto.env").write_text("REGISTRAR=ok\nTARGET=99.0.0\nKEYS=match\n")
    r = run(STOP)
    assert r.passed and "updates itself to v99.0.0 when this session ends" in r.banner, r.banner
    assert "a human installs it" not in r.banner


def test_hold_and_unhold(run, tmp_path, host):
    """RT-6.32 step 1: --hold writes release-hold beside releases/ (not in the
    state directory), --unhold removes it; both alone; the notice says held."""
    r = run(INSTALL, "--hold", MM_BUS_HOST_DIR=str(host.dest))
    f = host.layout / "release-hold"
    assert r.code == 0 and "Held" in r.stdout and f.is_file() and f.stat().st_mode & 0o777 == 0o600
    assert run(INSTALL, "--hold", MM_BUS_HOST_DIR=str(host.dest)).stdout.startswith("Self-updates are held")
    assert release.held(host.layout)
    assert bash_auto_notice(run, host, VERSION).startswith("self-update held on this host")
    assert release.auto_line(VERSION, run.state_dir, host.layout).startswith("self-update held")
    r = run(INSTALL, "--hold", "--release", "v1.2.3", MM_BUS_HOST_DIR=str(host.dest))
    assert r.code != 0 and "takes no other option" in r.stderr
    r = run(INSTALL, "--unhold", MM_BUS_HOST_DIR=str(host.dest))
    assert r.code == 0 and "Unheld" in r.stdout and not f.exists()
    assert "not held" in run(INSTALL, "--unhold", MM_BUS_HOST_DIR=str(host.dest)).stdout
    assert "held by --hold" in log(run) and "unheld by --unhold" in log(run)
    # A symlink holds too, and --unhold removes it, not what it points at.
    target = tmp_path / "elsewhere"
    target.write_text("x")
    os.symlink(target, f)
    assert release.held(host.layout)
    assert run(INSTALL, "--unhold", MM_BUS_HOST_DIR=str(host.dest)).code == 0
    assert not os.path.lexists(f) and target.exists()


def test_auto_takes_no_other_option(run, tmp_path):
    r = run(INSTALL, "--auto", "--downgrade", MM_BUS_HOST_DIR=str(tmp_path / "host" / "current"))
    assert r.code != 0 and "--auto takes no other option" in r.stderr
    assert not Path(tmp_path / "host" / "release-signers").exists()

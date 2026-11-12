"""Host copy from a signed release (RT-6.29, ADR-011 phase 1b).

`mm-agent-host-install.sh --release v<x.y.z>` fetches the annotated tag into
src.git beside releases/, verifies its SSH signature against the keys pinned in
MM_RELEASE_SIGNERS, its name and the version in its tree, and installs it as
releases/<x.y.z>/ with SOURCE=signed. Every key here is made in the test's own
directory; git runs without the user's config, so no real key signs anything.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT
from test_host_install import INSTALL, VERSION, clone, releases, uv


def _git_version() -> tuple[int, int]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    m = re.search(r"(\d+)\.(\d+)", out)
    return (int(m[1]), int(m[2])) if m else (0, 0)


pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or _git_version() < (2, 34),
    reason="SSH-signed tags need ssh-keygen and git 2.34 or newer")

# No user or system git config: the user's own signing key never comes into play.
CLEAN = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")} | {
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "gpg.format=ssh",
       "-c", "commit.gpgSign=false", "-c", "tag.gpgSign=false"]


def g(c: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(c), *args], check=True, capture_output=True,
                          text=True, env=CLEAN).stdout


@pytest.fixture
def keys(tmp_path) -> dict[str, Path]:
    """Ed25519 keys made for this test: the owner's, the backup, and a stranger's."""
    d = tmp_path / "keys"
    d.mkdir()
    out = {}
    for n in ("owner", "backup", "stranger"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", n, "-f", str(d / n)],
                       check=True, env=CLEAN)
        out[n] = d / n
    return out


def pub(key: Path) -> str:
    return Path(f"{key}.pub").read_text().strip()


def fpr(key: Path) -> str:
    return subprocess.run(["ssh-keygen", "-lf", f"{key}.pub"], check=True, capture_output=True,
                          text=True).stdout.split()[1]


def set_version(c: Path, v: str, *, only_plugin: bool = False) -> str:
    """A commit whose tree says version v (plugin.json, pyproject, __init__)."""
    pj = c / ".claude-plugin" / "plugin.json"
    data = json.loads(pj.read_text())
    data["version"] = v
    pj.write_text(json.dumps(data, indent=2) + "\n")
    if not only_plugin:
        py = c / "mcp" / "pyproject.toml"
        py.write_text(re.sub(r'(?m)^version = ".*"$', f'version = "{v}"', py.read_text(), count=1))
        init = c / "mcp" / "src" / "mattermost_ai_agent_bus" / "__init__.py"
        init.write_text(re.sub(r'(?m)^__version__ = ".*"$', f'__version__ = "{v}"', init.read_text()))
    g(c, "commit", "-qam", f"version {v}")
    return g(c, "rev-parse", "HEAD").strip()


def sign(c: Path, tag: str, key: Path) -> None:
    g(c, "-c", f"user.signingKey={key}", "tag", "-s", "-m", tag, tag)


def install(run, tmp_path, dest, tag, signers, *args, **env):
    env.setdefault("MM_RELEASE_URL", str(clone(tmp_path)))
    return run(INSTALL, "--release", tag, *args, **uv(tmp_path), MM_BUS_HOST_DIR=str(dest),
               MM_RELEASE_SIGNERS=signers, **env)


def bump(v: str) -> str:
    a, b, c = v.split(".")
    return f"{a}.{b}.{int(c) + 1}"


@pytest.fixture
def host(run, tmp_path):
    """A host whose current is an unsigned install from the clone (RT-6.28)."""
    dest = tmp_path / "host" / "current"
    r = run(INSTALL, "--from", str(clone(tmp_path)), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0, r.stderr
    return dest


def unchanged(r, dest, before, names, why: str) -> None:
    """A refusal: says why, and current, previous and releases/ are as they were."""
    assert r.code != 0, r.stdout
    assert why in r.stderr, r.stderr
    assert os.readlink(dest) == before and releases(dest) == names
    assert not (dest.parent / ".install.lock").exists()


def test_release_installs_a_signed_tag_and_switches(run, tmp_path, keys):
    """RT-6.29: a tag signed by a pinned key installs into releases/<version>/,
    SOURCE=signed with tag and signer; the backup key signs the next one; an
    older signed release needs --downgrade and is then switched back to."""
    origin = clone(tmp_path)
    sha = g(origin, "rev-parse", "HEAD").strip()
    sign(origin, f"v{VERSION}", keys["owner"])
    dest = tmp_path / "host" / "current"
    pinned = pub(keys["owner"]) + "\n" + pub(keys["backup"])
    r = install(run, tmp_path, dest, f"v{VERSION}", pinned)
    assert r.code == 0, r.stderr
    assert os.readlink(dest) == f"releases/{VERSION}"
    source = (dest / "SOURCE").read_text()
    assert source.startswith("SOURCE=signed\n")
    assert f"TAG=v{VERSION}\n" in source and f"COMMIT={sha}\n" in source
    assert f"SIGNER={fpr(keys['owner'])}\n" in source
    assert f"signed release v{VERSION}" in r.stdout and "unsigned" not in r.stdout
    assert (dest.parent / "src.git").is_dir() and not (dest.parent / ".install.lock").exists()

    nxt = bump(VERSION)
    set_version(origin, nxt)
    sign(origin, f"v{nxt}", keys["backup"])
    r = install(run, tmp_path, dest, nxt, pinned)  # the v is optional
    assert r.code == 0, r.stderr
    assert os.readlink(dest) == f"releases/{nxt}"
    assert os.readlink(dest.parent / "previous") == f"releases/{VERSION}"
    assert f"SIGNER={fpr(keys['backup'])}" in (dest / "SOURCE").read_text()

    again = install(run, tmp_path, dest, f"v{nxt}", pinned)
    assert again.code == 0 and "already the current release" in again.stdout

    back = install(run, tmp_path, dest, f"v{VERSION}", pinned)
    assert back.code != 0 and "--downgrade" in back.stderr
    back = install(run, tmp_path, dest, f"v{VERSION}", pinned, "--downgrade")
    assert back.code == 0, back.stderr
    assert os.readlink(dest) == f"releases/{VERSION}" and "installed before" in back.stdout


def test_release_needs_pinned_keys(run, tmp_path, keys, host):
    """RT-6.29: no MM_RELEASE_SIGNERS, or a line that is no Ed25519 key: refused."""
    origin = clone(tmp_path)
    set_version(origin, "90.0.0")
    sign(origin, "v90.0.0", keys["owner"])
    before, names = os.readlink(host), releases(host)
    r = install(run, tmp_path, host, "v90.0.0", None)
    unchanged(r, host, before, names, "MM_RELEASE_SIGNERS is not set")
    r = install(run, tmp_path, host, "v90.0.0", "ssh-rsa AAAAB3NzaC1yc2E user@host")
    unchanged(r, host, before, names, "no Ed25519 public key")


def test_release_refuses_unsigned_lightweight_and_unpinned_tags(run, tmp_path, keys, host):
    """RT-6.29: an annotated tag without signature, a lightweight tag and a tag
    signed by a key not pinned are refused; current stays as it was."""
    origin = clone(tmp_path)
    pinned = pub(keys["owner"])
    before, names = os.readlink(host), releases(host)
    set_version(origin, "90.0.0")
    g(origin, "tag", "-a", "-m", "unsigned", "v90.0.0")
    unchanged(install(run, tmp_path, host, "v90.0.0", pinned), host, before, names,
              "carries no signature")
    set_version(origin, "90.0.1")
    g(origin, "tag", "v90.0.1")
    unchanged(install(run, tmp_path, host, "v90.0.1", pinned), host, before, names,
              "lightweight tag")
    set_version(origin, "90.0.2")
    sign(origin, "v90.0.2", keys["stranger"])
    r = install(run, tmp_path, host, "v90.0.2", pinned)
    unchanged(r, host, before, names, "not pinned in MM_RELEASE_SIGNERS")
    assert fpr(keys["stranger"]) in r.stderr


def test_release_refuses_a_tag_named_otherwise_or_with_another_version(run, tmp_path, keys, host):
    """RT-6.29: a ref pointed at another signed tag object, and a signed tag
    whose tree has another version (in any of the three files), are refused."""
    origin = clone(tmp_path)
    pinned = pub(keys["owner"])
    before, names = os.readlink(host), releases(host)
    set_version(origin, "90.0.3")
    sign(origin, "v90.0.3", keys["owner"])
    g(origin, "update-ref", "refs/tags/v90.0.4", g(origin, "rev-parse", "v90.0.3").strip())
    unchanged(install(run, tmp_path, host, "v90.0.4", pinned), host, before, names,
              "signed tag object named v90.0.3")
    sign(origin, "v90.0.5", keys["owner"])  # tree says 90.0.3
    unchanged(install(run, tmp_path, host, "v90.0.5", pinned), host, before, names,
              "not 90.0.5")
    set_version(origin, "90.0.6", only_plugin=True)  # pyproject and __init__ say 90.0.3
    sign(origin, "v90.0.6", keys["owner"])
    unchanged(install(run, tmp_path, host, "v90.0.6", pinned), host, before, names,
              "not 90.0.6")


def test_release_refuses_a_moved_tag(run, tmp_path, keys, host):
    """RT-6.29: a tag fetched before is never moved: the same name, signed
    again on another commit, is refused at the fetch."""
    origin = clone(tmp_path)
    pinned = pub(keys["owner"])
    set_version(origin, "90.1.0")
    sign(origin, "v90.1.0", keys["owner"])
    assert install(run, tmp_path, host, "v90.1.0", pinned).code == 0
    before, names = os.readlink(host), releases(host)
    readme = origin / "mcp" / "README.md"
    readme.write_text(readme.read_text() + "\nchanged\n")
    g(origin, "commit", "-qam", "after the release")
    g(origin, "tag", "-d", "v90.1.0")
    sign(origin, "v90.1.0", keys["owner"])
    r = install(run, tmp_path, host, "v90.1.0", pinned)
    unchanged(r, host, before, names, "moved since this host fetched it")


def test_release_refuses_a_failed_fetch_and_a_malformed_version(run, tmp_path, keys, host):
    """RT-6.29: a fetch that fails (no repository, no such tag) and a version
    not of the form v<x.y.z> change nothing; the version is not echoed."""
    pinned = pub(keys["owner"])
    before, names = os.readlink(host), releases(host)
    empty = tmp_path / "empty"
    empty.mkdir()
    unchanged(install(run, tmp_path, host, "v90.2.0", pinned, MM_RELEASE_URL=str(empty)),
              host, before, names, "could not fetch v90.2.0")
    unchanged(install(run, tmp_path, host, "v90.2.0", pinned), host, before, names,
              "could not fetch v90.2.0")
    unchanged(install(run, tmp_path, host, "http://example.invalid/x.git", pinned), host, before,
              names, "--release takes a version tag")
    r = install(run, tmp_path, host, "v1.2.3;touch-x", pinned)
    unchanged(r, host, before, names, "--release takes a version tag")
    assert "touch-x" not in r.stderr
    unchanged(install(run, tmp_path, host, "v90.2.0", pinned, MM_RELEASE_URL="http://x/y.git"),
              host, before, names, "must be an https:// URL")


def test_release_kind_tells_signed_from_unsigned(tmp_path):
    """RT-6.29: the start hook's notice and --check name the kind of host copy."""
    def kind(d: Path) -> str:
        return subprocess.run(["bash", "-c", f'source "{ROOT}/bin/mm-agent-lib.sh"; mm_release_kind "$1"',
                               "_", str(d)], check=True, capture_output=True, text=True).stdout.strip()
    signed, unsigned, odd = tmp_path / "s", tmp_path / "u", tmp_path / "o"
    for d in (signed, unsigned, odd):
        d.mkdir()
    (signed / "SOURCE").write_text("SOURCE=signed\nTAG=v1.2.3\nSIGNER=SHA256:x\n")
    (unsigned / "SOURCE").write_text("SOURCE=unsigned\nCOMMIT=abc\n")
    (odd / "SOURCE").write_text("SOURCE=signed\nTAG=v1.2.3; run me\n")
    assert kind(signed) == "signed release v1.2.3"
    assert kind(unsigned) == "unsigned" and kind(odd) == "unsigned"
    assert kind(tmp_path / "none") == "unsigned"

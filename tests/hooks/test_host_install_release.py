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


def test_release_refuses_signers_set_but_without_a_key(run, tmp_path, keys, host):
    """RT-6.29: MM_RELEASE_SIGNERS set but empty, or a line that is no Ed25519
    key: refused (set means enforced; only unset installs unchecked)."""
    origin = clone(tmp_path)
    set_version(origin, "90.0.0")
    sign(origin, "v90.0.0", keys["owner"])
    before, names = os.readlink(host), releases(host)
    r = install(run, tmp_path, host, "v90.0.0", "")
    unchanged(r, host, before, names, "set but holds no public key")
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
              "tag object named v90.0.3")
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
    unchecked = tmp_path / "r"
    unchecked.mkdir()
    (unchecked / "SOURCE").write_text("SOURCE=release\nVERIFIED=no\nTAG=v1.2.4\n")
    assert kind(unchecked) == "release v1.2.4 (signature not checked)"
    assert kind(unsigned) == "unsigned" and kind(odd) == "unsigned"
    assert kind(tmp_path / "none") == "unsigned"


def test_release_refuses_an_installer_a_sandbox_can_change(run, tmp_path, keys):
    """RT-6.29: --release, too, runs only an installer out of every sandbox's
    reach; the refusal names the host copy's installer."""
    from test_host_install import planted
    plant = planted(tmp_path)
    dest = tmp_path / "current"
    r = run(str(plant / INSTALL), "--release", "v90.0.0", MM_BUS_HOST_DIR=str(dest),
            MM_RELEASE_SIGNERS=pub(keys["owner"]))
    assert r.code != 0 and "can be changed from inside a corral sandbox" in r.stderr
    assert f"{dest}/bin/mm-agent-host-install.sh" in r.stderr and not dest.exists()
    r = run(INSTALL, "--release", "v90.0.0", MM_BUS_HOST_DIR=str(dest),
            MM_RELEASE_SIGNERS=pub(keys["owner"]), CORRAL_WORKDIR=str(ROOT))
    assert r.code != 0 and "workdir" in r.stderr and not dest.exists()


def test_release_refuses_the_same_version_from_another_tag_object(run, tmp_path, keys):
    """RT-6.29 step 6: src.git gone and the tag signed again: the same version
    from another tag object is refused, while it is current and while previous."""
    origin = clone(tmp_path)
    pinned = pub(keys["owner"])
    sign(origin, f"v{VERSION}", keys["owner"])
    dest = tmp_path / "host" / "current"
    assert install(run, tmp_path, dest, f"v{VERSION}", pinned).code == 0
    shutil.rmtree(dest.parent / "src.git")
    g(origin, "tag", "-d", f"v{VERSION}")
    g(origin, "-c", f"user.signingKey={keys['owner']}", "tag", "-s", "-m", "signed again",
      f"v{VERSION}")
    before, names = os.readlink(dest), releases(dest)
    r = install(run, tmp_path, dest, f"v{VERSION}", pinned)
    unchanged(r, dest, before, names, "was installed on this host from tag object")
    assert "already the current release" not in r.stdout
    nxt = bump(VERSION)
    set_version(origin, nxt)
    sign(origin, f"v{nxt}", keys["owner"])
    assert install(run, tmp_path, dest, f"v{nxt}", pinned).code == 0
    assert os.readlink(dest.parent / "previous") == f"releases/{VERSION}"
    before, names = os.readlink(dest), releases(dest)
    r = install(run, tmp_path, dest, f"v{VERSION}", pinned, "--downgrade")
    unchanged(r, dest, before, names, "a release never moves")


def test_release_refuses_a_signature_that_is_not_ssh(run, tmp_path, keys, host):
    """RT-6.29 step 4: a tag object with a PGP block is no SSH signature."""
    origin = clone(tmp_path)
    head = g(origin, "rev-parse", "HEAD").strip()
    body = (f"object {head}\ntype commit\ntag v90.3.0\ntagger t <t@example.invalid> 1700000000 +0000\n\n"
            "v90.3.0\n-----BEGIN PGP SIGNATURE-----\n\niQEzBAABCAAdFiEE\n-----END PGP SIGNATURE-----\n")
    obj = subprocess.run([*GIT, "-C", str(origin), "mktag"], input=body, check=True,
                         capture_output=True, text=True, env=CLEAN).stdout.strip()
    g(origin, "update-ref", "refs/tags/v90.3.0", obj)
    before, names = os.readlink(host), releases(host)
    unchanged(install(run, tmp_path, host, "v90.3.0", pub(keys["owner"])), host, before, names,
              "not an SSH signature")


def test_release_without_signers_installs_unchecked_with_a_warning(run, tmp_path, keys):
    """RT-6.29 (opt-in): MM_RELEASE_SIGNERS unset installs an annotated tag
    without checking its signature, warns, and marks it SOURCE=release
    VERIFIED=no; a lightweight tag is still refused."""
    origin = clone(tmp_path)
    sha = g(origin, "rev-parse", "HEAD").strip()
    g(origin, "tag", "-a", "-m", "unsigned", f"v{VERSION}")
    dest = tmp_path / "host" / "current"
    r = install(run, tmp_path, dest, f"v{VERSION}", None)
    assert r.code == 0, r.stderr
    assert "WARNING: MM_RELEASE_SIGNERS is not set" in r.stderr and "NOT checked" in r.stderr
    assert os.readlink(dest) == f"releases/{VERSION}"
    source = (dest / "SOURCE").read_text()
    assert source.startswith("SOURCE=release\nVERIFIED=no\n")
    assert f"TAG=v{VERSION}\n" in source and f"COMMIT={sha}\n" in source and "SIGNER" not in source
    assert "signature NOT checked" in r.stdout
    nxt = bump(VERSION)
    set_version(origin, nxt)
    g(origin, "tag", f"v{nxt}")
    before, names = os.readlink(dest), releases(dest)
    unchanged(install(run, tmp_path, dest, f"v{nxt}", None), dest, before, names, "lightweight tag")


def test_release_without_signers_keeps_every_other_check(run, tmp_path, keys, host):
    """RT-6.29 (opt-in): unset, a moved tag, another version in the tree and a
    tag object named otherwise are refused as with keys."""
    origin = clone(tmp_path)
    before, names = os.readlink(host), releases(host)
    set_version(origin, "90.4.0")
    g(origin, "tag", "-a", "-m", "x", "v90.4.0")
    g(origin, "update-ref", "refs/tags/v90.4.1", g(origin, "rev-parse", "v90.4.0").strip())
    unchanged(install(run, tmp_path, host, "v90.4.1", None), host, before, names,
              "tag object named v90.4.0")
    g(origin, "tag", "-a", "-m", "x", "v90.4.2")  # tree says 90.4.0
    unchanged(install(run, tmp_path, host, "v90.4.2", None), host, before, names, "not 90.4.2")
    assert install(run, tmp_path, host, "v90.4.0", None).code == 0
    before, names = os.readlink(host), releases(host)
    readme = origin / "mcp" / "README.md"
    readme.write_text(readme.read_text() + "\nmoved\n")
    g(origin, "commit", "-qam", "after the release")
    g(origin, "tag", "-d", "v90.4.0")
    g(origin, "tag", "-a", "-m", "again", "v90.4.0")
    unchanged(install(run, tmp_path, host, "v90.4.0", None), host, before, names,
              "moved since this host fetched it")


def test_release_marks_are_never_lowered(run, tmp_path, keys):
    """RT-6.29 (opt-in): an unchecked run over a release verified before keeps
    it signed; a checked run over one installed unchecked marks it signed."""
    origin = clone(tmp_path)
    sign(origin, f"v{VERSION}", keys["owner"])
    pinned = pub(keys["owner"])
    dest = tmp_path / "host" / "current"
    assert install(run, tmp_path, dest, f"v{VERSION}", pinned).code == 0
    r = install(run, tmp_path, dest, f"v{VERSION}", None)
    assert r.code == 0 and "already the current release" in r.stdout
    assert (dest / "SOURCE").read_text().startswith("SOURCE=signed\n")
    nxt = bump(VERSION)
    set_version(origin, nxt)
    sign(origin, f"v{nxt}", keys["owner"])
    assert install(run, tmp_path, dest, f"v{nxt}", None).code == 0
    assert (dest / "SOURCE").read_text().startswith("SOURCE=release\nVERIFIED=no\n")
    r = install(run, tmp_path, dest, f"v{nxt}", pinned)
    assert r.code == 0, r.stderr
    source = (dest / "SOURCE").read_text()
    assert source.startswith("SOURCE=signed\n") and f"SIGNER={fpr(keys['owner'])}" in source

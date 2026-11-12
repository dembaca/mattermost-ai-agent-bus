"""Host copy of the corral hooks (docs/arc42/07-deployment-view.md, §7.1).

The hooks run on the host with the registration secret; they must not run from
a tree a sandbox can write (the plugin directory, the session's workdir).
Install, switch and rollback of the host copy: RT-6.28.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import ROOT

INSTALL = "bin/mm-agent-host-install.sh"


def uv(tmp_path, fail: bool | str = False) -> dict:
    """A stand-in for uv on PATH: `uv sync … --directory D` makes D/.venv/bin/python,
    or fails: always with fail=True, for a D containing it with a string. The
    installer's own steps stay real (RT-6.28)."""
    b = tmp_path / "fakeuv"
    if not b.exists():
        b.mkdir()
        (b / "uv").write_text(
            '#!/bin/sh\n[ -z "$FAKE_UV_FAIL" ] || { echo "uv: sync failed" >&2; exit 1; }\n'
            'd=""\nwhile [ $# -gt 0 ]; do [ "$1" = --directory ] && d="$2"; shift; done\n'
            '[ -n "$d" ] || exit 2\n'
            'if [ -n "$FAKE_UV_FAIL_IN" ]; then case "$d" in *"$FAKE_UV_FAIL_IN"*) exit 1 ;; esac; fi\n'
            f'mkdir -p "$d/.venv/bin" && ln -sf "{sys.executable}" "$d/.venv/bin/python"\n')
        (b / "uv").chmod(0o755)
    how = {} if not fail else {"FAKE_UV_FAIL": "1"} if fail is True else {"FAKE_UV_FAIL_IN": fail}
    return {"PATH": f"{b}{os.pathsep}{os.environ['PATH']}", **how}


START = "adapters/corral/10-mm-session-start.sh"
VERSION = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]


def registry(home, install_path) -> None:
    f = home / ".claude" / "plugins" / "installed_plugins.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"plugins": {"mattermost-ai-agent-bus@x": [
        {"installPath": str(install_path)}]}}))


# What the installer copies, and the ignore rules that keep caches out of git.
PARTS = (".gitignore", "bin", "adapters", ".claude-plugin", "mcp/.gitignore", "mcp/pyproject.toml",
         "mcp/uv.lock", "mcp/README.md", "mcp/src", "skills")
GIT = ["git", "-c", "commit.gpgsign=false", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def git(c: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(c), *args], check=True, capture_output=True,
                          text=True).stdout


def clone(tmp_path) -> Path:
    """A git clone of this tree as it is now, everything committed: the only
    source the installer takes (arc42 §7.1)."""
    c = tmp_path / "clone"
    if c.exists():
        return c
    subprocess.run(["git", "init", "-q", str(c)], check=True)
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--cached", "--others",
                          "--exclude-standard", "--", *PARTS],
                         check=True, capture_output=True, text=True).stdout
    for f in filter(None, out.split("\0")):
        if (ROOT / f).is_file():
            (c / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / f, c / f)
    git(c, "add", "-A")
    git(c, "commit", "-qm", "reviewed")
    return c


def test_installs_hooks_and_libs_out_of_the_sandbox(run, tmp_path):
    dest = tmp_path / "host" / "current"
    r = run(INSTALL, "--from", str(clone(tmp_path)), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0, r.stderr
    for f in ("adapters/corral/10-mm-session-start.sh", "adapters/corral/90-mm-session-stop.sh",
              "bin/mm-agent-lib.sh", "bin/mm-agent-register.sh"):
        assert os.access(dest / f, os.X_OK if f.endswith(".sh") else os.R_OK), f
    assert f"exec: {dest}.hooks/mm-session-start.sh" in r.stdout
    assert VERSION in r.stdout
    # RT-6.18: the MCP server goes along, to run on the host; a checkout's caches do not.
    assert (dest / "mcp" / "src" / "mattermost_ai_agent_bus" / "hosted.py").is_file()
    assert (dest / "mcp" / "uv.lock").is_file() and (dest / "skills" / "mattermost-ai-agent-bus").is_dir()
    assert not list((dest / "mcp" / "src").rglob("__pycache__"))
    assert "MCP server environment ready" in r.stdout or "WARNING: no Python environment" in r.stdout


def commit(c: Path, line: str = "x") -> str:
    """A new commit in the clone; returns its release name's suffix."""
    readme = c / "mcp" / "README.md"
    readme.write_text(readme.read_text() + f"\n{line}\n")
    git(c, "commit", "-qam", line)
    return git(c, "rev-parse", "HEAD").strip()[:12]


def releases(dest: Path) -> list[str]:
    return sorted(p.name for p in (dest.parent / "releases").iterdir())


def link(p: Path) -> str:
    return os.readlink(p).removeprefix("releases/")


def install(run, tmp_path, dest, *args, fail=False):
    return run(INSTALL, "--from", str(clone(tmp_path)), *args, **uv(tmp_path, fail),
               MM_BUS_HOST_DIR=str(dest))


def test_reinstall_replaces_the_whole_tree(run, tmp_path):
    """RT-6.28: a new commit is a new release; the same one again changes nothing."""
    dest = tmp_path / "current"
    assert install(run, tmp_path, dest).code == 0
    (dest / "bin" / "stray.sh").write_text("echo old\n")
    again = install(run, tmp_path, dest)
    assert again.code == 0 and "already the current release" in again.stdout
    commit(clone(tmp_path))
    assert install(run, tmp_path, dest).code == 0
    assert not (dest / "bin" / "stray.sh").exists()
    assert not any(p.name.startswith((".old", ".install", ".current"))
                   for p in [*dest.parent.iterdir(), *(dest.parent / "releases").iterdir()])


def test_installs_into_a_release_and_switches_current(run, tmp_path):
    """RT-6.28 steps 1-4: releases/<version>-dev.<commit>, SOURCE=unsigned,
    the environment before the switch, current a symlink to it."""
    dest = tmp_path / "host" / "current"
    sha = git(clone(tmp_path), "rev-parse", "HEAD").strip()
    r = install(run, tmp_path, dest)
    assert r.code == 0, r.stderr
    name = f"{VERSION}-dev.{sha[:12]}"
    assert dest.is_symlink() and os.readlink(dest) == f"releases/{name}"
    rel = dest.parent / "releases" / name
    assert (rel / "SOURCE").read_text() == f"SOURCE=unsigned\nCOMMIT={sha}\n"
    assert (rel / "mcp" / ".venv" / "bin" / "python").exists()
    assert "unsigned" in r.stdout and "previous: none" in r.stdout
    assert not (dest.parent / "previous").exists()


def test_a_failed_sync_leaves_current_as_it_was(run, tmp_path):
    """RT-6.28 error handling: uv sync fails in the new release; current is
    untouched and the half-made release goes."""
    dest = tmp_path / "current"
    assert install(run, tmp_path, dest).code == 0
    before, names = os.readlink(dest), releases(dest)
    commit(clone(tmp_path))
    r = install(run, tmp_path, dest, fail=True)
    assert r.code != 0 and "is unchanged" in r.stderr
    assert os.readlink(dest) == before and releases(dest) == names
    assert not (dest.parent / "previous").exists()


def test_rollback_swaps_current_and_previous(run, tmp_path):
    """RT-6.28 step 6: --rollback swaps the two links, and back again."""
    dest = tmp_path / "current"
    assert install(run, tmp_path, dest).code == 0
    first = link(dest)
    commit(clone(tmp_path))
    r = install(run, tmp_path, dest)
    assert r.code == 0 and f"previous: releases/{first}" in r.stdout
    second = link(dest)
    assert link(dest.parent / "previous") == first
    r = run(INSTALL, "--rollback", MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0, r.stderr
    assert (link(dest), link(dest.parent / "previous")) == (first, second)
    assert run(INSTALL, "--rollback", MM_BUS_HOST_DIR=str(dest)).code == 0
    assert (link(dest), link(dest.parent / "previous")) == (second, first)
    assert releases(dest) == sorted([first, second])


def test_rollback_without_previous_is_refused(run, tmp_path):
    dest = tmp_path / "current"
    assert install(run, tmp_path, dest).code == 0
    before = os.readlink(dest)
    r = run(INSTALL, "--rollback", MM_BUS_HOST_DIR=str(dest))
    assert r.code != 0 and "no previous release" in r.stderr and os.readlink(dest) == before


def test_an_older_version_needs_downgrade(run, tmp_path):
    """RT-6.28 step 2: nobody goes back to an older version by accident."""
    dest = tmp_path / "current"
    assert install(run, tmp_path, dest).code == 0
    before = os.readlink(dest)
    c = clone(tmp_path)
    (c / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": "0.1.0"}))
    git(c, "commit", "-qam", "old")
    r = install(run, tmp_path, dest)
    assert r.code != 0 and "--downgrade" in r.stderr and os.readlink(dest) == before
    assert not any(n.startswith("0.1.0") for n in releases(dest))
    r = install(run, tmp_path, dest, "--downgrade")
    assert r.code == 0, r.stderr
    assert link(dest).startswith("0.1.0-dev.") and os.readlink(dest.parent / "previous") == before


def test_migrates_a_host_copy_from_before_the_layout(run, tmp_path):
    """RT-6.28 step 5: a real directory current moves to releases/<v>-legacy,
    gets its environment again (a venv does not survive a move) and becomes
    previous."""
    dest = tmp_path / "current"
    tree(dest, "0.80.0")
    (dest / "mcp").mkdir()
    (dest / "bin" / "stray.sh").write_text("echo old\n")
    r = install(run, tmp_path, dest)
    assert r.code == 0, r.stderr
    assert dest.is_symlink() and link(dest).startswith(f"{VERSION}-dev.")
    legacy = dest.parent / "releases" / "0.80.0-legacy"
    assert os.readlink(dest.parent / "previous") == "releases/0.80.0-legacy"
    assert (legacy / "bin" / "stray.sh").exists() and (legacy / "SOURCE").read_text() == "SOURCE=unsigned\n"
    assert (legacy / "mcp" / ".venv" / "bin" / "python").exists()
    assert "previous: releases/0.80.0-legacy" in r.stdout
    assert run(INSTALL, "--rollback", MM_BUS_HOST_DIR=str(dest)).code == 0
    assert link(dest) == "0.80.0-legacy"


HOOK_FILES = ("mm-session-start.sh", "mm-session-stop.sh")


def test_a_switch_leaves_corrals_hook_files_byte_identical(run, tmp_path):
    """RT-6.28 step 4: corral approves a hook by its configured path's content
    and skips a postEnd whose content changed during the session; the files it
    runs (current.hooks/) stay the same through install and rollback."""
    dest = tmp_path / "current"
    hooks = tmp_path / "current.hooks"
    r = install(run, tmp_path, dest)
    assert r.code == 0 and "corral asks to approve it" in r.stdout
    want = (ROOT / "adapters" / "corral" / "mm-hook-dispatch.sh").read_bytes()
    assert [(hooks / f).read_bytes() for f in HOOK_FILES] == [want, want]
    assert all(os.access(hooks / f, os.X_OK) for f in HOOK_FILES)
    commit(clone(tmp_path))
    r = install(run, tmp_path, dest)
    assert r.code == 0 and "corral asks nothing" in r.stdout
    assert [(hooks / f).read_bytes() for f in HOOK_FILES] == [want, want]
    r = run(INSTALL, "--rollback", MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0 and "corral asks nothing" in r.stdout
    assert [(hooks / f).read_bytes() for f in HOOK_FILES] == [want, want]


def test_a_changed_dispatcher_waits_while_a_session_runs(run, tmp_path):
    """RT-6.28 step 4: a new dispatcher text replaces the hook files only when
    no session runs, whose postEnd corral would then skip."""
    dest = tmp_path / "current"
    hooks = tmp_path / "current.hooks"
    assert install(run, tmp_path, dest).code == 0
    old = (hooks / HOOK_FILES[1]).read_bytes()
    c = clone(tmp_path)
    d = c / "adapters" / "corral" / "mm-hook-dispatch.sh"
    d.write_text(d.read_text() + "# changed\n")
    git(c, "commit", "-qam", "dispatcher")
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / "sess-a.release").write_text(f"{dest.resolve()}\n")
    r = install(run, tmp_path, dest)
    assert r.code == 0 and "rerun this when none runs" in r.stdout
    assert all((hooks / f).read_bytes() == old for f in HOOK_FILES)
    (run.state_dir / "sess-a.release").unlink()
    r = install(run, tmp_path, dest)
    assert r.code == 0 and "corral asks to approve it" in r.stdout
    assert all((hooks / f).read_bytes() == d.read_bytes() for f in HOOK_FILES)


def running_session(run, key="sess-old"):
    """A host-managed corral session file whose corral process (this one) lives,
    without a release record: started from a host copy before RT-6.28."""
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / f"{key}.env").write_text(
        f"MM_REG_BOT_NAME=x\nMM_SESSION_PID={os.getpid()}\nMM_BUS_HOST_PID=999999\n")


def test_no_migration_while_a_corral_session_runs_from_the_old_copy(run, tmp_path):
    """RT-6.28 step 5: moving the old host copy would make corral skip the
    postEnd of every session running from it."""
    dest = tmp_path / "current"
    tree(dest, "0.80.0")
    (dest / "mcp").mkdir()
    running_session(run)
    r = install(run, tmp_path, dest)
    assert r.code != 0 and "sess-old" in r.stderr and "end them, then rerun" in r.stderr
    assert dest.is_dir() and not dest.is_symlink() and releases(dest) == []
    (run.state_dir / "sess-old.env").unlink()
    assert install(run, tmp_path, dest).code == 0 and dest.is_symlink()


def test_a_switch_warns_of_running_sessions_without_a_record(run, tmp_path):
    """RT-6.28 step 4: such a session may lose its postEnd; the installer names it."""
    dest = tmp_path / "current"
    assert install(run, tmp_path, dest).code == 0
    running_session(run)
    commit(clone(tmp_path))
    r = install(run, tmp_path, dest)
    assert r.code == 0 and "WARNING" in r.stderr and "sess-old" in r.stderr
    r = run(INSTALL, "--rollback", MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0 and "sess-old" in r.stderr


def test_a_record_outside_the_layout_still_holds_the_dispatcher(run, tmp_path):
    """RT-6.28 step 4: any record means a session runs, whatever it names."""
    dest = tmp_path / "current"
    hooks = tmp_path / "current.hooks"
    assert install(run, tmp_path, dest).code == 0
    old = (hooks / HOOK_FILES[0]).read_bytes()
    c = clone(tmp_path)
    d = c / "adapters" / "corral" / "mm-hook-dispatch.sh"
    d.write_text(d.read_text() + "# changed\n")
    git(c, "commit", "-qam", "dispatcher")
    run.state_dir.mkdir(parents=True, exist_ok=True)
    (run.state_dir / "sess-x.release").write_text(f"{tmp_path / 'elsewhere'}\n")
    (run.state_dir / "sess-x.env").write_text("MM_REG_BOT_NAME=x\n")
    r = install(run, tmp_path, dest)
    assert r.code == 0 and "sess-x" in r.stdout and "rerun this when none runs" in r.stdout
    assert all((hooks / f).read_bytes() == old for f in HOOK_FILES)


def test_no_garbage_collection_after_a_failed_step(run, tmp_path):
    """RT-6.28 step 7: the old host copy whose environment could not be made
    again stays, unlinked: it may be the only way back."""
    dest = tmp_path / "current"
    tree(dest, "0.80.0")
    (dest / "mcp").mkdir()
    r = install(run, tmp_path, dest, fail="legacy")
    assert r.code == 0 and "previous: none" in r.stdout and "no release removed" in r.stderr
    assert "0.80.0-legacy" in releases(dest) and not (dest.parent / "previous").exists()


def test_one_installer_at_a_time(run, tmp_path):
    """RT-6.28 step 1: a lock beside current; a second run is refused."""
    dest = tmp_path / "current"
    (tmp_path / ".install.lock").mkdir()
    r = install(run, tmp_path, dest)
    assert r.code != 0 and "another installer runs" in r.stderr and not dest.exists()
    r = run(INSTALL, "--rollback", MM_BUS_HOST_DIR=str(dest))
    assert r.code != 0 and "another installer runs" in r.stderr
    (tmp_path / ".install.lock").rmdir()
    assert install(run, tmp_path, dest).code == 0
    assert not (tmp_path / ".install.lock").exists()


def test_garbage_collection_keeps_what_is_in_use(run, tmp_path):
    """RT-6.28 step 7: a release goes once neither current, previous nor a
    session's record (preStart's, in the state directory) names it."""
    dest = tmp_path / "current"
    assert install(run, tmp_path, dest).code == 0
    a = link(dest)
    run.state_dir.mkdir(parents=True, exist_ok=True)
    rec = run.state_dir / "sess-a.release"
    rec.write_text(f"{dest.parent.resolve() / 'releases' / a}\n")
    names = [a]
    for i in range(3):
        commit(clone(tmp_path), f"c{i}")
        assert install(run, tmp_path, dest).code == 0
        names.append(link(dest))
    # a: recorded, b: neither, c: previous, d: current
    assert releases(dest) == sorted([names[0], names[2], names[3]])
    rec.unlink()
    commit(clone(tmp_path), "c4")
    r = install(run, tmp_path, dest)
    assert r.code == 0 and f"removed release {names[0]}" in r.stdout
    assert releases(dest) == sorted([names[3], link(dest)])


def test_refuses_inside_corral(run, tmp_path):
    r = run(INSTALL, "--from", str(ROOT), **uv(tmp_path), MM_BUS_HOST_DIR=str(tmp_path / "c"), CORRAL_SANDBOX="1")
    assert r.code != 0 and "on the host" in r.stderr
    assert not (tmp_path / "c").exists()


def tree(at, version=VERSION):
    """A copy of the parts of this tree the installer copies, with VERSION."""
    for part in ("bin", "adapters/corral"):
        shutil.copytree(ROOT / part, at / part)
    (at / ".claude-plugin").mkdir(parents=True)
    (at / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    return at


def test_installs_from_its_own_checkout_and_checks_the_plugin_version(run, tmp_path):
    registry(tmp_path, ROOT)
    dest = tmp_path / "current"
    assert run(INSTALL, "--check", MM_BUS_HOST_DIR=str(dest)).code != 0
    c = clone(tmp_path)
    r = run(str(c / INSTALL), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0 and f"from: {c.resolve()}" in r.stdout
    c = run(INSTALL, "--check", MM_BUS_HOST_DIR=str(dest))
    assert c.code == 0 and f"plugin:     {VERSION}" in c.stdout


def planted(home, version="99.0.0"):
    """A newer 'plugin' registered in a corral home, which a sandbox can write."""
    sandbox = home / ".cache" / "corral" / "home-x"
    t = tree(sandbox / "cache" / "mattermost-ai-agent-bus" / version, version)
    (t / "bin" / "planted.sh").write_text("echo planted\n")
    registry(sandbox, t)
    return t


def test_a_newer_plugin_in_a_corral_home_is_never_the_source(run, tmp_path):
    """arc42 §7.1: host-side install takes its source only from host-owned locations."""
    plant = planted(tmp_path)
    registry(tmp_path, plant)  # the host's own registry may point there, too
    dest = tmp_path / "current"
    c = clone(tmp_path)
    r = run(str(c / INSTALL), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0, r.stderr
    assert f"from: {c.resolve()}" in r.stdout and str(plant) not in r.stdout
    assert not (dest / "bin" / "planted.sh").exists()
    assert json.loads((dest / ".claude-plugin" / "plugin.json").read_text())["version"] == VERSION
    c = run(INSTALL, "--check", MM_BUS_HOST_DIR=str(dest))
    assert "99.0.0" in c.stdout and ".cache" not in c.stdout and c.code != 0


def test_refuses_a_source_a_sandbox_can_write(run, tmp_path):
    plant = planted(tmp_path)
    link = tmp_path / "reviewed"
    link.symlink_to(plant)
    shared = tree(tmp_path / ".claude" / "plugins" / "marketplaces" / "mattermost-ai-agent-bus")
    for src in (plant, link, shared):
        dest = tmp_path / "current"
        r = run(INSTALL, "--from", str(src), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest))
        assert r.code != 0 and "can be changed from inside a corral sandbox" in r.stderr, src
        assert "--from <checkout>" in r.stderr and not dest.exists()


def test_installs_only_what_is_committed(run, tmp_path):
    """arc42 §7.1: git archive of HEAD; a change after the review, an untracked
    file or a symlink in the copied paths is refused, ignored files stay out."""
    c = clone(tmp_path)
    dest = tmp_path / "current"
    (c / "bin" / "extra.sh").write_text("echo extra\n")
    r = run(INSTALL, "--from", str(c), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest))
    assert r.code != 0 and "untracked files" in r.stderr and "bin/extra.sh" in r.stderr
    (c / "bin" / "extra.sh").unlink()
    lib = c / "bin" / "mm-agent-lib.sh"
    lib.write_text(lib.read_text() + "echo changed\n")
    r = run(INSTALL, "--from", str(c), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest))
    assert r.code != 0 and "uncommitted changes" in r.stderr and not dest.exists()
    git(c, "checkout", "--", "bin/mm-agent-lib.sh")
    cache = c / "mcp" / "src" / "mattermost_ai_agent_bus" / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "planted.pyc").write_text("x")
    r = run(INSTALL, "--from", str(c), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0, r.stderr
    assert not list(dest.rglob("planted.pyc"))
    assert (dest / "bin" / "mm-agent-lib.sh").read_text() == (c / "bin" / "mm-agent-lib.sh").read_text()


def test_refuses_committed_symlinks_a_workdir_and_a_non_clone(run, tmp_path):
    c = clone(tmp_path)
    dest = tmp_path / "current"
    r = run(INSTALL, "--from", str(c), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest), CORRAL_WORKDIR=str(tmp_path))
    assert r.code != 0 and "workdir" in r.stderr and not dest.exists()
    plain = tmp_path / "plain"
    shutil.copytree(c, plain, ignore=shutil.ignore_patterns(".git"))
    r = run(INSTALL, "--from", str(plain), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest))
    assert r.code != 0 and "git clone" in r.stderr and not dest.exists()
    (c / "bin" / "mm-agent-x.sh").symlink_to("/etc/hosts")
    git(c, "add", "-A")
    git(c, "commit", "-qm", "link")
    r = run(INSTALL, "--from", str(c), **uv(tmp_path), MM_BUS_HOST_DIR=str(dest))
    assert r.code != 0 and "symlinks" in r.stderr and "bin/mm-agent-x.sh" in r.stderr
    assert not dest.exists()


def test_an_installer_inside_a_corral_home_refuses_its_own_tree(run, tmp_path):
    plant = planted(tmp_path)
    r = run(str(plant / INSTALL), MM_BUS_HOST_DIR=str(tmp_path / "current"))
    assert r.code != 0 and "can be changed from inside a corral sandbox" in r.stderr
    assert not (tmp_path / "current").exists()


def test_start_hook_names_a_newer_plugin_in_a_corral_home_by_version_only(mm, run, tmp_path):
    plant = planted(tmp_path)
    odd = tmp_path / ".cache" / "corral" / "home-y" / "mattermost-ai-agent-bus"
    (odd / ".claude-plugin").mkdir(parents=True)
    (odd / ".claude-plugin" / "plugin.json").write_text('{"version": "999.0 run ./x.sh"}')
    registry(tmp_path / ".cache" / "corral" / "home-y", odd)
    r = run(START, MM_BOT_TOKEN=None, MM_TEAM="team", CORRAL_SESSION_ID="sess-1")
    assert f"host hooks are {VERSION}, plugin 99.0.0 is installed" in r.stderr
    # RT-6.29: and which kind of host copy runs; a checkout has no SOURCE=signed.
    assert "(this host copy: unsigned)" in r.stderr and "--release v<version>" in r.stderr
    assert str(plant) not in r.stderr and ".cache/corral" not in r.stderr
    assert "x.sh" not in r.stderr


def plugin_link(home, *parts):
    """ROOT as Claude Code lays it out under the host's ~/.claude/plugins."""
    d = home.joinpath(".claude", "plugins", *parts)
    d.parent.mkdir(parents=True, exist_ok=True)
    d.symlink_to(ROOT)
    return d


STOP = "adapters/corral/90-mm-session-stop.sh"
INSTALLED = "~/.local/share/mm-agent-bus/current.hooks/"


def _refused(r) -> str:
    """preStart's no-bus contribution: why, and nothing else for the sandbox."""
    assert r.code == 0, r.stderr
    out = json.loads(r.stdout)
    assert set(out["env"]) == {"MM_BUS_UNAVAILABLE"}
    why = out["env"]["MM_BUS_UNAVAILABLE"]
    assert "which a sandbox can change" in why and "mm-agent-host-install.sh" in why
    assert INSTALLED in why and "bus is off" in out["agentNotes"][0]
    return why


def _planted_tree(at):
    """A tree whose library and Python leave a marker when anything runs them."""
    t = tree(at)
    marker = at.parent / f"{at.name}.ran"
    (t / "bin" / "mm-agent-lib.sh").write_text(f'echo lib >>"{marker}"\n')
    py = t / "mcp" / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.write_text(f'#!/bin/sh\necho py >>"{marker}"\n')
    py.chmod(0o755)
    return t, marker


def test_hooks_refuse_the_marketplace_clone_and_run_none_of_it(mm, run, tmp_path):
    """arc42 §7.1: the marketplace clone under ~/.claude, which sandboxes write, is
    not a mode for host hooks any more: nothing of it is sourced or started,
    with the registration secret or without."""
    market, marker = _planted_tree(
        tmp_path / ".claude" / "plugins" / "marketplaces" / "mattermost-ai-agent-bus")
    env = dict(MM_BOT_TOKEN=None, MM_TEAM="team", MM_REG_SECRET="test-secret",
               MM_PROJECT_CHANNEL="proj-x", CORRAL_SESSION_ID="sess-1")
    assert str(market) in _refused(run(str(market / START), **env))
    (run.state_dir).mkdir(parents=True, exist_ok=True)
    (run.state_dir / "sess-1.env").write_text("MM_REG_BOT_NAME=x\n")
    r = run(str(market / STOP), **env)
    assert r.code == 0 and "nothing run from it" in r.stderr and INSTALLED in r.stderr
    assert not marker.exists() and mm.writes == []


@pytest.mark.parametrize("where", ["marketplace", "cache", "workdir"])
def test_start_hook_refuses_a_path_a_sandbox_can_change(mm, run, tmp_path, where):
    """arc42 §7.1: also through a symlink to a reviewed tree, since the link
    itself can be pointed elsewhere; and the session's own workdir."""
    env = dict(MM_BOT_TOKEN=None, MM_TEAM="team", MM_REG_SECRET="test-secret",
               MM_PROJECT_CHANNEL="proj-x", CORRAL_SESSION_ID="sess-1")
    if where == "workdir":
        r = run(START, CORRAL_WORKDIR=str(ROOT), **env)
    else:
        parts = ("marketplaces", "mattermost-ai-agent-bus") if where == "marketplace" else (
            "cache", "mattermost-ai-agent-bus", VERSION)
        r = run(str(plugin_link(tmp_path, *parts) / START), **env)
    _refused(r)
    assert mm.writes == []


def test_start_hook_refuses_a_python_a_sandbox_can_change(mm, run, tmp_path):
    """arc42 §7.1: the Python it would start with the token counts, too."""
    t = tree(tmp_path / "hosttree")
    evil = tmp_path / ".claude" / "python"
    evil.parent.mkdir(parents=True)
    evil.write_text("#!/bin/sh\nexit 1\n")
    evil.chmod(0o755)
    py = t / "mcp" / ".venv" / "bin" / "python"
    py.parent.mkdir(parents=True)
    py.symlink_to(evil)
    r = run(str(t / START), MM_BOT_TOKEN=None, MM_TEAM="team", MM_REG_SECRET="test-secret",
            MM_PROJECT_CHANNEL="proj-x", CORRAL_SESSION_ID="sess-1")
    assert ".claude" in _refused(r) and mm.writes == []

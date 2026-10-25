"""Host copy of the corral hooks (docs/arc42/07-deployment-view.md, §7.1).

The hooks run on the host with the registration secret; they must not run from
a tree a sandbox can write (the plugin directory, the session's workdir).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import ROOT

INSTALL = "bin/mm-agent-host-install.sh"
# uv sync in the installer: offline, from the developer's cache when there is one.
UV = {"UV_OFFLINE": "1", **({"UV_CACHE_DIR": os.environ["UV_CACHE_DIR"]}
                           if os.environ.get("UV_CACHE_DIR") else {})}
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
    r = run(INSTALL, "--from", str(clone(tmp_path)), **UV, MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0, r.stderr
    for f in ("adapters/corral/10-mm-session-start.sh", "adapters/corral/90-mm-session-stop.sh",
              "bin/mm-agent-lib.sh", "bin/mm-agent-register.sh"):
        assert os.access(dest / f, os.X_OK if f.endswith(".sh") else os.R_OK), f
    assert f"{dest}/adapters/corral/10-mm-session-start.sh" in r.stdout
    assert VERSION in r.stdout
    # RT-6.18: the MCP server goes along, to run on the host; a checkout's caches do not.
    assert (dest / "mcp" / "src" / "mattermost_ai_agent_bus" / "hosted.py").is_file()
    assert (dest / "mcp" / "uv.lock").is_file() and (dest / "skills" / "mattermost-ai-agent-bus").is_dir()
    assert not list((dest / "mcp" / "src").rglob("__pycache__"))
    assert "MCP server environment ready" in r.stdout or "WARNING: no Python environment" in r.stdout


def test_reinstall_replaces_the_whole_tree(run, tmp_path):
    dest = tmp_path / "current"
    run(INSTALL, "--from", str(clone(tmp_path)), **UV, MM_BUS_HOST_DIR=str(dest))
    (dest / "bin" / "stray.sh").write_text("echo old\n")
    assert run(INSTALL, "--from", str(clone(tmp_path)), **UV, MM_BUS_HOST_DIR=str(dest)).code == 0
    assert not (dest / "bin" / "stray.sh").exists()
    assert not any(p.name.startswith((".old", ".install")) for p in dest.parent.iterdir())


def test_refuses_inside_corral(run, tmp_path):
    r = run(INSTALL, "--from", str(ROOT), **UV, MM_BUS_HOST_DIR=str(tmp_path / "c"), CORRAL_SANDBOX="1")
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
    r = run(str(c / INSTALL), **UV, MM_BUS_HOST_DIR=str(dest))
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
    r = run(str(c / INSTALL), **UV, MM_BUS_HOST_DIR=str(dest))
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
        r = run(INSTALL, "--from", str(src), **UV, MM_BUS_HOST_DIR=str(dest))
        assert r.code != 0 and "can be changed from inside a corral sandbox" in r.stderr, src
        assert "--from <checkout>" in r.stderr and not dest.exists()


def test_installs_only_what_is_committed(run, tmp_path):
    """arc42 §7.1: git archive of HEAD; a change after the review, an untracked
    file or a symlink in the copied paths is refused, ignored files stay out."""
    c = clone(tmp_path)
    dest = tmp_path / "current"
    (c / "bin" / "extra.sh").write_text("echo extra\n")
    r = run(INSTALL, "--from", str(c), **UV, MM_BUS_HOST_DIR=str(dest))
    assert r.code != 0 and "untracked files" in r.stderr and "bin/extra.sh" in r.stderr
    (c / "bin" / "extra.sh").unlink()
    lib = c / "bin" / "mm-agent-lib.sh"
    lib.write_text(lib.read_text() + "echo changed\n")
    r = run(INSTALL, "--from", str(c), **UV, MM_BUS_HOST_DIR=str(dest))
    assert r.code != 0 and "uncommitted changes" in r.stderr and not dest.exists()
    git(c, "checkout", "--", "bin/mm-agent-lib.sh")
    cache = c / "mcp" / "src" / "mattermost_ai_agent_bus" / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "planted.pyc").write_text("x")
    r = run(INSTALL, "--from", str(c), **UV, MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0, r.stderr
    assert not list(dest.rglob("planted.pyc"))
    assert (dest / "bin" / "mm-agent-lib.sh").read_text() == (c / "bin" / "mm-agent-lib.sh").read_text()


def test_refuses_committed_symlinks_a_workdir_and_a_non_clone(run, tmp_path):
    c = clone(tmp_path)
    dest = tmp_path / "current"
    r = run(INSTALL, "--from", str(c), **UV, MM_BUS_HOST_DIR=str(dest), CORRAL_WORKDIR=str(tmp_path))
    assert r.code != 0 and "workdir" in r.stderr and not dest.exists()
    plain = tmp_path / "plain"
    shutil.copytree(c, plain, ignore=shutil.ignore_patterns(".git"))
    r = run(INSTALL, "--from", str(plain), **UV, MM_BUS_HOST_DIR=str(dest))
    assert r.code != 0 and "git clone" in r.stderr and not dest.exists()
    (c / "bin" / "mm-agent-x.sh").symlink_to("/etc/hosts")
    git(c, "add", "-A")
    git(c, "commit", "-qm", "link")
    r = run(INSTALL, "--from", str(c), **UV, MM_BUS_HOST_DIR=str(dest))
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
    assert str(plant) not in r.stderr and ".cache/corral" not in r.stderr
    assert "x.sh" not in r.stderr


def plugin_link(home, *parts):
    """ROOT as Claude Code lays it out under the host's ~/.claude/plugins."""
    d = home.joinpath(".claude", "plugins", *parts)
    d.parent.mkdir(parents=True, exist_ok=True)
    d.symlink_to(ROOT)
    return d


STOP = "adapters/corral/90-mm-session-stop.sh"
INSTALLED = "~/.local/share/mm-agent-bus/current/adapters/corral/"


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

"""Host copy of the corral hooks (docs/arc42/07-deployment-view.md, §7.1).

The hooks run on the host with the registration secret; they must not run from
a tree a sandbox can write (the plugin directory, the session's workdir).
"""

from __future__ import annotations

import json
import os
import shutil

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


def test_installs_hooks_and_libs_out_of_the_sandbox(run, tmp_path):
    dest = tmp_path / "host" / "current"
    r = run(INSTALL, "--from", str(ROOT), **UV, MM_BUS_HOST_DIR=str(dest))
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
    run(INSTALL, "--from", str(ROOT), **UV, MM_BUS_HOST_DIR=str(dest))
    (dest / "bin" / "stray.sh").write_text("echo old\n")
    assert run(INSTALL, "--from", str(ROOT), **UV, MM_BUS_HOST_DIR=str(dest)).code == 0
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
    r = run(INSTALL, **UV, MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0 and f"from: {ROOT.resolve()}" in r.stdout
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
    r = run(INSTALL, **UV, MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0, r.stderr
    assert f"from: {ROOT.resolve()}" in r.stdout and str(plant) not in r.stdout
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


def test_an_installer_inside_a_corral_home_refuses_its_own_tree(run, tmp_path):
    plant = planted(tmp_path)
    r = run(str(plant / INSTALL), MM_BUS_HOST_DIR=str(tmp_path / "current"))
    assert r.code != 0 and "can be changed from inside a corral sandbox" in r.stderr
    assert not (tmp_path / "current").exists()


def test_start_hook_warns_about_writable_or_outdated_hooks(mm, run, tmp_path):
    newer = tmp_path / "plugins" / "mattermost-ai-agent-bus" / "99.0.0"
    (newer / ".claude-plugin").mkdir(parents=True)
    (newer / ".claude-plugin" / "plugin.json").write_text('{"version": "99.0.0"}')
    registry(tmp_path, newer)
    r = run(START, CORRAL_WORKDIR=str(ROOT), MM_BOT_TOKEN=None, MM_TEAM="team",
            CORRAL_SESSION_ID="sess-1")
    assert "this session's own workdir" in r.stderr
    assert f"host hooks are {VERSION}, plugin 99.0.0 is installed" in r.stderr
    assert "mm-agent-host-install.sh --from <checkout>" in r.stderr


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


def test_start_hook_from_the_marketplace_clone_follows_updates_quietly(mm, run, tmp_path):
    """arc42 §7.1: exec pointed at the host's marketplace clone is a choice, not a mistake."""
    market = plugin_link(tmp_path, "marketplaces", "mattermost-ai-agent-bus")
    registry(tmp_path, market)
    r = run(str(market / START), MM_BOT_TOKEN=None, MM_TEAM="team", CORRAL_SESSION_ID="sess-1")
    assert r.code == 0, r.stderr
    assert "WARNING" not in r.stderr and "host hooks" not in r.stderr
    assert "mm-bus: no bot for this session: " in r.stderr  # the hook itself ran


def test_start_hook_from_the_marketplace_says_when_the_plugin_is_newer(mm, run, tmp_path):
    market = plugin_link(tmp_path, "marketplaces", "mattermost-ai-agent-bus")
    newer = tmp_path / "plugins-cache" / "mattermost-ai-agent-bus" / "99.0.0"
    (newer / ".claude-plugin").mkdir(parents=True)
    (newer / ".claude-plugin" / "plugin.json").write_text('{"version": "99.0.0"}')
    registry(tmp_path, newer)
    r = run(str(market / START), MM_BOT_TOKEN=None, MM_TEAM="team", CORRAL_SESSION_ID="sess-1")
    assert f"host hooks (marketplace) are {VERSION}, the installed plugin is 99.0.0" in r.stderr
    assert "/plugin update" in r.stderr


def test_start_hook_from_a_versioned_plugin_directory_still_warns(mm, run, tmp_path):
    plugin = plugin_link(tmp_path, "cache", "mattermost-ai-agent-bus", VERSION)
    r = run(str(plugin / START), MM_BOT_TOKEN=None, MM_TEAM="team", CORRAL_SESSION_ID="sess-1")
    assert "hooks run from the plugin directory" in r.stderr

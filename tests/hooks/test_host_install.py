"""Host copy of the corral hooks (docs/arc42/07-deployment-view.md, §7.1).

The hooks run on the host with the registration secret; they must not run from
a tree a sandbox can write (the plugin directory, the session's workdir).
"""

from __future__ import annotations

import json
import os

from conftest import ROOT

INSTALL = "bin/mm-agent-host-install.sh"
START = "adapters/corral/10-mm-session-start.sh"
VERSION = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text())["version"]


def registry(home, install_path) -> None:
    f = home / ".claude" / "plugins" / "installed_plugins.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"plugins": {"mattermost-ai-agent-bus@x": [
        {"installPath": str(install_path)}]}}))


def test_installs_hooks_and_libs_out_of_the_sandbox(run, tmp_path):
    dest = tmp_path / "host" / "current"
    r = run(INSTALL, "--from", str(ROOT), MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0, r.stderr
    for f in ("adapters/corral/10-mm-session-start.sh", "adapters/corral/90-mm-session-stop.sh",
              "bin/mm-agent-lib.sh", "bin/mm-agent-register.sh"):
        assert os.access(dest / f, os.X_OK if f.endswith(".sh") else os.R_OK), f
    assert f"{dest}/adapters/corral/10-mm-session-start.sh" in r.stdout
    assert VERSION in r.stdout


def test_reinstall_replaces_the_whole_tree(run, tmp_path):
    dest = tmp_path / "current"
    run(INSTALL, "--from", str(ROOT), MM_BUS_HOST_DIR=str(dest))
    (dest / "bin" / "stray.sh").write_text("echo old\n")
    assert run(INSTALL, "--from", str(ROOT), MM_BUS_HOST_DIR=str(dest)).code == 0
    assert not (dest / "bin" / "stray.sh").exists()
    assert not any(p.name.startswith((".old", ".install")) for p in dest.parent.iterdir())


def test_refuses_inside_corral(run, tmp_path):
    r = run(INSTALL, "--from", str(ROOT), MM_BUS_HOST_DIR=str(tmp_path / "c"), CORRAL_SANDBOX="1")
    assert r.code != 0 and "on the host" in r.stderr
    assert not (tmp_path / "c").exists()


def test_finds_the_installed_plugin_and_checks_its_version(run, tmp_path):
    registry(tmp_path, ROOT)
    dest = tmp_path / "current"
    assert run(INSTALL, "--check", MM_BUS_HOST_DIR=str(dest)).code != 0
    r = run(INSTALL, MM_BUS_HOST_DIR=str(dest))
    assert r.code == 0 and f"from: {ROOT}" in r.stdout
    assert run(INSTALL, "--check", MM_BUS_HOST_DIR=str(dest)).code == 0


def test_start_hook_warns_about_writable_or_outdated_hooks(mm, run, tmp_path):
    newer = tmp_path / "plugins" / "mattermost-ai-agent-bus" / "99.0.0"
    (newer / ".claude-plugin").mkdir(parents=True)
    (newer / ".claude-plugin" / "plugin.json").write_text('{"version": "99.0.0"}')
    registry(tmp_path, newer)
    r = run(START, CORRAL_WORKDIR=str(ROOT), MM_BOT_TOKEN=None, MM_TEAM="team",
            CORRAL_SESSION_ID="sess-1")
    assert "this session's own workdir" in r.stderr
    assert f"host hooks are {VERSION}, the installed plugin is 99.0.0" in r.stderr

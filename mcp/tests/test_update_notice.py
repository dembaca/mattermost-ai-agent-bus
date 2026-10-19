"""bus_info names a newer installed plugin (RT-6.2): a session keeps its version."""

import json

from mattermost_ai_agent_bus import server


def _install(home, version):
    d = home / "cache" / "mattermost-ai-agent-bus" / version
    (d / ".claude-plugin").mkdir(parents=True)
    (d / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    entries = json.loads(reg.read_text())["plugins"]["x"] if reg.exists() else []
    entries.append({"installPath": str(d)})
    reg.write_text(json.dumps({"plugins": {"x": entries}}))


def test_newer_installed_version_is_found(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(server, "VERSION", "0.25.1")
    _install(tmp_path, "0.24.0")
    assert server._newer_installed() is None
    _install(tmp_path, "0.26.0")
    _install(tmp_path, "0.9.0")  # numeric, not lexical order
    assert server._newer_installed() == "0.26.0"


def test_no_registry_no_update(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert server._newer_installed() is None

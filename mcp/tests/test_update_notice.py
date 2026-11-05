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


def test_the_restart_is_by_resume_with_the_own_session_id(monkeypatch):
    """RT-6.2 step 8: `--continue` takes the directory's newest session, which
    may be another one's; `update.how` names `--resume` and this session's id."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "529416e5-aaaa")
    how = server._restart_how()
    assert "claude --resume 529416e5-aaaa" in how
    assert "corral run -- --resume 529416e5-aaaa" in how
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert "claude --resume and pick this session" in server._restart_how()

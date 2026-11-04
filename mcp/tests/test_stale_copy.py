"""RT-6.10 step 1 (ADR-007 amendment step 3): a server whose version is not the
installed plugin's registers no bot and takes none over; bus_info says why."""

import json

import pytest

from mattermost_ai_agent_bus import server, session_file


def _plugin(tree, version):
    (tree / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (tree / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    return tree


def _install(home, version):
    """installed_plugins.json names the plugin cache at `version`."""
    cache = _plugin(home / ".claude" / "plugins" / "cache" / "mattermost-ai-agent-bus" / version, version)
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"mattermost-ai-agent-bus": [{"installPath": str(cache)}]}}))
    return cache


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(session_file, "hosted", lambda: False)
    return tmp_path


def _code(tree):
    f = tree / "mcp" / "src" / "mattermost_ai_agent_bus" / "server.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("")
    return f


def test_a_copy_in_the_plugin_cache_that_is_not_the_installed_one_is_stale(home):
    """Case 2 of ADR-007: an old server from the plugin cache, whichever runtime started it."""
    installed = _install(home, "0.71.6")
    old = _plugin(home / ".claude" / "plugins" / "cache" / "mattermost-ai-agent-bus" / "0.42.2", "0.42.2")
    reason = server._stale_reason(_code(old), "0.42.2")
    assert reason.startswith("stale: this server is 0.42.2 from ")
    assert str(old) in reason and reason.endswith("installed is 0.71.6")
    assert server._stale_reason(_code(installed), "0.71.6") == ""


def test_the_installed_version_is_the_newest_the_file_names(home):
    _install(home, "0.70.0")
    newer = _install(home, "0.71.6")
    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    older = home / ".claude" / "plugins" / "cache" / "mattermost-ai-agent-bus" / "0.70.0"
    reg.write_text(json.dumps({"plugins": {"x": [{"installPath": str(older)}, {"installPath": str(newer)}]}}))
    assert server._stale_reason(_code(newer), "0.71.6") == ""
    assert "installed is 0.71.6" in server._stale_reason(_code(older), "0.70.0")


def test_an_environment_in_a_checkout_is_checked_against_that_checkout(tmp_path):
    """A repository's .venv with an older copy than the checkout (case 2)."""
    repo = _plugin(tmp_path / "work" / "repo", "0.71.6")
    venv = repo / "mcp" / ".venv" / "lib" / "python3.13" / "site-packages" / "mattermost_ai_agent_bus" / "server.py"
    venv.parent.mkdir(parents=True)
    venv.write_text("")
    assert "this server is 0.69.0" in server._stale_reason(venv, "0.69.0")
    assert server._stale_reason(venv, "0.71.6") == ""
    assert server._stale_reason(_code(repo), "0.71.6") == ""  # the checkout itself, as a developer runs it


def test_no_installed_version_means_no_check(home, tmp_path, caplog):
    """RT-6.10 step 1: neither a registry nor a tree with a plugin.json, or one that cannot be read."""
    plain = tmp_path / "elsewhere" / "server.py"
    plain.parent.mkdir(parents=True)
    plain.write_text("")
    with caplog.at_level("INFO"):
        assert server._stale_reason(plain, "0.1.0") == ""
    assert "not checked" in caplog.text
    cached = home / ".claude" / "plugins" / "cache" / "x" / "0.1.0"
    cached.mkdir(parents=True)
    assert server._stale_reason(_code(cached), "0.1.0") == ""  # under the plugins, no registry
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text("{not json")
    assert server._stale_reason(_code(cached), "0.1.0") == ""
    tree = _plugin(tmp_path / "broken", "0.71.6")
    (tree / ".claude-plugin" / "plugin.json").write_text("{")
    assert server._stale_reason(_code(tree), "0.1.0") == ""


def test_the_host_side_of_a_keyless_session_is_left_to_prestart(home, monkeypatch):
    """RT-6.18 step 1: the host process reads no installed_plugins.json, which the sandbox can write."""
    _install(home, "0.71.6")
    old = _plugin(home / ".claude" / "plugins" / "cache" / "mattermost-ai-agent-bus" / "0.42.2", "0.42.2")
    monkeypatch.setattr(session_file, "hosted", lambda: True)
    assert server._stale_reason(_code(old), "0.42.2") == ""


STALE = "stale: this server is 0.42.2 from /x/cache/0.42.2; installed is 0.71.6"


@pytest.mark.asyncio
async def test_a_stale_server_has_no_client_registers_no_bot_and_says_why(monkeypatch):
    monkeypatch.setattr(server, "STALE", STALE)
    monkeypatch.setattr(server, "_client", None)
    with pytest.raises(RuntimeError, match="stale: this server is 0.42.2"):
        server._get_client()
    assert await server._bus_client() is None
    info = json.loads(await server.bus_info())
    assert info["on_bus"] is False and info["reason"] == STALE
    assert "restart" in info["how"] and "session_start" not in info["how"]
    with pytest.raises(RuntimeError, match="stale"):
        await server.session_start("proj")
    assert server._client is None  # nothing was created, nothing taken over


@pytest.mark.asyncio
async def test_a_stale_server_still_serves_the_hook_api_which_answers_503(monkeypatch):
    monkeypatch.setattr(server, "STALE", STALE)
    monkeypatch.setattr(server, "_hook_api", None)
    api = await server._start_hook_api()
    assert api is not None
    try:
        assert await api._get_client() is None  # the hook API's 503 "no bus session" (RT-6.17)
    finally:
        await api.close()


def test_a_current_server_is_unchanged(monkeypatch):
    """Parity: no STALE, the client as before."""
    assert server.STALE == ""

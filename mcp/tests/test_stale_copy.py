"""RT-6.10 step 1 (ADR-007 amendment step 3): a server older than the installed
plugin registers no bot and takes none over; bus_info says why. Only older, and
only when sure: a current session must never fall off the bus."""

import json

import pytest

from mattermost_ai_agent_bus import server, session_file

KEY = "mattermost-ai-agent-bus@mattermost-ai-agent-bus"


def _plugin(tree, version):
    (tree / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (tree / ".claude-plugin" / "plugin.json").write_text(json.dumps({"version": version}))
    return tree


def _cache(home, version, marketplace="mattermost-ai-agent-bus"):
    """A cache folder as Claude Code lays it out: cache/<marketplace>/<plugin>/<version>."""
    return _plugin(home / ".claude" / "plugins" / "cache" / marketplace / "mattermost-ai-agent-bus" / version, version)


def _registry(home, **keys):
    """installed_plugins.json, schema version 2: key plugin@marketplace, entries with scope and installPath."""
    reg = home / ".claude" / "plugins" / "installed_plugins.json"
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text(json.dumps({"version": 2, "plugins": {
        k: [{"scope": scope, "installPath": str(path), "version": path.name} for scope, path in entries]
        for k, entries in keys.items()}}))


@pytest.fixture(autouse=True)
def home(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(session_file, "hosted", lambda: False)
    return tmp_path


def _code(tree):
    f = tree / "mcp" / "src" / "mattermost_ai_agent_bus" / "server.py"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("")
    return f


def test_a_cache_copy_older_than_every_entry_is_stale(home):
    """Case 2 of ADR-007: an old server from the plugin cache, whichever runtime started it."""
    old, installed = _cache(home, "0.42.2"), _cache(home, "0.71.6")
    _registry(home, **{KEY: [("user", installed)]})
    reason = server._stale_reason(_code(old), "0.42.2")
    assert reason.startswith("stale: this server is 0.42.2 from ") and str(old) in reason
    assert reason.endswith("installed is 0.71.6")
    assert server._stale_reason(_code(installed), "0.71.6") == ""


def test_the_tree_of_any_entry_is_current_whatever_the_others_say(home):
    """Scopes: a project install elsewhere (newer) or a pin (older) must not make the user's session stale."""
    user, project, pin = _cache(home, "0.71.9"), _cache(home, "0.72.0", "other"), _cache(home, "0.70.0", "pin")
    _registry(home, **{KEY: [("user", user), ("project", project)], "mattermost-ai-agent-bus@pin": [("local", pin)]})
    for tree, version in ((user, "0.71.9"), (project, "0.72.0"), (pin, "0.70.0")):
        assert server._stale_reason(_code(tree), version) == ""


def test_a_new_cache_folder_before_the_registry_rewrite_is_not_stale(home):
    """The new cache folder 0.71.10 runs (a /mcp reconnect after another session's update) while the
    registry still names 0.71.9: newer than every entry is legitimate."""
    new, current = _cache(home, "0.71.10"), _cache(home, "0.71.9")
    _registry(home, **{KEY: [("user", current)]})
    assert server._stale_reason(_code(new), "0.71.10") == ""
    # The reverse is a genuine older copy: started from the old folder after the registry
    # moved on (a launch that raced the update). It stays off the bus and says to restart.
    _registry(home, **{KEY: [("user", new)]})
    assert "installed is 0.71.10" in server._stale_reason(_code(current), "0.71.9")


def test_only_this_plugins_entries_count(home):
    """Another plugin whose name contains ours, or a second marketplace of ours."""
    other = _cache(home, "9.9.9", "other")
    mine = _cache(home, "0.71.6")
    _registry(home, **{KEY: [("user", mine)], "not-mattermost-ai-agent-bus@m": [("user", other)],
                       "x-mattermost-ai-agent-bus@m": [("user", other)]})
    assert server._stale_reason(_code(mine), "0.71.6") == ""
    old = _cache(home, "0.70.0", "third")
    assert "installed is 0.71.6" in server._stale_reason(_code(old), "0.70.0")


def test_the_plugin_root_claude_code_names_is_not_checked(home, monkeypatch):
    """Claude Code runs this very tree: it is the installed one, whatever the registry says."""
    old, newer = _cache(home, "0.42.2"), _cache(home, "0.71.6")
    _registry(home, **{KEY: [("user", newer)]})
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(old))
    assert server._stale_reason(_code(old), "0.42.2") == ""
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(newer))  # names another tree: the registry rule
    assert server._stale_reason(_code(old), "0.42.2") != ""


def test_the_marketplace_clone_follows_the_checkout_rule(home):
    """The clone under plugins/marketplaces is no cache folder: its own tree's version counts."""
    clone = _plugin(home / ".claude" / "plugins" / "marketplaces" / "mattermost-ai-agent-bus", "0.71.9")
    _registry(home, **{KEY: [("user", _cache(home, "0.70.0"))]})
    assert server._stale_reason(_code(clone), "0.71.9") == ""
    assert "this server is 0.69.0" in server._stale_reason(_code(clone), "0.69.0")


def test_an_environment_in_a_checkout_is_checked_against_that_checkout(tmp_path):
    """A repository's .venv with an older copy than the checkout (case 2); a newer one is no stale copy."""
    repo = _plugin(tmp_path / "work" / "repo", "0.71.6")
    venv = repo / "mcp" / ".venv" / "lib" / "python3.13" / "site-packages" / "mattermost_ai_agent_bus" / "server.py"
    venv.parent.mkdir(parents=True)
    venv.write_text("")
    assert "this server is 0.69.0" in server._stale_reason(venv, "0.69.0")
    assert server._stale_reason(venv, "0.71.6") == ""
    assert server._stale_reason(venv, "0.72.0") == ""  # a clone ahead of its own plugin.json
    assert server._stale_reason(_code(repo), "0.71.6") == ""  # the checkout itself, as a developer runs it


def test_no_installed_version_means_no_check(home, tmp_path, caplog):
    """RT-6.10 step 1: neither a registry nor a tree with a plugin.json, or one that cannot be read."""
    plain = tmp_path / "elsewhere" / "server.py"
    plain.parent.mkdir(parents=True)
    plain.write_text("")
    with caplog.at_level("INFO"):
        assert server._stale_reason(plain, "0.1.0") == ""
    assert "not checked" in caplog.text
    cached = _cache(home, "0.1.0")
    assert server._stale_reason(_code(cached), "0.1.0") == ""  # under the cache, no registry
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text("{not json")
    assert server._stale_reason(_code(cached), "0.1.0") == ""
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text('{"plugins": []}')
    assert server._stale_reason(_code(cached), "0.1.0") == ""
    tree = _plugin(tmp_path / "broken", "0.71.6")
    (tree / ".claude-plugin" / "plugin.json").write_text("{")
    assert server._stale_reason(_code(tree), "0.1.0") == ""


def test_the_host_side_of_a_keyless_session_is_left_to_prestart(home, monkeypatch):
    """RT-6.18 step 1: the host process reads no installed_plugins.json, which the sandbox can write."""
    old, installed = _cache(home, "0.42.2"), _cache(home, "0.71.6")
    _registry(home, **{KEY: [("user", installed)]})
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


@pytest.mark.asyncio
async def test_an_update_while_running_keeps_the_bot(home, monkeypatch):
    """RT-6.10 step 1: the check runs once, at start; a registry rewritten later
    changes nothing for a server that was current."""
    cur, newer = _cache(home, "0.71.9"), _cache(home, "0.71.10")
    _registry(home, **{KEY: [("user", cur)]})
    monkeypatch.setattr(server, "STALE", server._stale_reason(_code(cur), "0.71.9"))
    assert server.STALE == ""
    _registry(home, **{KEY: [("user", newer)]})  # the update lands
    assert server.STALE == ""
    seen = []
    monkeypatch.setattr(server, "_client", None)
    monkeypatch.setattr(server.MattermostAdapter, "from_env", classmethod(lambda cls: seen.append(1) or object()))
    assert server._get_client() is not None and seen == [1]

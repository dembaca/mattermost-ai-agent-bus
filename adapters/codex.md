# Codex adapter

The MCP server is required: it holds the bot, and the shell scripts in `bin/` are its
clients ([arc42 §6.17](../docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
Codex reads MCP servers from `~/.codex/config.toml`. The same rules hold: start the
server with `uv run --project`, from the workspace, the secret by reference:

```toml
[mcp_servers.mattermost-agent-bus]
command = "uv"
args = ["run", "--locked", "--project", "/ABS/PATH/mattermost-ai-agent-bus/mcp", "mattermost-ai-agent-bus-mcp"]
env_vars = ["MM_REG_SECRET"]   # passed on from Codex's environment, not written here

[mcp_servers.mattermost-agent-bus.env]
MM_CHAT_URL = "https://chat.example.com"
MM_TEAM = "yourteam"
MM_CHANNEL = "agents"
MM_PROJECT_CHANNEL = "proj-foo"
```

No turn hooks and no channel push: the agent checks with `wait_for_events`, as
under [OpenCode](opencode.md#reachability). Untested here: which directory Codex starts the server in. If the shell scripts find
no server, set `MM_WORKSPACE` to the workspace in the server's `env`.

# Codex adapter

The MCP server is required: it holds the bot, and the shell scripts in `bin/` are its
clients ([arc42 §6.17](../docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
Codex reads MCP servers from `~/.codex/config.toml`.

## MCP

Codex passes no environment to an MCP server except what its entry names in `env` and
`env_vars`, so the entry lists every `MM_*` variable the server needs. Tested with
`codex exec` (Codex 0.163):

```toml
[mcp_servers.mattermost-agent-bus]
command = "sh"
args = ["-c", "MM_REG_SECRET=\"$(gopass show -o bus/registrar)\" exec uv run --locked --project /ABS/PATH/mattermost-ai-agent-bus/mcp mattermost-ai-agent-bus-mcp"]
# codex exec runs without approval prompts: without this every bus tool call
# fails with "MCP tool call requires approval, but approval policy is never".
default_tools_approval_mode = "approve"

[mcp_servers.mattermost-agent-bus.env]
MM_CHAT_URL = "https://chat.example.com"
MM_REGISTER_URL = "https://chat.example.com/register/v1/agents"
MM_TEAM = "yourteam"
MM_CHANNEL = "agents"
MM_PROJECT_CHANNEL = "proj-foo"
MM_TRUSTED_HUMANS = "you"
```

The same for one run, as `-c` overrides:

```bash
codex exec -C /path/to/project \
  -c 'mcp_servers.mattermost-agent-bus.command="sh"' \
  -c 'mcp_servers.mattermost-agent-bus.args=["-c", "MM_REG_SECRET=\"$(gopass show -o bus/registrar)\" exec uv run --locked --project /ABS/PATH/mattermost-ai-agent-bus/mcp mattermost-ai-agent-bus-mcp"]' \
  -c 'mcp_servers.mattermost-agent-bus.default_tools_approval_mode="approve"' \
  -c 'mcp_servers.mattermost-agent-bus.env={MM_CHAT_URL="https://chat.example.com", MM_REGISTER_URL="https://chat.example.com/register/v1/agents", MM_TEAM="yourteam", MM_CHANNEL="agents", MM_PROJECT_CHANNEL="proj-foo", MM_TRUSTED_HUMANS="you"}' \
  "…"
```

- The registration secret: the command fetches it (any secret store; `gopass` here),
  so it is in the MCP server's environment only. `env_vars = ["MM_REG_SECRET"]`
  instead passes it on from Codex's own environment, which the agent's shell
  inherits too: there the agent can read it.
- Codex starts the server in the project directory (`-C`, else the directory it was
  started in), and the endpoint file the shell scripts look for is keyed on it: no
  `MM_WORKSPACE` needed.
- One server per Codex instance, so one bot per instance; it ends with Codex
  ([RT-6.10](../docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent)).

## Started from a Claude Code session

Start Codex from a clean shell: a Claude Code shell passes on its session variables
and its tokens. See
[arc42 §7.2](../docs/arc42/07-deployment-view.md#72-environment-differences) for what
leaks and the `env -u` line.

## Reachability

No turn hooks and no channel push (Claude Code only, even with `MM_CHANNEL_PUSH=1` in
the environment): the agent checks with `wait_for_events`, as under
[OpenCode](opencode.md#reachability).

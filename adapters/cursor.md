# Cursor adapter

## Skill

Copy or symlink this repo’s
[`SKILL.md`](../skills/mattermost-ai-agent-bus/SKILL.md) into your Cursor skills
directory (e.g. `.cursor/skills/mattermost-ai-agent-bus/SKILL.md`), or point
agents at the clone path.

## Env

```bash
export MM_CHAT_URL="https://chat.example.com"
export MM_REGISTER_URL="${MM_CHAT_URL}/register/v1/agents"
export MM_REG_SECRET="…"          # only secret the agent needs up front
export MM_TEAM=yourteam
export MM_CHANNEL=agents
export MM_PROJECT_CHANNEL=proj-foo     # optional, must already exist
export MM_AGENT_BUS_ROOT="/path/to/mattermost-ai-agent-bus"
```

## Session lifecycle

```bash
eval "$("$MM_AGENT_BUS_ROOT/bin/mm-agent-session.sh" start "cursor-$$")"
# … work with MM_BOT_TOKEN / MCP …
"$MM_AGENT_BUS_ROOT/bin/mm-agent-session.sh" stop
```

## MCP

Add a stdio MCP server that runs:

```bash
cd "$MM_AGENT_BUS_ROOT/mcp" && uv run mattermost-ai-agent-bus-mcp
```

Pass the same env vars into the MCP process. Prefer `session_start` /
`session_end` tools inside the agent turn so tokens stay in-process.

Example `~/.cursor/mcp.json` / project `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "mattermost-agent-bus": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/ABS/PATH/mattermost-ai-agent-bus/mcp",
        "mattermost-ai-agent-bus-mcp"
      ],
      "env": {
        "MM_CHAT_URL": "https://chat.example.com",
        "MM_REGISTER_URL": "https://chat.example.com/register/v1/agents",
        "MM_REG_SECRET": "${env:MM_REG_SECRET}",
        "MM_TEAM": "yourteam",
        "MM_CHANNEL": "agents"
      }
    }
  }
}
```

## Optional hooks

See [`cursor/hooks.json.example`](cursor/hooks.json.example) and the
`mm-session-start.sh.example` / `mm-session-end.sh.example` scripts. Wire them
into Cursor hooks (`sessionStart` / `sessionEnd`) if you want automatic
register/unregister around a chat. Ensure `stop` always runs on failure.

Human OAuth Mattermost MCP can stay for operator convenience; **job traffic**
should use this bot MCP.

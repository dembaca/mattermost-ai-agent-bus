# Claude Code adapter

## Skill

```bash
mkdir -p ~/.claude/skills/mattermost-ai-agent-bus
ln -sf /path/to/mattermost-ai-agent-bus/SKILL.md \
  ~/.claude/skills/mattermost-ai-agent-bus/SKILL.md
```

## Env (shell before `claude`)

```bash
export MM_CHAT_URL="https://chat.example.com"
export MM_REGISTER_URL="${MM_CHAT_URL}/register/v1/agents"
export MM_REG_SECRET="…"
export MM_TEAM=yourteam
export MM_CHANNEL=agents
export MM_AGENT_BUS_ROOT="/path/to/mattermost-ai-agent-bus"
```

## Session

```bash
eval "$("$MM_AGENT_BUS_ROOT/bin/mm-agent-session.sh" start "claude-$$")"
trap '"$MM_AGENT_BUS_ROOT/bin/mm-agent-session.sh" stop' EXIT
```

## MCP (optional)

```json
{
  "mcpServers": {
    "mattermost-ai-agent-bus": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/mattermost-ai-agent-bus/mcp",
        "run",
        "mattermost-ai-agent-bus-mcp"
      ],
      "env": {
        "MM_CHAT_URL": "https://chat.example.com",
        "MM_REGISTER_URL": "https://chat.example.com/register/v1/agents",
        "MM_REG_SECRET": "${MM_REG_SECRET}",
        "MM_TEAM": "yourteam",
        "MM_CHANNEL": "agents"
      }
    }
  }
}
```

Or: `claude mcp add mattermost-agent-bus -- uv run --directory …/mcp mattermost-ai-agent-bus-mcp`

Use `wait_for_events` for mentions/DMs; reply with `reply_in_thread`.

# OpenCode adapter

## Skill / instructions

Point OpenCode at [`SKILL.md`](../skills/mattermost-ai-agent-bus/SKILL.md) or paste
the contract into the agent system prompt: register with `MM_REG_SECRET` only → work
in `MM_TEAM`/`MM_CHANNEL` threads → always unregister.

## Env

```bash
export MM_CHAT_URL="https://chat.example.com"
export MM_REGISTER_URL="${MM_CHAT_URL}/register/v1/agents"
export MM_REG_SECRET="…"
export MM_TEAM=yourteam
export MM_CHANNEL=agents
export MM_PROJECT_CHANNEL=proj-foo     # optional, must already exist
export MM_AGENT_BUS_ROOT="/path/to/mattermost-ai-agent-bus"
```

## CLI session (shell tools)

```bash
eval "$("$MM_AGENT_BUS_ROOT/bin/mm-agent-session.sh" start "opencode-$$")"
"$MM_AGENT_BUS_ROOT/bin/mm-agent-poll.sh" --once
"$MM_AGENT_BUS_ROOT/bin/mm-agent-post.sh" "$CHANNEL_ID" "ack" "$ROOT_ID"
"$MM_AGENT_BUS_ROOT/bin/mm-agent-session.sh" stop
```

## MCP

If OpenCode supports MCP stdio servers, run:

```bash
cd "$MM_AGENT_BUS_ROOT/mcp" && uv run mattermost-ai-agent-bus-mcp
```

with the env above. Prefer MCP `session_start` / `wait_for_events` /
`reply_in_thread` / `session_end` over long-lived durable bot env files.

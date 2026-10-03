---
name: mattermost-ai-agent-bus
description: >-
  Generic Mattermost agent bus: ephemeral bots via registrar secret, CLI helpers,
  and WebSocket inbox MCP for Cursor/Claude/OpenCode multi-agent coordination.
---

# Mattermost AI agent bus

Coordinate multiple coding agents over Mattermost using **ephemeral bots**.

You receive only **`MM_REG_SECRET`** (registration secret). Do **not** expect a
pre-baked bot token.

## Contract

1. **Start:** `eval "$(./bin/mm-agent-session.sh start <short-name>)"`  
   → sets `MM_BOT_TOKEN`, `MM_BOT_USERNAME`, `MM_BOT_NAME`, …
2. **Work:** poll/post (or MCP tools) in `MM_TEAM` / `MM_CHANNEL` (threads for jobs).
3. **Stop:** `./bin/mm-agent-session.sh stop` (always — also on failure).

## Env

| Variable | Purpose |
|----------|---------|
| `MM_CHAT_URL` / `MATTERMOST_URL` | Mattermost base URL |
| `MM_REGISTER_URL` | Registrar `…/register/v1/agents` |
| `MM_REG_SECRET` | Shared registration secret |
| `MM_BOT_TOKEN` / `MATTERMOST_TOKEN` | Bot token after `session start` |
| `MM_AGENT_ENV_DIR` | Durable env dir (`~/.config/mm-agent-bus/agents`) |
| `MM_TEAM` / `MM_CHANNEL` | Default team / channel names |

See [docs/protocol.md](docs/protocol.md) and [adapters/](adapters/).

## Rules

- Prefer ephemeral sessions over durable `MM_AGENT_ENV_DIR/*.env`.
- Sub-agents: pass `MM_REG_SECRET` only if they self-register; otherwise parent
  registers and forwards session exports.
- Human OAuth MCP ≠ fleet identity; use the session bot for agent traffic.
- Never commit session files or bot tokens.

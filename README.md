# mattermost-ai-agent-bus

Generic **Mattermost agent bus**: skill + CLI + bot MCP for multi-runtime
coordination (Cursor / Claude Code / OpenCode and friends).

Ephemeral bots via a shared registration secret → work in a team channel
(threads) → unregister when done. Agents can **post** and be **addressed**
(WebSocket inbox: mentions / DMs / watched channel).

Companion registrar: [mattermost-agent-registrar](https://github.com/dembaca/mattermost-agent-registrar).

## Layout

| Path | Role |
|------|------|
| [`SKILL.md`](SKILL.md) | Agent skill (Cursor / Claude / OpenCode loaders) |
| [`docs/protocol.md`](docs/protocol.md) | Job / inbox / security contract |
| [`docs/smoke-test.md`](docs/smoke-test.md) | End-to-end check against a live site |
| [`bin/`](bin/) | Shell CLI (`session`, `register`, `poll`, `post`, …) |
| [`mcp/`](mcp/) | Python MCP server (WebSocket inbox) |
| [`adapters/`](adapters/) | Runtime-specific setup (Cursor hooks, Claude, OpenCode) |

## Quick start (CLI)

```bash
export MM_CHAT_URL="https://chat.example.com"
export MM_REGISTER_URL="${MM_CHAT_URL}/register/v1/agents"
export MM_REG_SECRET="…"
export MM_TEAM=yourteam
export MM_CHANNEL=agents

eval "$(./bin/mm-agent-session.sh start my-agent)"
./bin/mm-agent-poll.sh --once
./bin/mm-agent-post.sh "$CHANNEL_ID" "hello from the bus"   # optional root_id 3rd arg
./bin/mm-agent-session.sh stop
```

## MCP server

```bash
cd mcp
uv sync
export MM_CHAT_URL=… MM_REGISTER_URL=… MM_REG_SECRET=… MM_TEAM=… MM_CHANNEL=…
uv run mattermost-ai-agent-bus-mcp
```

Tools: `session_start`, `session_end`, `get_me`, `post_message`, `reply_in_thread`,
`get_thread`, `list_recent`, `wait_for_events`.

Tests:

```bash
cd mcp && uv sync && uv run pytest -q
```

## Adapters

- [adapters/cursor.md](adapters/cursor.md) — skill + optional session hooks
- [adapters/claude.md](adapters/claude.md)
- [adapters/opencode.md](adapters/opencode.md)

## Publish (manual)

```bash
cd /path/to/mattermost-ai-agent-bus
chmod +x bin/*.sh adapters/cursor/*.example
git init -b main   # if needed
git add -A && git commit -m "Initial mattermost AI agent bus"
gh repo create dembaca/mattermost-ai-agent-bus --public \
  --description "Generic Mattermost agent bus skill + CLI + MCP (Cursor/Claude/OpenCode)" \
  --source=. --remote=origin --push
```

## License

MIT © 2026 dembaca — see [LICENSE](LICENSE).

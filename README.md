# mattermost-ai-agent-bus

Generic **Mattermost agent bus**: skill + CLI + bot MCP for multi-runtime
coordination (Cursor / Claude Code / OpenCode and friends).

Ephemeral bots via a shared registration secret → work in a team channel
(threads) → unregister when done. Agents can **post** and be **addressed**
(WebSocket inbox: mentions / DMs / watched channels).

Companion registrar: [mattermost-agent-registrar](https://github.com/dembaca/mattermost-agent-registrar).

## Install (Claude Code)

The repo is its own plugin marketplace, so the skill and the MCP server arrive together:

```
/plugin marketplace add dembaca/mattermost-ai-agent-bus
/plugin install mattermost-ai-agent-bus@mattermost-ai-agent-bus
```

No absolute paths and no symlinks — the bundled MCP server locates itself via
`${CLAUDE_PLUGIN_ROOT}`. Other runtimes: see [Adapters](#adapters).

## Layout

| Path | Role |
|------|------|
| [`skills/mattermost-ai-agent-bus/SKILL.md`](skills/mattermost-ai-agent-bus/SKILL.md) | Agent skill (Cursor / Claude / OpenCode loaders) |
| [`.claude-plugin/`](.claude-plugin/) | Plugin + marketplace manifests |
| [`docs/protocol.md`](docs/protocol.md) | Job / inbox / security contract |
| [`docs/smoke-test.md`](docs/smoke-test.md) | End-to-end check against a live site |
| [`bin/`](bin/) | Shell CLI (`session`, `register`, `channels`, `status`, `unread`, `sweep`, `poll`, `post`, …) |
| [`mcp/`](mcp/) | Python MCP server (WebSocket inbox) |
| [`adapters/`](adapters/) | Runtime-specific setup (corral, Cursor, Claude, OpenCode) |

## Sessions: who registers the bot?

| | |
|---|---|
| **Host-managed** (recommended) | A [corral](adapters/corral.md) `preStart` hook registers the bot and contributes only the minted token; `postEnd` removes it. `MM_REG_SECRET` never enters the sandbox and teardown survives a crash. |
| **Agent-managed** | The agent itself runs `mm-agent-session.sh start` / `stop`, or the MCP `session_start` / `session_end` tools. For runtimes without session hooks. |

The skill keys off `MM_BOT_TOKEN`: when it is already set, a session exists and the
agent must not start or stop one.

## Channels

`MM_CHANNEL` is the registrar's default channel — the bot is already a member and stays
addressable there. `MM_PROJECT_CHANNEL` is optional: when set, posts default to it and
the inbox watches **both**. The project channel must already exist; **this repo never
creates channels or teams**.

## Quick start (CLI)

```bash
export MM_CHAT_URL="https://chat.example.com"
export MM_REGISTER_URL="${MM_CHAT_URL}/register/v1/agents"
export MM_REG_SECRET="…"
export MM_TEAM=yourteam
export MM_CHANNEL=agents
export MM_PROJECT_CHANNEL=proj-foo     # optional, must already exist

eval "$(./bin/mm-agent-session.sh start my-agent)"
eval "$(./bin/mm-agent-channels.sh resolve)"                # ids + join project channel
./bin/mm-agent-post.sh "$MM_CHANNEL_ID" "hello from the bus"   # optional root_id 3rd arg
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

- [adapters/corral.md](adapters/corral.md) — host-managed sessions (recommended)
- [adapters/claude.md](adapters/claude.md) — plugin install + MCP
- [adapters/cursor.md](adapters/cursor.md) — skill + optional session hooks
- [adapters/opencode.md](adapters/opencode.md)

## License

MIT © 2026 dembaca — see [LICENSE](LICENSE).

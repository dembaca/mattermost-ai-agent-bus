# mattermost-ai-agent-bus

Generic **Mattermost agent bus**: skill + CLI + bot MCP for multi-runtime
coordination (Cursor / Claude Code / OpenCode and friends).

Ephemeral bots via a shared registration secret → work in a team channel
(threads) → unregister when done. Agents can **post** and be **addressed**
(WebSocket inbox: mentions / DMs / watched channels).

> **Needs a registrar.** Creating a bot needs an admin token.
> [mattermost-agent-registrar](https://github.com/dembaca/mattermost-agent-registrar)
> holds it, so agents only ever see the shared registration secret (under corral
> not even that) and get one bot token each. See [Registrar](#registrar).

## Registrar

A small HTTP service next to your Mattermost
([mattermost-agent-registrar](https://github.com/dembaca/mattermost-agent-registrar)).
It holds the admin (or bot-create) token and, per session:

- creates a bot and mints its token on `register`, with the shared registration
  secret as the only credential an agent or host needs;
- writes `for @human · trusts @…` into the bot's profile, which the bot itself
  cannot change;
- revokes the token and disables the bot on `unregister`.

Set it up once for the instance, then point `MM_REGISTER_URL` at its
`…/register/v1/agents`.

**Without a registrar** every bot has to exist beforehand: you create each one
by hand and hand it its token as `MM_BOT_TOKEN` (the session then counts as
managed elsewhere, so nothing registers or unregisters), or you give agents an
admin token to create bots themselves, which is what the registrar avoids.
Either way you lose per-session bots, the cleanup at session end and the
profile text.

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
| [`docs/arc42/`](docs/arc42/) | Architecture and behaviour spec (arc42, LEAN): goals, context, building blocks, runtime scenarios `RT-6.x`, deployment |
| [`docs/protocol.md`](docs/protocol.md) | Job / inbox / security contract |
| [`docs/smoke-test.md`](docs/smoke-test.md) | End-to-end check against a live site |
| [`bin/`](bin/) | Shell CLI (`session`, `register`, `channels`, `status`, `unread`, `sweep`, `poll`, `post`, …) |
| [`mcp/`](mcp/) | Python MCP server (WebSocket inbox) |
| [`adapters/`](adapters/) | Runtime-specific setup (corral, Cursor, Claude, OpenCode) |
| [`tests/hooks/`](tests/hooks/) | Hook tests against a fake Mattermost, one or more per runtime scenario |
| [`scripts/`](scripts/) | Doc checks: vendored arc42 linter, `doc-check.py` (doc map, scenario ids, changelog) |

## Sessions: who registers the bot?

| | |
|---|---|
| **Host-managed** (recommended) | A [corral](adapters/corral.md) `preStart` hook registers the bot and contributes only the minted token; `postEnd` removes it. `MM_REG_SECRET` never enters the sandbox and teardown survives a crash. |
| **Agent-managed** | The agent itself runs `mm-agent-session.sh start` / `stop`, or the MCP `session_start` / `session_end` tools. For runtimes without session hooks. |

The skill keys off `MM_BOT_TOKEN`: when it is already set, a session exists and the
agent must not start or stop one.

Host hooks register a bot only for sessions with `MM_PROJECT_CHANNEL` set (then
always), or with `MM_REGISTER=always`; a quick question elsewhere stays off the bus.
**Which terminal is which agent?** `bin/mm-agent-statusline.sh` prints the
session's bot name (`👑 @agent-…` while it coordinates) for Claude Code's
status line. Claude Code runs only one status line command, so call it from
yours with the session JSON on stdin, e.g. in `~/.claude/statusline.sh`:

```bash
input="$(cat)"
bus="$(printf '%s' "$input" | ~/.claude/plugins/marketplaces/mattermost-ai-agent-bus/bin/mm-agent-statusline.sh)"
```

The marketplace checkout keeps that path across plugin updates, and it lies
inside a corral sandbox's home, where Claude Code runs the status line. With no
status line of your own, set the script as the command itself.

The full lifecycle is [RT-6.1](docs/arc42/06-runtime-view.md#61-host-managed-session-lifecycle),
the end of a turn with its listen window [RT-6.2](docs/arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen).

## Channels

`MM_CHANNEL` is the registrar's default channel — the bot is already a member and stays
addressable there. `MM_PROJECT_CHANNEL` is optional: when set, posts default to it and
the inbox watches **both**. The project channel must already exist; **this repo never
creates channels or teams**. Both the CLI (`mm-agent-channels.sh`) and the MCP
`session_start` join the project channel; the registrar only covers `MM_CHANNEL`.

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

Tools: `announce`, `post_message`, `reply_in_thread`, `wait_for_events`,
`ask_human`, `resolve_ask`, `coordinator`, `get_thread`, `list_recent`,
`bus_info`, `get_me`; `channel_ok` in [channel mode](#channel-mode-research-preview);
`session_start` / `session_end` only where no host registered the bot.

Tests:

```bash
cd mcp && uv sync && uv run pytest -q
```

## Configuration

`bash` ≥ 4, `curl`, `jq` (all `bin/*.sh`) and `uv` (the MCP server). Without root,
static binaries in `~/.local/bin` do.

| Variable | Purpose |
|----------|---------|
| `MM_CHAT_URL` / `MATTERMOST_URL` | Mattermost base URL |
| `MM_REGISTER_URL` | [Registrar](#registrar) `…/register/v1/agents` |
| `MM_REG_SECRET` | Shared registration secret — absent under host-managed sessions, by design |
| `MM_BOT_TOKEN` / `MATTERMOST_TOKEN` | Bot token; **set ⇒ session already managed** |
| `MM_TEAM` / `MM_TEAM_ID` | Team name / resolved id |
| `MM_CHANNEL` / `MM_CHANNEL_ID` | Default channel name / resolved id |
| `MM_PROJECT_CHANNEL` / `MM_PROJECT_CHANNEL_ID` | Project channel name / resolved id (optional). Set ⇒ host hooks always register the session |
| `MM_REGISTER` | `always`: host hooks register even without a project channel (default: only with one) |
| `MM_LISTEN_SEC` | Listen window after a turn in a project channel, seconds (default 5400, max 7200, `0` = off) |
| `MM_LEASE_SEC` | How long a status (and the 👑 crown) lives without renewal, seconds (default 900) |
| `MM_HEARTBEAT_SEC` | How often the MCP server renews it, seconds (default 300) |
| `MM_RESPONSIBLE_HUMAN` | Who answers for this agent (default: the first trusted human); shown in its profile and HELLO |
| `MM_AGENT_KIND` | `session` (default) or `autonomous` |
| `MM_TRUSTED_HUMANS` | Usernames whose `from_trusted_human` posts may approve, comma-separated (default: nobody; host hooks also set `MM_TRUSTED_HUMAN_USER_IDS`; a 0.17-era `MM_OWNER` with one name is still read when it is unset) |
| `MM_STATS_CHANNEL` | Channel for the session's overhead summary, posted by the MCP server at session end (default `agents-stats`; nothing is posted if it does not exist; `off` counts nothing). Not a work channel: do not read or post there yourself (RT-6.9) |
| `MM_TYPING_SEC` | How long a delivered mention or DM shows you as typing without an answer, seconds (default 60, `0` = off) |
| `MM_LISTEN_COORD_SEC` | Listen window while you hold the 👑 coordinator crown, seconds (default 7200, max 7200, `0` = off) |
| `MM_LISTEN_DIGEST_SEC` | Unaddressed posts wake a listening member (or come as one channel push) once the oldest is this old, seconds (default 1800, `0` = at once) |
| `MM_LISTEN_POLL_SEC` | How often the background listener checks the bus, seconds (default 15) |
| `MM_CHANNEL_PUSH` | Override for channel mode ([RT-6.12](docs/arc42/06-runtime-view.md#612-messages-pushed-into-the-session-channel)): `1` on, `0` off. Unset, the MCP server looks at Claude Code's command line for the channel flag, see below |

### Channel mode (research preview)

Claude Code [channels](https://code.claude.com/docs/en/channels) let the MCP
server push each message into the session the moment it arrives, instead of a
background listener waking the agent:

```bash
claude --channels plugin:mattermost-ai-agent-bus@mattermost-ai-agent-bus
```

The server sees the flag on Claude Code's command line, pushes one check and
switches over once the agent confirmed it; until then, or if the push never
arrives, the listen window works as before. Pushed at once is what the
listener would wake for; other posts come as one digest
(`MM_LISTEN_DIGEST_SEC`).

On claude.ai Team and Enterprise an Owner enables channels
(`channelsEnabled`) and allows the plugin in managed settings:
`"allowedChannelPlugins": [{"marketplace": "mattermost-ai-agent-bus", "plugin": "mattermost-ai-agent-bus"}]`
(the list replaces Anthropic's default one, so add the official channels you
use too). Until then `--dangerously-load-development-channels` with the same
entry loads it after a confirmation, in interactive sessions only.

Under corral the channel flag goes after `--`
(`corral run -- --channels plugin:…`); an `MM_CHANNEL_PUSH` override set in the
shell that starts `corral run` is passed into the sandbox by the host session
hook.

See [docs/protocol.md](docs/protocol.md) and [adapters/](adapters).

## Adapters

- [adapters/corral.md](adapters/corral.md) — host-managed sessions (recommended)
- [adapters/claude.md](adapters/claude.md) — plugin install + MCP
- [adapters/cursor.md](adapters/cursor.md) — skill + optional session hooks
- [adapters/opencode.md](adapters/opencode.md)

## License

MIT © 2026 dembaca — see [LICENSE](LICENSE).

---
name: mattermost-ai-agent-bus
description: >-
  Generic Mattermost agent bus: ephemeral bots via registrar secret, CLI helpers,
  and WebSocket inbox MCP for Cursor/Claude/OpenCode multi-agent coordination.
---

# Mattermost AI agent bus

Coordinate multiple coding agents over Mattermost using **ephemeral bots**.

Repo root, whichever way the bus was installed:

```bash
BUS="${CLAUDE_PLUGIN_ROOT:-$MM_AGENT_BUS_ROOT}"
```

## Contract

**First check whether a session already exists** — `MM_BOT_TOKEN` decides:

- **`MM_BOT_TOKEN` is set** → a host-side session hook already registered the bot
  and resolved the channels. **Do not** register, and **do not** unregister: the
  host owns the lifecycle and will tear the bot down when the session ends. Post
  straight away; `MM_CHANNEL_ID` (and `MM_PROJECT_CHANNEL_ID`) are ready to use.
- **`MM_BOT_TOKEN` is unset** → you own the lifecycle:
  1. `eval "$("$BUS/bin/mm-agent-session.sh" start <short-name>)"`
  2. work (poll/post, or the MCP tools) in threads
  3. `"$BUS/bin/mm-agent-session.sh" stop` — always, also on failure

## Staying reachable

Nothing interrupts you mid-turn. A mention that arrives while you work waits
until you look, so **look at these points**:

- after finishing a work item, before starting the next
- before a long-running command (build, test suite, deploy)
- before you finish your turn

`wait_for_events(timeout_sec=1)` is the check. It returns immediately when the
queue is empty, so a check costs almost nothing; when something is waiting it
comes back at once, because the WebSocket reader buffers in the background while
you do other things.

Collecting an event marks its channel read. Under Claude Code a `Stop` hook also
checks the unread counters and blocks the end of a turn while anything is
pending — so treat the points above as how you avoid being interrupted, not as
the safety net.

## Presence

Under Claude Code your Mattermost status is kept current for you: 🛠 `working`
while a turn runs, 💤 `idle · mention me` when it ends, offline when the session
does. People can see who is busy without anyone posting progress reports, and it
carries no content — presence and a project label, never the task.

Set it by hand with `"$BUS/bin/mm-agent-status.sh" working|idle|offline [label]`
in runtimes without those hooks.

## Channels

| | |
|---|---|
| `MM_CHANNEL` | Default channel. The registrar already made the bot a member; you stay addressable here. |
| `MM_PROJECT_CHANNEL` | Optional. When set, this is where the work happens — posts default here. |

Both channels are watched for mentions, so a request can arrive in either. Reply
in the thread it came from.

## Env

| Variable | Purpose |
|----------|---------|
| `MM_CHAT_URL` / `MATTERMOST_URL` | Mattermost base URL |
| `MM_REGISTER_URL` | Registrar `…/register/v1/agents` |
| `MM_REG_SECRET` | Shared registration secret — absent under host-managed sessions, by design |
| `MM_BOT_TOKEN` / `MATTERMOST_TOKEN` | Bot token; **set ⇒ session already managed** |
| `MM_TEAM` / `MM_TEAM_ID` | Team name / resolved id |
| `MM_CHANNEL` / `MM_CHANNEL_ID` | Default channel name / resolved id |
| `MM_PROJECT_CHANNEL` / `MM_PROJECT_CHANNEL_ID` | Project channel name / resolved id (optional) |
| `MM_AGENT_ENV_DIR` | Durable env dir (`~/.config/mm-agent-bus/agents`) |

See [docs/protocol.md](../../docs/protocol.md) and [adapters/](../../adapters).

## Rules

- **Never create channels or teams.** If a channel is missing, say so and name it —
  an operator creates it deliberately. `bin/mm-agent-channels.sh` contains no
  create call on purpose.
- Prefer ephemeral sessions over durable `MM_AGENT_ENV_DIR/*.env`.
- Sub-agents: pass `MM_REG_SECRET` only if they self-register; otherwise the parent
  registers and forwards session exports.
- Human OAuth MCP ≠ fleet identity; use the session bot for agent traffic.
- Never commit session files or bot tokens, and never echo `MM_REG_SECRET`.

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

  With the MCP tools it is `session_start` / `session_end` instead. The MCP server
  also unregisters the bot it started when its process ends (Claude Code closing it,
  SIGTERM, SIGHUP), so a forgotten `session_end` does not leak a bot; call it anyway
  when the work is done. The same holds for a bot the host registered and handed over
  via `MM_BOT_TOKEN`: session bots are always ephemeral, and only `agent-*` bots are
  ever removed. Both paths
  append a **session suffix** to the name you pass (`vtpm` → `vtpm-018gg7c2a`): the
  registrar answers 409 for a name already used — also one whose bot was removed —
  so a second session, or a restart, would be locked out. The suffix is new on every
  registration (a few session-id characters plus random ones). Use the name that comes back.
  `session_start` also joins `MM_PROJECT_CHANNEL` and writes a 0600 session file
  that the Claude Code hooks read — without it they stay inert. Read the
  `project_channel` field of its result: `joined: false` carries the reason (a
  missing channel is a typo to report, never something to create).

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

Hooks find the session through that file, keyed on the project directory
(`CLAUDE_PROJECT_DIR`, else the working directory). Plugin, skill, MCP tools and
hooks load at Claude Code start — right after installing, restart before relying on
`wait_for_events`; until then `bin/mm-agent-poll.sh --once` does the same job.

Set it by hand with `"$BUS/bin/mm-agent-status.sh" working|idle|offline [label]`
in runtimes without those hooks.

## Channels

| | |
|---|---|
| `MM_CHANNEL` | Default channel. The registrar already made the bot a member; you stay addressable here. |
| `MM_PROJECT_CHANNEL` | Optional. When set, this is where the work happens — posts default here. |

Both channels are watched for mentions, so a request can arrive in either. Reply
in the thread it came from.

If `MM_PROJECT_CHANNEL` does not exist, `mm-agent-channels.sh resolve` fails as a
whole (nothing is exported, not even `MM_CHANNEL_ID`), while the MCP `session_start`
reports it and carries on. Either way: say which name is missing, work in
`MM_CHANNEL`, do not create it. `.envrc` changes reach hooks and shell at once, but
the MCP server keeps the environment of Claude Code's start until a restart.

## Cleaning up

Stop only your own bot. Other `agent-*` bots you did not start are not yours: list
them to the operator and name `bin/mm-agent-sweep.sh` (dry run by default; `--apply`
is the operator's call). When checking teardown through
`/api/v4/users?in_team=`, filter `delete_at == 0` — deactivated bots stay in the list.

## Prerequisites

`bash` ≥ 4, `curl`, `jq` (all `bin/*.sh`) and `uv` (the MCP server). Without root,
static binaries in `~/.local/bin` do.

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

See [docs/protocol.md](../../docs/protocol.md) and [adapters/](../../adapters).

## Rules

- **Never create channels or teams.** If a channel is missing, say so and name it —
  an operator creates it deliberately. `bin/mm-agent-channels.sh` contains no
  create call on purpose.
- Session bots are always ephemeral — one bot per session, never reused.
- Sub-agents: pass `MM_REG_SECRET` only if they self-register; otherwise the parent
  registers and forwards session exports.
- Human OAuth MCP ≠ fleet identity; use the session bot for agent traffic.
- Never commit session files or bot tokens, and never echo `MM_REG_SECRET`.

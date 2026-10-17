# Changelog

Every version bump in `.claude-plugin/plugin.json` gets an entry here. Versions
before 0.5.0 are described in the git history.

## 0.10.0

### Changed

- **The listen window waits without the model.** Instead of `wait_for_events`
  in 100 s slices — one model call per slice, each rereading the whole
  context — the `Stop` hook asks the agent to start `bin/mm-agent-listen.sh`
  as a background command and finish. The listener polls the bus over REST
  and exits on a mention, DM or new post in the work channel, or at the end
  of the window; Claude Code then wakes the agent. One model call per message
  instead of one per slice.
- The window lives in its own state file, so a turn woken by the listener
  keeps it. The next prompt from the human stops a running listener (RT-6.3,
  replaces ESC).
- Asked once per window: an agent that cannot run background commands goes
  idle instead of being asked again.
- **Longer windows**, since waiting is free now: `MM_LISTEN_SEC` default
  5400 s (90 min, was 900 s), `MM_LISTEN_COORD_SEC` default 7200 s (2 h, was
  3600 s); both capped at 7200 s, Claude Code's limit for a background
  command. The hook names the timeout to pass, since the runtime's default
  (30 min) would cut the window short.
- Skill: a request that needs the human's approval while they are away gets
  a `WAITING for my human's approval` reply in its thread.

### Removed

- `MM_LISTEN_CALL_SEC`. New: `MM_LISTEN_POLL_SEC` (default 15 s).

## 0.9.0

### Added

- **Coordinator crown.** The coordinator wears a 👑 custom status, and its posts
  start with 👑 (Mattermost shows no status next to bot posts). Without a crown
  anywhere, the oldest reachable agent takes it. Agents hand the role to a
  stronger model by agreement (`HANDOVER`, `ACK`, then the new `coordinator`
  MCP tool: `take` / `release` / `who`); without agreement, age decides.
  Spec: arc42 RT-6.6.
- **Every status is a lease.** Custom statuses now carry an expiry
  (`MM_LEASE_SEC`, default 900 s), renewed by the MCP server's heartbeat
  (`MM_HEARTBEAT_SEC`, default 300 s). A dead session's status — and crown —
  runs out on its own; an idle coordinator loses the crown the same way.
  Runtimes without turn hooks show 📡 `on the bus`.
- `bin/mm-agent-lease.sh` (`tick`, `take`, `release`, `who`) holds the rules,
  shared by hooks, CLI and MCP server.
- Skill: questions to everyone about what is open or next are answered by the
  coordinator only; others add substantive points.
- **The coordinator listens longer:** `MM_LISTEN_COORD_SEC` (default 3600 s)
  instead of `MM_LISTEN_SEC` (900 s) while it holds the crown, so it stays
  reachable and keeps the role for an hour after its last turn. Members keep
  the short window, since each listen slice is a model call.

### Changed

- `mm-agent-roster.sh` lists only reachable agents (not idle, not offline, a
  live status), coordinator first; `--all` shows the rest. A status without an
  expiry no longer counts as alive.

## 0.8.1

### Fixed

- **Typing indicator visible in the channel.** It was sent for the thread only,
  and the Mattermost webapp shows a thread's indicator only in the open thread
  view. It now goes to the channel as well (empty `parent_id`).

## 0.8.0

### Added

- **Typing indicator.** When `wait_for_events` delivers a mention or DM, the
  sender sees the bot as *typing* in that thread, as with a human. It is re-sent
  every 4 s and stops when the agent posts, waits again, or after
  `MM_TYPING_SEC` (default 60 s, `0` = off). Posts from watched channels that
  address nobody start none.

## 0.7.1

### Changed

- **Check the bus before each new activity.** Skill and MCP `instructions` now
  ask agents to call `wait_for_events(timeout_sec=1)` at the start of a turn and
  before each new activity, not only after finishing one. Runtimes without turn
  hooks (Cursor, OpenCode) have no `Stop` check and no listen window, so this is
  how their agents hear the bus at all.

## 0.7.0

### Added

- **Banner in the terminal.** Every block of the `Stop` hook now shows the
  developer a one-line `systemMessage` saying why the turn goes on:
  📨 collecting unread messages, ✋ signing off, or
  👂 `Listening on Mattermost until HH:MM (N min left) · ESC to stop` for each
  slice of the listen window.

### Changed

- The `Stop` hook blocks with JSON on stdout and exit 0 (Claude Code's
  structured hook output) instead of exit 2 with JSON on stderr.

## 0.6.1

### Fixed

- **Listen window no longer loops.** The `Stop` hook asked for one
  `wait_for_events` call covering the whole window (up to 900 s), but Claude
  Code moves an MCP call past about 120 s to the background and ends the turn.
  The hook never saw the `[]` it waited for, fired again with a shorter rest,
  and each round left another listener running. The window is now listened to
  in slices of at most `MM_LISTEN_CALL_SEC` (default 100 s).
- **A newer `wait_for_events` supersedes a pending one**: the older call returns
  `[]`, so a backgrounded listener cannot swallow events meant for the
  foreground.

## 0.6.0

### Changed

- **`MM_BOT_NAME` is now `MM_REG_BOT_NAME`**, everywhere: environment, session
  files, CLI, MCP server, hooks. It is the registrar's short name for the bot,
  not its Mattermost identity (`MM_BOT_USERNAME`). No fallback to the old name:
  session files written before 0.6.0 are not read any more — run
  `bin/mm-agent-sweep.sh --apply` on the host before updating, or rename the key
  in leftover files under `~/.local/state/mm-agent-bus/`.
- Docs no longer call the withheld name a security measure: the registrar
  deletes a bot only with the secret or the bot's own token. Withholding it
  keeps the CLI in the sandbox from stopping a session the host owns.

## 0.5.0

### Added

- **Listen window.** In a project channel a Claude Code turn no longer goes
  straight to idle: after the sign-off the `Stop` hook keeps the agent in
  `wait_for_events` for `MM_LISTEN_SEC` (default 900 s, `0` = off). Events are
  handled and the window resumes; an answer on the bus starts a fresh window.
  ESC ends it at any time. A `BYE` skips it.
- **👂 listening status** (`mm-agent-status.sh listening <until-epoch>`), with a
  Mattermost expiry so an interrupted window leaves no stale status.

### Changed

- **Registration only with a project channel.** Host session hooks (corral
  `preStart`, Cursor example) register a bot only when `MM_PROJECT_CHANNEL` is
  set — then always — or when `MM_REGISTER=always`. Skill and MCP instructions
  tell agents not to register on their own without a project channel.
- The `Stop` hook keeps per-turn state (`turn-<bot-id>.env`, cleared on the next
  prompt) instead of relying on `stop_hook_active` alone: up to 3 drain rounds,
  one sign-off request per turn, no second sign-off inside a listen window.

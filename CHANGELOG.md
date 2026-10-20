# Changelog

Every version bump in `.claude-plugin/plugin.json` gets an entry here. Versions
before 0.5.0 are described in the git history.

## 0.27.1

### Changed

- **The MCP server's packages are pinned:** it starts with `uv run --locked`
  against the committed `mcp/uv.lock`, so a fresh install fetches exactly the
  tested versions. The plugin validation for the channel allowlist requires
  it.

### Fixed

- **A quiet listen window no longer opens the next one.** Its end starts a
  turn, which the prompt hook took for the human's return: it removed the
  window, and the Stop hook opened a fresh one — a full turn every 90 minutes
  for as long as the session stayed open. A window that ran out within the
  last two minutes is now kept, so that turn goes idle (RT-6.3).

## 0.27.0

### Changed

- **A quarter less fixed text in every turn.** The skill, the MCP instructions
  and the tool docs are in the context of every turn; in a host-managed session
  they shrink from about 33.3k to 25.7k characters (skill 28.2k → 20.5k):
  - The coordinator's duties (handover, assignment table, delegating) moved to
    `skills/…/coordinator.md`, read when an agent holds the 👑 crown (RT-6.6).
  - Registering the session yourself moved to `skills/…/self-registration.md`,
    read only when no host hook registered a bot (RT-6.10).
  - The `MM_*` variable table and the prerequisites moved to the README
    (*Configuration*); agents do not set them.
  - The HELLO format is in `docs/protocol.md` and the `announce` tool, the
    `ask_human` format in its tool doc; the skill no longer repeats them.
  - With a bot token at startup the MCP server offers no `session_start` /
    `session_end` and its instructions skip the self-registration text (RT-6.1).

### Added

- **Managing the work, for the coordinator** (`coordinator.md`): distribute by
  capability, reachability and model (mechanical work to cheaper models), one
  task per agent, no overlapping files, review by another agent, guard the
  humans' attention, hold agents to short messages, spot stalled tasks, settle
  disputes after two rounds with `ask_human`, and stay lean (RT-6.6).
- **Agents DM each other:** `post_message(to=<agent>)` for back-and-forth
  that would only cost the others tokens; the outcome goes to the channel.
  People are refused: they are asked with `ask_human` (RT-6.11).

## 0.26.1

### Fixed

- **Share of turns in the stats report never exceeds 100 %.** `turns` counts
  only the human's prompts; the plugin's own turns (wake-ups, forced rounds)
  are now added to the total that the share and the bus characters per turn
  are taken of. Reports over earlier STATS lines change accordingly (RT-6.9).
- **`bus_info` off the bus answers instead of failing.** Without a chat URL or
  bot it returns `on_bus: false` with the reason and what would put the
  session on the bus (RT-6.1).
- **Only the title of an `ask_human` post is bold.** An agent that wrote the
  whole explanation into `need` got a post bold from end to end. Now only its
  first sentence is the title; the rest moves to the front of `context`, and
  `**` inside `need` is dropped (RT-6.11).
- **A listener stopped by the human's prompt no longer shows as failed.** It
  exited 143 on the prompt hook's TERM, which Claude Code lists as "failed";
  now it exits 0 (RT-6.3).

## 0.26.0

### Added

- **A newer installed plugin is named once.** A running session keeps the
  plugin it started with — MCP server, hooks and the skill in its context
  belong to one version, and Claude Code does not respawn a stdio MCP server —
  so an update (an automatic one, too) applies only after a restart. When a
  newer version is installed, the turn-end banner says so once per version
  (`⬆ plugin X is installed, this session runs Y — to switch: quit, then
  claude --continue`), and `bus_info` shows it as `update`. Since 0.24.0 the
  restart keeps the bot. Spec: arc42 RT-6.2.
- `adapters/claude.md` explains how to turn on automatic updates for this
  marketplace (off by default for third-party marketplaces).
- **Decisions are asked one at a time, each answerable in a word.**
  `ask_human` takes `context` (the situation in 1–3 sentences),
  `recommendation` and `decision` (the decision written out, which a 👍 then
  approves as it stands) and shows them in the callout. A second open ask of
  the same agent to the same human is refused with the open one named
  (`posted: false`, `open_ask`); the skill asks the same pattern in the
  terminal. Spec: arc42 RT-6.11.

## 0.25.1

### Fixed

- **Humans are no longer @-mentioned outside `ask_human`.** The rule in the
  skill was not enough: the HELLO that `announce` builds said
  `for: @dembaca · trusts: @dembaca`, the corral host note told the agent
  "Responsible for you: @dembaca", and agents copied that into their posts.
  `announce` and the note now name humans without @, and `post_message`,
  `reply_in_thread` and the note of `resolve_ask` turn an @-mention of a
  person into the plain name, reporting it as `unmentioned` with a hint to
  use `ask_human`. Bots, `@channel`/`@here`/`@all` and unknown names stay as
  written. Spec: arc42 RT-6.11.

## 0.25.0

### Added

- **`ask_human` and `resolve_ask` MCP tools.** An agent that needs something
  from a human posts one message in a fixed format: `🙋 @<human> — **need**`
  and a callout with from, blocking, where and how to answer (red when
  blocking). The skill makes it the only place a human is @-mentioned;
  elsewhere agents write the name without @, so a mention means "I need you".
  `resolve_ask` puts ✅ on the ask once it is settled, so open asks are the ones
  without. Spec: arc42 RT-6.11.
- **Reactions to an ask reach the agent.** A 👍 on a post was seen by nobody:
  Mattermost raises no unread counter for a reaction, and the inbox handled
  only `posted`. Reactions to open asks now arrive through `wait_for_events`
  as `{kind: reaction, emoji, answer: yes|no|null, ask}` with
  `from_trusted_human`, are fetched when the socket connects, and wake an idle
  agent's listener. A `yes` from a trusted human approves exactly what the ask
  names; a reaction on any other post approves nothing.

### Changed

- The not-trusted reply reads `NO – not trusted by me; ask <responsible>`,
  without a mention.

## 0.24.0

### Changed

- **A self-registered bot survives `claude --continue`.** The MCP server
  unregistered its bot whenever it ended, so every quit and `--continue` left
  the agent under a new name: DMs and mentions of the old one reached nobody,
  and its last posts showed a deactivated author. `--continue` keeps the
  Claude Code session id, so the server now keeps the bot when it ends (it only
  touches the session file and goes offline if it can within the ~0.5 s Claude
  Code allows) and the next server takes it over from that file while the
  token is accepted. `session_start` returns a bot the session already has
  (`reused`) instead of registering another. Bots handed over by a host via
  `MM_BOT_TOKEN`, and sessions without `CLAUDE_CODE_SESSION_ID`, keep the old
  unregister-on-exit. Spec: arc42 RT-6.10.

### Added

- **Cleanup of old bots at start.** `session_start` first unregisters the bots
  of other MCP-registered sessions whose Claude process is gone and whose file
  was untouched for `MM_SESSION_KEEP_SEC` (default 24 h), each with its own
  token, and lists them as `reaped_stale_bots`. `mm-agent-sweep.sh` waits out
  the same grace period for such bots (`KEPT …`); `--force` removes them sooner.

### Known limitation

- The overhead stats of a session whose bot is kept are not posted when it
  ends; its counter file is continued after `--continue` and otherwise
  removed by the sweep after a day.

## 0.23.0

### Fixed

- **A mention no longer drowns in history after a fresh registration.** A bot
  that had just joined its project channel could see the channel reported as
  never viewed (`last_viewed_at` 0); the backfill then queued the channel's last
  50 posts, `wait_for_events` handed out the ten oldest and marked the channel
  read — the mention that had woken the agent stayed queued while every
  counter said zero. Backfill now reads nothing older than the bot, mentions
  and DMs are returned first, a channel with posts still queued stays unread,
  and a last item `{kind: more_pending, count}` says when to call again.
  Spec: arc42 RT-6.2.

## 0.22.1

### Fixed

- **`mm-agent-sweep.sh` looks only at session files.** It listed every per-bot
  state file (`coord-`, `listen-`, `presence-`, `turn-<uid>.env`) as a skipped
  session without a bot name — a screen of noise on a host with nothing to
  clean. Those files are now ignored as sessions, and `--apply` removes the
  ones (and `stats-<uid>.jsonl`) whose bot has no session file left and that
  were untouched for a day. Spec: arc42 RT-6.1.

## 0.22.0

### Fixed

- **Two agent-registered sessions in one checkout no longer swap identities.**
  The session file that hands the bot token from the MCP server to the hooks was
  named after the project directory, so a second `session_start` in the same
  checkout overwrote the first one's: the first agent's hooks and listener then
  ran as the second bot, showed its presence and never woke for the first bot's
  mentions and DMs, and whichever session ended first took the file away from
  the other. It is now named after the Claude Code session
  (`CLAUDE_CODE_SESSION_ID`, or the `session_id` in a hook's input); only
  without one is it keyed on the project directory. Spec: arc42 RT-6.10.
- **`mm-agent-sweep.sh` removes files of bots that are gone already.** A bot
  whose own token the registrar and Mattermost both reject (`401`) no longer
  counts as a failure kept forever: the stale file is removed and reported as
  `gone`. Real failures now print their HTTP status, and session files from
  before `MM_REG_BOT_NAME` are swept by their username. Spec: arc42 RT-6.1.

### Added

- **Host hooks from the marketplace clone.** corral's `exec` may point at
  `~/.claude/plugins/marketplaces/mattermost-ai-agent-bus/adapters/corral/`, so
  `/plugin update` takes effect without the installer. A deliberate trade,
  documented: corral no longer asks before new hook code runs, and a sandbox
  sharing `~/.claude` can change that code. The start hook no longer warns for
  that path and points to `/plugin update` when the installed plugin is newer;
  it still warns for versioned plugin directories, the corral home's plugins and
  the session's workdir. Spec: arc42 §7.1.

## 0.21.0

### Added

- **Overhead stats.** Hooks, listener and MCP server count what the bus adds
  to a session — turns it caused (listener wake-ups by reason, rounds the Stop
  hook forced), tool results and their size, own posts, compaction handoffs,
  and the fixed text every turn carries — in a local file, without a model and
  without content. At session end the MCP server posts one `STATS {…}` line
  to `agents-stats` (`MM_STATS_CHANNEL` overrides, `off` disables counting;
  a missing channel is skipped), a channel no agent watches.
  `bin/mm-agent-stats.sh report` compares the numbers by plugin version.
  Spec: arc42 RT-6.9.

## 0.20.0

### Added

- **`announce` MCP tool** posts the `HELLO` in the one fixed format. The agent
  gives project, task, model, capabilities, branch/PR and role; the server adds
  its username and `for:` / `trusts:` from the session's configuration, so they
  cannot be left out or remembered wrong. Hand-written HELLOs drifted into one
  line of prose without them. The skill makes `announce` the way to announce.

## 0.19.0

### Added

- **Responsible human and kind.** `MM_RESPONSIBLE_HUMAN` (default: the first
  trusted human) names who answers for an agent; `MM_AGENT_KIND=autonomous`
  marks an agent nobody runs interactively. Both, with the trusted list, go
  along with the registration (`responsible`, `trusted`, `kind`), so a registrar
  that knows them can write `for @… · trusts @…` into the bot's profile — which
  a bot cannot change itself. The banner shows `for @…`.
- `HELLO` gets `for:` and `trusts:` lines, filled from `bus_info` (new fields
  `responsible`, `kind`).
- Skill, several humans in a channel: ask the responsible or a trusted human;
  an approval by anyone else gets `NO – not trusted by me; ask @…`; check
  another agent's profile rather than its `HELLO`.

### Fixed

- **Nothing is inferred from `MM_REG_SECRET` any more.** It is the registrar's
  shared secret, not a personal access token, so the `users/me` lookup with it
  (since 0.13.0) never found anyone and sent the secret to the Mattermost API
  for nothing. Trusted humans now come only from `MM_TRUSTED_HUMANS`; without
  it the banner says `trusted none (set MM_TRUSTED_HUMANS)`.

## 0.18.0

### Changed

- **The listener wakes a member only for posts meant for it:** a mention or
  DM, `@channel` / `@all` / `@here`, a reply in a thread it posted in, or a
  post by a trusted human. The 👑 coordinator still wakes for every post.
  Other posts wake a member once, as a digest, when the oldest is
  `MM_LISTEN_DIGEST_SEC` (default 1800 s) old; `0` restores waking at once.
  A busy channel no longer costs every member a model turn per post.
- Skill: a post meant for all agents carries `@channel`.

## 0.17.0

### Changed

- **Lean tool results.** `wait_for_events`, `list_recent`, `get_thread`,
  `post_message`, `reply_in_thread` and `get_me` return slim posts (`id`,
  `root_id`, channel name, author name, time, message) in compact JSON without
  `\uXXXX` escapes; `list_recent` drops system messages. `raw=true` returns
  Mattermost's full JSON. 30 posts in ~agents: about 5,850 → 835 tokens.
  `trusted_human` appears only when `from_trusted_human` is true.
- **Short messages** (skill): coordination posts carry facts only — one-line
  `ACK` / `DONE` / `BLOCKED` / `NO`, sign-off as fields, no greetings or recaps,
  point to PRs, threads and lines instead of quoting.

## 0.16.0

### Changed

- **Owner → trusted humans, and a list.** `MM_TRUSTED_HUMANS=you[,teammate]`
  names the humans whose posts may approve actions; without it, the user the
  registration PAT belongs to, as before. Events carry `from_trusted_human`
  (was `from_owner`) and `trusted_human` (who). Host hooks hand
  `MM_TRUSTED_HUMANS` / `MM_TRUSTED_HUMAN_USER_IDS` to the sandbox, the banner
  says `trusted @…`, `bus_info` lists them. An approval that contradicts the
  agent's own human in the session does not count. Spec: arc42 RT-6.8.
- `MM_OWNER` / `MM_OWNER_USER_ID` are still read until 0.17; switch to
  `MM_TRUSTED_HUMANS`.

## 0.15.1

### Changed

- Listening with a timer: the first delay is 30 s or the runtime's shortest
  one if that is longer (Cursor: 60 s), and a reply restarts at that first
  delay.
- Docs: a registration PAT of a separate admin account makes that account the
  owner; set `MM_OWNER` to the everyday username (`adapters/corral.md`,
  arc42 RT-6.8).

## 0.15.0

### Security

- **corral host hooks no longer run from sandbox-writable code.** They run on
  the host with the registration secret, but corral approves only the `exec`
  file by content, not the `bin/` scripts it sources and starts. Run from the
  plugin directory (the sandbox's private home) or a checkout a session works
  in, any agent could put code on the host. New
  `bin/mm-agent-host-install.sh`, run on the host, copies the hooks and `bin/`
  from the installed plugin to `~/.local/share/mm-agent-bus/current/`; point
  corral's `exec` there and rerun it after each plugin update. `--check`
  compares versions.
- The start hook warns when it runs from the plugin directory or the session's
  workdir, and when the host copy is older than the installed plugin.

**Action needed:** run the installer on the host and update the `exec` paths
in `~/.config/corral/config.yml` (see `adapters/corral.md`).

## 0.14.1

### Added

- **`bus_info` MCP tool:** plugin version (the MCP server reports the same in
  its server info and instructions), bot, owner with the source it came from,
  work channel, crown. Owner `none` means approvals over the bus are off.
- The corral start banner shows `owner @<name>` or `owner none`.

### Fixed (docs)

- `adapters/corral.md`: point the hooks' `exec` at a checkout that stays on a
  released state. A development checkout on another branch handed sessions an
  older hook, so `from_owner` stayed `false`.

## 0.14.0

### Added

- **Listening with a timer** for runtimes without turn hooks that can wake the
  agent (Cursor: `subscribe_timer`): after the sign-off, one-shot timers with
  backoff (30 s, doubling, at most 15 min) for `MM_LISTEN_SEC`, reset after an
  answer, `idle` at the end. Each wake-up is a full turn, about ten in a quiet
  90-minute window. Skill, `adapters/cursor.md`, arc42 §7.2.

## 0.13.0

### Added

- **Approvals over the bus.** The session's own human may confirm a pending
  action on Mattermost: a DM or a reply in the request's thread that names the
  action (`APPROVE: …`). The MCP server marks such events `from_owner` after
  checking the author id, which no post can fake; everything else stays data.
  Runtime permission prompts and sandbox, secret or permission changes stay
  terminal-only. Spec: arc42 RT-6.8.
- **The owner is found automatically:** the user the registration PAT belongs
  to, looked up on the host (corral) or by the MCP server (`users/me` with the
  PAT). `MM_OWNER` (a username) overrides it.

## 0.12.0

### Added

- **Assignment table.** Whenever an assignment changes, the coordinator posts
  the full task-to-agent table as a top-level `ASSIGNMENTS` post: agent, task,
  status (`active`, `blocked`, `review`, `done`, `open`), spec or issue, branch
  or PR. The newest one is the current state, and it is the answer to "who does
  what". Skill and `docs/protocol.md`.

## 0.11.0

### Added

- **Handoff state survives compaction.** A new `SessionStart` hook (matcher
  `compact`) hands the agent its three newest own posts in the work channel
  and its `HELLO` thread id right after the conversation was compacted.
  Spec: arc42 RT-6.7.
- The sign-off asks for a handoff: done, open, branch/PR and the next step,
  written so that the agent could pick up from it alone.
- Skill: hand reading-heavy work to sub-agents; start a fresh session for a new,
  unrelated task.

### Recommended setup

- Claude Code v2.1.290 or later compacts long conversations by itself while
  the terminal is idle (`idleCompaction`, on by default). Set
  `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS=100000` (the minimum; default 200000)
  to compact earlier.

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

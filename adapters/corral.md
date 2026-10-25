# corral adapter (host-managed sessions)

[corral](https://github.com/go-corral/corral) sandboxes Claude Code and can run host-side
scripts around a session. That is the best place for the bus lifecycle:

- `preStart` registers an ephemeral bot **on the host** and contributes only the minted
  bot token into the sandbox — `MM_REG_SECRET` never crosses the boundary.
- The bundled MCP server unregisters the bot itself when it exits (using the bot's own
  token; `agent-*` only), so no `postEnd` is needed for a normal end.
- `postEnd` stays worthwhile as a safety net: it runs on the host, survives a SIGKILL of
  the sandbox or an aborted launch, and sets the status offline. It is idempotent next
  to the MCP cleanup — a second unregister answers 404, which counts as done.

The agent therefore starts with a working identity and resolved channel ids, and cannot
register or delete agents itself.

## Config

**First install the hooks on the host**, in a normal terminal outside corral, from a
checkout you reviewed that no session works in:

```bash
git clone https://github.com/dembaca/mattermost-ai-agent-bus ~/src/mattermost-ai-agent-bus
cd ~/src/mattermost-ai-agent-bus    # later: git pull; review what changed, then rerun
bin/mm-agent-host-install.sh        # or: mm-agent-host-install.sh --from <checkout>
```

The installer copies from the checkout it runs from, or from `--from DIR`. It never
takes its source from the installed plugin, and refuses one under `~/.cache/corral/`
or `~/.claude/`: a sandbox can write those
([arc42 §7.1](../docs/arc42/07-deployment-view.md#71-production-deployment-corral-host-managed)).
The host copy is only as trustworthy as the checkout you reviewed.

It copies `adapters/corral/` and `bin/` to `~/.local/share/mm-agent-bus/current/`,
where no sandbox can change them, and prints the config below. **Rerun it after every
plugin update**, from the checkout updated to that version;
`mm-agent-host-install.sh --check` tells whether the host copy is current, and the
start hook says so in the banner when it is not.

**Or follow `/plugin update` without installing:** point both `exec` entries at the
host's marketplace clone instead:

```yaml
exec: ~/.claude/plugins/marketplaces/mattermost-ai-agent-bus/adapters/corral/10-mm-session-start.sh
exec: ~/.claude/plugins/marketplaces/mattermost-ai-agent-bus/adapters/corral/90-mm-session-stop.sh
```

Nothing to rerun after an update. The cost: corral no longer asks before new hook
code runs, and since the corral homes share `~/.claude`, a sandboxed agent can change
that code. Choose it where that is acceptable — see
[arc42 §7.1](../docs/arc42/07-deployment-view.md#71-production-deployment-corral-host-managed).

**Otherwise never point `exec` at a plugin directory or at a checkout a session
works in.** The hooks run on the host with the registration secret, and corral approves only the
`exec` file by content, not the scripts it sources and starts. Both locations are
writable from inside a sandbox
([arc42 §7.1](../docs/arc42/07-deployment-view.md#71-production-deployment-corral-host-managed)).
The start hook warns if it finds itself there.

The bus is not repo-specific, so this belongs in the **global** config
`~/.config/corral/config.yml`:

```yaml
providers:
  hooks:
    enabled: true
    preStart:
      mm-bus:
        exec: ~/.local/share/mm-agent-bus/current/adapters/corral/10-mm-session-start.sh
        optional: true
    postEnd:
      mm-bus:
        exec: ~/.local/share/mm-agent-bus/current/adapters/corral/90-mm-session-stop.sh
        optional: true
```

`~` is expanded in `exec`, so the same entry works from any working directory. After
each reinstall corral asks once to approve the changed hooks: that prompt is your
check that new host code is about to run.

**`optional: true` is deliberate**: a Mattermost outage then degrades to a warning
instead of blocking every `corral run` on the machine. Set it to `false` if a session
without a bus identity is not acceptable.

## Host environment

The hooks inherit the host environment. Set these in the shell that runs `corral run`
(or hardcode them in the hook):

```bash
export MM_CHAT_URL="https://chat.example.com"
export MM_REGISTER_URL="${MM_CHAT_URL}/register/v1/agents"
export MM_TEAM=yourteam
export MM_CHANNEL=agents
export MM_PROJECT_CHANNEL=proj-foo     # optional, must already exist
```

**A bot is registered only for sessions with a project channel.** The plugin is meant
to be configured globally, but most sessions are a quick question that has no business
on the bus — without `MM_PROJECT_CHANNEL` `preStart` skips registration (one banner line
says why), so no bot, `HELLO` or sign-off lands in `MM_CHANNEL`. Set the project
channel per checkout (e.g. with direnv) to put that session on the bus; with it set,
registration is not optional. `MM_REGISTER=always` registers every session, as before.

The registration secret, **gopass preferred**:

```bash
export MM_REG_SECRET_GOPASS=path/to/mattermost/agent-registration-secret
# or, if you have no gopass:
export MM_REG_SECRET="…"
```

With neither set the hook exits 0 silently — a machine without a bus is not an error.

## What the sandbox receives

`MM_CHAT_URL`, `MM_REGISTER_URL`, `MM_BOT_TOKEN`, `MM_BOT_USERNAME`, `MM_AGENT_PREFIX`
(the registrar's username prefix,
[arc42 §7.1](../docs/arc42/07-deployment-view.md#71-production-deployment-corral-host-managed)),
`MM_BOT_USER_ID`, `MM_TEAM`, `MM_TEAM_ID`, `MM_CHANNEL`, `MM_CHANNEL_ID` and — when set —
`MM_PROJECT_CHANNEL`, `MM_PROJECT_CHANNEL_ID`, and `MM_TRUSTED_HUMANS`,
`MM_TRUSTED_HUMAN_USER_IDS`: the humans whose posts may approve actions, and
`MM_STATS_CHANNEL` when set on the host (override of the default `agents-stats`, or
`off`; the overhead summary, see
[arc42 §6.9](../docs/arc42/06-runtime-view.md#69-measuring-the-coordination-overhead)).
The trusted humans are listed by username on the host; without the list nobody is trusted, since `MM_REG_SECRET` is
the registrar's shared secret and says nothing about who you are
([arc42 §6.8](../docs/arc42/06-runtime-view.md#68-approval-from-a-trusted-human-over-the-bus)):

```bash
export MM_TRUSTED_HUMANS=you,teammate   # next to MM_REG_SECRET_GOPASS
```

`MM_RESPONSIBLE_HUMAN` names who answers for the agent (default: the first trusted
human) and `MM_AGENT_KIND=autonomous` marks an agent nobody runs interactively. Both,
with the trusted list, go along with the registration. A registrar that knows the
fields writes `for @you · trusts @you, @teammate` into the bot's profile with its
admin token; a bot cannot change its own profile. An older registrar ignores them,
and only the agent's `HELLO` says so. The start banner shows
`for @you, trusted @you, @teammate` or `trusted none (set MM_TRUSTED_HUMANS)`. Every listed account becomes an
approval key for your sessions: protect them with 2FA. `MM_OWNER` (one name) is
still read until 0.17.

Deliberately **not** contributed:

- `MM_REG_SECRET` — the whole point; it stays on the host.
- `MM_REG_BOT_NAME` — the registrar's short name for the bot (the username without
  its prefix). Not a secret: the registrar deletes a bot only with `MM_REG_SECRET` or that
  bot's own token, so from inside the sandbox an agent can remove at most its own bot.
  Withholding the name guards the host's lifecycle: without it the CLI finds no session
  file, so `mm-agent-session.sh stop` cannot tear down a session the host owns.

## Presence

The plugin's own Claude Code hooks keep the bot's Mattermost status current, so a
human can see at a glance which agents are busy:

| When | Presence | Custom status |
|---|---|---|
| session start (`preStart`) | online | 💤 `idle · until my human resumes` |
| a turn begins (`UserPromptSubmit`) | online | 🛠 `working · <project>` |
| a turn ends, project channel (`Stop`) | online | 👂 `listening · until HH:MM, then idle` for `MM_LISTEN_SEC` (default 900 s), expires on its own |
| a turn ends (`Stop`) | online | 💤 `idle · until my human resumes` |
| session end (`postEnd`) | offline | cleared |

Only presence and the project directory name are published — never a prompt, a
file or a task description. `postEnd` sets offline *before* unregistering, so a
bot that survives a failed teardown does not sit there looking available.

## Cleaning up leftovers

A failed teardown leaves a bot account behind. `postEnd` keeps the session file
when it cannot unregister — that file holds the bot's own token, which the
registrar accepts for deleting *that* bot, so the sweep works with no
`MM_REG_SECRET` at all:

```bash
./bin/mm-agent-sweep.sh            # dry run: list what would go
./bin/mm-agent-sweep.sh --apply
```

**A session file is not evidence that a bot is abandoned** — it exists for the
whole run. Each one records `MM_SESSION_PID`, the supervising corral process,
and a session whose process is still alive is skipped. Files written before that
guard existed have no PID and are skipped too; `--force` includes them. Without
this, sweeping while a session runs revokes that session's credentials mid-task.

When the registrar rejects a file's token (`401`) and Mattermost rejects it too,
the bot is gone already: the sweep reports `gone` and removes the stale file. Any
other failure is printed with its HTTP status and the file kept. Per-bot state
files (presence, listen window, turn, crown, stats) are no sessions; the sweep
removes those of bots without a session file once they are a day old
([arc42 §6.1](../docs/arc42/06-runtime-view.md#61-host-managed-session-lifecycle)).
A bot an agent registered itself (`mcp-*.env`, outside corral) is kept for
`claude --continue`; the sweep reports it as `KEPT` until its file is untouched for
`MM_SESSION_KEEP_SEC` (24 h), and `--force` includes it sooner
([arc42 §6.10](../docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent)).

Bots whose session file is already gone need the registration secret, because a
bot token can only delete its own agent (the registrar answers `401` otherwise):

```bash
export MM_REG_SECRET="$(gopass show -o "$MM_REG_SECRET_GOPASS")"
./bin/mm-agent-sweep.sh --apply
```

Run it on the **host**: session files never enter a sandbox.

## Gotchas

- **Editing a hook re-triggers corral's approve-once gate.** The next launch asks you to
  approve the changed executable. That is the feature working, not an error — read the
  diff, then approve.
- **`corral run --dry-run` never executes hooks**, so it cannot be used to verify this
  setup. Launch for real and read the banner's `preStart ran` row.
- **`MM_TEAM` / `MM_CHANNEL` must match the registrar's `DEFAULT_TEAM_NAME` /
  `DEFAULT_CHANNEL_NAME`.** They are two separate settings. A mismatch registers the bot
  fine and then fails with `default channel '<name>' does not exist in team '<team>'`.
- Session state lives in `${XDG_STATE_HOME:-~/.local/state}/mm-agent-bus/<session-id>.env`,
  one file per corral session so concurrent sessions do not clobber each other. `postEnd`
  removes it.

## Verifying

From inside a fresh session:

```bash
echo "$MM_BOT_USERNAME"   # agent-cc-…
echo "$MM_CHANNEL_ID"     # resolved id
echo "$MM_REG_SECRET"     # MUST be empty
```

On the host after the session ends, the `agent-cc-*` account should be gone from the team.

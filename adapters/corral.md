# corral adapter (host-managed sessions)

[corral](https://git.dg-i.net/pub/corral) sandboxes Claude Code and can run host-side
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

The bus is not repo-specific, so this belongs in the **global** config
`~/.config/corral/config.yml`:

```yaml
providers:
  hooks:
    enabled: true
    preStart:
      mm-bus:
        exec: ~/git/github.com/dembaca/mattermost-ai-agent-bus/adapters/corral/10-mm-session-start.sh
        optional: true
    postEnd:
      mm-bus:
        exec: ~/git/github.com/dembaca/mattermost-ai-agent-bus/adapters/corral/90-mm-session-stop.sh
        optional: true
```

`~` is expanded in `exec`, so the same entry works from any working directory. Both
scripts need the executable bit (`chmod +x adapters/corral/*.sh`).

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

The registration secret, **gopass preferred**:

```bash
export MM_REG_SECRET_GOPASS=path/to/mattermost/agent-registration-secret
# or, if you have no gopass:
export MM_REG_SECRET="…"
```

With neither set the hook exits 0 silently — a machine without a bus is not an error.

## What the sandbox receives

`MM_CHAT_URL`, `MM_REGISTER_URL`, `MM_BOT_TOKEN`, `MM_BOT_USERNAME`, `MM_BOT_USER_ID`,
`MM_TEAM`, `MM_TEAM_ID`, `MM_CHANNEL`, `MM_CHANNEL_ID` and — when set —
`MM_PROJECT_CHANNEL`, `MM_PROJECT_CHANNEL_ID`.

Deliberately **not** contributed:

- `MM_REG_SECRET` — the whole point; it stays on the host.
- `MM_BOT_NAME` — the registrar's delete key. Without it nothing inside the sandbox
  can ask the registrar to remove an agent.

## Presence

The plugin's own Claude Code hooks keep the bot's Mattermost status current, so a
human can see at a glance which agents are busy:

| When | Presence | Custom status |
|---|---|---|
| session start (`preStart`) | online | 💤 `idle · until my human resumes` |
| a turn begins (`UserPromptSubmit`) | online | 🛠 `working · <project>` |
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

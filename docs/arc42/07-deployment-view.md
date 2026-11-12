# 7. Deployment View

## Overview

Everything in this repository runs on the developer's machine, split across one
trust boundary: the **host** (may hold the registration secret, and in a corral
session the bot token, with the session's MCP server, RT-6.18) and the **agent
session** (inside a corral sandbox when host-managed, holding no credential;
outside corral it holds the bot token).
Mattermost and the registrar run elsewhere and are reached over HTTPS. The split
realises `#secure` Secret confinement; running every bus part as a short-lived,
fail-open process next to the agent realises `#reliable`.

---

## 7.1 Production Deployment: corral, host-managed

<!-- LEAN: no deployment diagram. -->

### Infrastructure Components

| Component | Type | Purpose | Sizing / Count |
|-----------|------|---------|----------------|
| Developer host | macOS / Linux, outside the sandbox | Runs corral and its host hooks; holds gopass | 1 per developer |
| corral sandbox | Process sandbox (seatbelt / equivalent) | Runs the agent runtime with a private `$HOME`, no `/tmp`, no secret | 1 per session |
| Mattermost server | Remote, HTTPS | Channels, posts, presence, WebSocket | Shared by all agents |
| Registrar | Remote, HTTPS (`…/register/v1/agents`, `…/register/v1/modes/<channel id>`, `…/register/v1/keys`) | Creates and deletes `agent-*` bots; signs whom each answers to (the attestation) and lists its public keys (RT-6.24); keeps a home's ask mode set with `/agent-ask-mode` and serves it signed (RT-6.25 step 8) | Shared |

**Bot username prefix.** The registrar names each bot `<prefix><name>`, the prefix
from its `BOT_USERNAME_PREFIX` (default `agent-`); `agent-*` in these docs stands
for whatever prefix it uses. The bus tells its own bots by it (roster and crown, the
sweep, the mentions it leaves in place, a task's assignee) and strips it to
unregister. It learns the prefix from its own registration: the username the
registrar returns minus the name it sent. A host-managed sandbox never sees that
name, so the host session hooks hand the prefix in as `MM_AGENT_PREFIX`; the session
files carry it for the MCP server and the shell CLI. With neither, `agent-`. It is
derived, not a setting.

**Props prefix (RT-6.16 step 8, since 0.74.0).** `MM_PROPS_PREFIX` names the keys an agent
may add to its posts' props and the bus's own system key (`<prefix>system`): lower-case
letters and digits starting with a letter, ending in `_`; default `x_`. A prefix that is
empty after trimming is the default; one that is malformed, or that a key of the bus or
of Mattermost starts with (`a2a_`, `agent_`, `from_`, `override_`, `webhook_`, …), stops
the MCP server at its start with the reason (in a corral session the host process refuses
before it reports ready, so the hook's preStart fails with it). It is one setting of the whole bus: every
server must have the same value, or one server does not recognise another's system replies
and refuses the props the other writes. Like `MM_TRUSTED_HUMANS` and `MM_REGISTRAR_KEYS`
it is set in a corral session in the host's environment (the host process gets it in its
start line, RT-6.18), elsewhere in the MCP server's environment. A program that reads the
props (a bridge) is configured with the same prefix.

**Registrar keys (RT-6.24).** `MM_REGISTRAR_KEYS` pins the registrar's public
keys: base64url raw Ed25519 keys, comma-separated, each one's kid derived from it.
It is a setting like `MM_TRUSTED_HUMANS`: in a corral session in the host's
environment (the host process gets it in its start line, RT-6.18), elsewhere in
the MCP server's environment. `bin/mm-agent-registrar-keys.sh` shows what the
registrar lists at `…/register/v1/keys`, with the kids checked; the operator
compares the values with the registrar's operator out of band and sets the variable
by hand. The bus never reads that endpoint at runtime. To rotate, pin the old and
the new key together until no attestation of the old one is younger than its `exp`.
Without the setting nothing is verified and the bus behaves as before 0.66.0.

### Software-to-Infrastructure Mapping

| Building Block (Section 5) | Deployed On | Instances | Notes |
|--------------------|-------------|-----------|-------|
| Host session hooks | Developer host (corral `preStart` / `postEnd`), only from the host copy: corral runs the dispatcher in `~/.local/share/mm-agent-bus/current.hooks/`, which starts the release `current` points at (RT-6.28) | 1 run each per session | Only block that sees `MM_REG_SECRET`; puts no credential into any process's environment or argv (Section 7.3) |
| Shell CLI | Developer host (host copy) and corral sandbox (plugin) | Per call | Same scripts on both sides; session files under `~/.local/state/mm-agent-bus/`. In the sandbox, as in any agent's shell, it has no token: the listener, `mm-agent-post.sh`, `-poll.sh`, `-status.sh` and the status line ask the MCP server (RT-6.17) |
| MCP server | Developer host (host copy, `--host`), started by `preStart`, stopped by `postEnd`; in the corral sandbox only its relay (the plugin's entry point, child of the agent runtime) | 1 host process per session | Long-lived; holds the WebSocket and the bot token; serves the relay and the hook API on `127.0.0.1`, two ephemeral ports, one per-session key; state in `~/.local/state/mm-agent-bus/hosted/<session>/` (RT-6.17, RT-6.18) |
| Turn hooks | corral sandbox, run by Claude Code | Per turn | Must finish within the hook timeout; ask the MCP server, hold no token |
| OpenCode plugin | Not in a corral session: in the OpenCode 2.x process (Bun) that serves the location — its long-running service, or a private server with `--standalone` / `opencode serve` — loaded from `.opencode/plugins/` or a `plugins` entry in `opencode.json` (Section 7.2, RT-6.20) | 1 per location (project directory) | Unloaded with the location (the service: after an hour without use) and loaded again; holds no token, writes no file, keeps its turn state in the plugin's storage, asks the MCP server's hook API |
| pi extension | In the pi process of a session — in the corral sandbox under `corral run pi` — loaded from `~/.pi/agent/extensions/`, a project's `.pi/extensions/`, an `extensions` path in pi's settings or `pi --extension` (Section 7.2, RT-6.27) | 1 per pi session | Replaced with the extension runtime (a reload, a session switch); holds no token, writes no file, keeps no turn state; asks the MCP server's hook API, one long poll per session |
| Agent skill | corral sandbox, loaded by the agent runtime | 1 per session | Plain Markdown, part of the plugin |

**Host copy.** corral approves a hook's `exec` file by its content, but not the
scripts it sources and starts. The plugin directory lives in the sandbox's private
home, the host's marketplace clone and plugin cache in `~/.claude`, and a checkout may
be a session's workdir; all are writable from inside a sandbox. Host hooks that ran
from there would run agent-changeable code on the host with the registration secret.
`bin/mm-agent-host-install.sh`, run on the host, copies `adapters/corral/` and `bin/`
from a git clone the developer reviewed (the one it runs from, or `--from`) into a
release directory of its own, `releases/<version>-dev.<commit>/` beside
`~/.local/share/mm-agent-bus/current` (`MM_BUS_HOST_DIR`), which no sandbox sees. It
copies the MCP server (`mcp/`) and the skill as well and prepares the server's
environment in that release with `uv sync --locked` (RT-6.18): without it corral
sessions get no bus, and the host process runs that code with the bot token. Only a
complete release is switched to: `current` is a symlink, replaced in one rename, and
the release before stays as `previous`; `--rollback` swaps the two, an older version
installs only with `--downgrade`, a failed sync leaves `current` as it was, and a
release goes once neither link nor a running session uses it. `preStart` records the
release a session runs from, and `postEnd` ends the session with that release's stop
hook (RT-6.28). corral's `exec` points at `current.hooks/mm-session-start.sh` and
`mm-session-stop.sh`, a dispatcher (`adapters/corral/mm-hook-dispatch.sh`) that stays
byte for byte the same across releases and starts the release's hook. corral approves
a hook by the content of its configured path (not what it sources or starts), asks
again only when that content changed, and skips a `postEnd` whose content changed
during the session: a switch of `current` therefore asks nothing of corral, and the
developer running the installer from a reviewed clone is the check of a switch. When
the dispatcher itself changes, corral asks once; declined, it refuses the session and
runs no hook. A host copy from before this layout (a directory `current`) moves to
`releases/<version>-legacy` and becomes `previous`.

**From a release tag** (ADR-011 phase 1b, RT-6.29). `--release v<x.y.z>` takes no
clone: it fetches that annotated tag from `MM_RELEASE_URL` (default the GitHub
repository, `https://` or a local path, which is for a mirror and the tests) into the
bare `src.git` beside `releases/`, without `+`, so a tag fetched before never moves;
checks that the tag object carries the asked name and that the three version files in
its tree say that version; and installs `git archive` of the tagged commit as
`releases/<x.y.z>/`. The signature check is opt-in (decided 2026-11-12): with
`MM_RELEASE_SIGNERS` set, the pinned Ed25519 public keys of the owner's release key and
its offline backup, it verifies the tag with `git verify-tag` (`gpg.format=ssh`)
against an allowed-signers file it writes from them and marks the release
`SOURCE=signed` with the tag, its object, the commit and the signer's fingerprint.
Unset, it warns on stderr that the signature is not checked and marks the release
`SOURCE=release`, `VERIFIED=no`, with tag, object and commit: the trust then rests on
the repository over https and on the tag object pinned in `releases/`. Only the owner
signs, once per release, after the merge (CLAUDE.md, "Releasing"); agents never sign.
The pinned keys come, like `MM_REGISTRAR_KEYS`, from the host's own environment and are
compared out of band; to rotate, pin old and new together. Keys from the variable that
verified a tag become a host property (ADR-011 decision (4)): the installer keeps them
in `release-signers` beside `releases/` and `src.git` (0600, `<type> <base64>` per
line, one rename), not in the state directory, since `XDG_STATE_HOME` comes from the
operator's environment, which a workdir's `.envrc` can redirect. Later runs and the
release check use that file when the variable is unset, so the MCP server's check under
corral, which does not see the operator's shell, verifies too. Other keys in the
variable, a rotation included, are refused unless `--replace-signers`;
`--forget-signers` removes the file. It is read like `release.env` and more strictly:
a regular file of the user's own, writable by no one else, at most 16 KiB, in a
directory of the user's own that no one else and no sandbox can write, each line
checked again as a key; anything else refuses the run instead of going unchecked. The
release check also writes `NEWEST` and the withdrawn versions to
`release-withdrawn.env` beside `releases/`; `--release` and `--rollback` refuse a
version listed there, naming the release that withdrew it, unless `--withdrawn-ok`
(decision (5)); without the file nothing is refused, and a line says so. A lightweight tag, a tag
named otherwise, a moved tag, another version in the tree, a failed fetch, and with
keys an unsigned tag or a signature by another key, install nothing and leave
`current` as it was, and so does the same version from another tag object than the one
`releases/` holds. A release's record is never marked down: one verified before stays
`signed` in a run without keys. The signature covers what is installed, not the
installer: it runs on the host, so it must be the host copy's
(`<MM_BUS_HOST_DIR>/bin/mm-agent-host-install.sh`) or a reviewed clone's, not the
plugin cache's or a workdir's. In every mode it refuses to run from a tree a sandbox
can write, but only after it sourced `mm-agent-lib.sh` from there: that catches a
mistake, and running the right installer is the control. The installer's output,
`--check` and the start hook say which kind runs: `signed release v<x.y.z>`,
`release v<x.y.z> (signature not checked)` or `unsigned`.

**Which release is out** (ADR-011 phase 2, RT-6.30). `bin/mm-agent-release-check.sh`,
run by the installer's `--check` and by the MCP server about once a day as a child
(clean environment: `PATH`, `HOME`, `LC_ALL=C`, `MM_RELEASE_URL`, `MM_RELEASE_SIGNERS`,
`MM_BUS_HOST_DIR`, `CORRAL_WORKDIR`; 120 s, then its process group is stopped; a
failed try again at the next hourly look, at most three a day), lists the newest `v<x.y.z>` tags at `MM_RELEASE_URL`, fetches them into the same
`src.git` (`--depth=1`, without `+`), checks them as `--release` does, and writes what
the CHANGELOG of the newest one that passes says into `release.env` in the host state
directory (`$XDG_STATE_HOME/mm-agent-bus`; the host process of a keyless session uses
the one preStart chose). It installs nothing, runs nothing from the tag, and refuses
to run inside corral or from a tree, state directory or `src.git` a sandbox can write.
A server whose own tree a sandbox can write (the plugin cache under `~/.claude`) never
runs its script from there: it runs the host copy's (`MM_BUS_HOST_DIR`, default
`~/.local/share/mm-agent-bus/current`) when that is out of every sandbox's reach, and
otherwise none; it still reads the notice the host's check wrote. The installer and
the check do not fetch into `src.git` at the same time: each refuses or skips while
the other's lock is fresh.
Under corral the host process gets `MM_RELEASE_*` and `MM_BUS_HOST_DIR` from preStart's
environment like every `MM_*` setting: set them where corral's hooks see them, or its
check runs with the defaults (no keys: `signature not checked`). The start hook, the
turn-end banner, `--check` and `bus_info` show the notice; a human installs the release.

**From a clone** (`--from`, or the checkout the installer is in) every install is
marked `SOURCE=unsigned`, for development. It copies only what is
committed (`git archive HEAD`, so no untracked file and no cache), and refuses a clone
with uncommitted changes or untracked files in the copied paths and a symlink among
them. git runs with no system or global config, no attributes file, fsmonitor or
hook, and a clone whose own config names a filter, diff or merge driver or an include,
or that has `info/attributes`, is refused: those make git run commands, and a sandbox
can write the config of any clone that was ever a session's workdir. It refuses a
clone whose repository (`--git-dir`, `--git-common-dir`) lies where a sandbox writes,
and prints the commit it installs (`<sha> <subject>`) to compare with the one
reviewed. Host-side code takes no input from the corral homes or the session's workdir
that decides what runs on the host: the installer never looks its source up among the
installed plugins, and refuses a source (symlinks resolved) under `~/.cache/corral/`,
`~/.claude/` (which the homes share by default), `$CORRAL_WORKDIR` or corral's
temporary directories (`/private/tmp/corral-*`, a corral `$TMPDIR`), compared without
regard to case (macOS volumes ignore it); a path with a `..` component counts as
sandbox-writable. It cannot know which clones were a session's workdir before: use
one that never was and is not a worktree of one, and check the printed sha. corral's `exec`
points there once; after a plugin update the developer updates the clone, reviews it
and reruns the installer; corral asks again only if the dispatcher changed. The start hook
says when a newer plugin is installed than the host copy; it names that plugin by
version only, with no path, as does `--check`.

**The installed copy is the only mode.** Before they source anything, both hooks
check, inline, where they run from: the path corral ran, every symlink on the way to
the hook file, the real tree and the Python the start hook would run (with its
symlinks) must lie outside `~/.cache/corral/`, `~/.claude/`, `$CORRAL_WORKDIR` and
corral's temporary directories — each step as written (no `..` component) and with its
directory resolved, without regard to case. Then they keep only the absolute `PATH`
entries outside those places (no `.`, empty or relative entry) and work from `/`, so
no command is found in the workdir; the installer and `mm-agent-sweep.sh` do the same.
The version notice reads the installed plugins' files, which sandboxes write, only as
regular files, bounded in size and time, so a FIFO there cannot stall a session's start.
Otherwise the start hook contributes only `MM_BUS_UNAVAILABLE` with how to install
(fail closed, no bot), and the stop hook runs nothing but a note on stderr. Pointing
`exec` at the marketplace clone, which earlier versions allowed by choice, is no
longer supported: it ran sandbox-writable code on the host with the registration
secret.

---

## 7.2 Environment Differences

The MCP server is required in every runtime (0.40.0); the shell-only agent mode, with a token in the agent's shell, is gone.

| Aspect | corral + Claude Code (recommended) | Claude Code without corral | Cursor | OpenCode, Codex |
|--------|-------------------------------------|----------------------------|--------|-----------------|
| Who registers | Host session hooks (`preStart`) | Agent via MCP `session_start` | Agent via MCP `session_start` | Agent via MCP `session_start` |
| Registration secret | Host only | The MCP server's environment: the plugin's server inherits Claude Code's, so it is in the agent's shell too unless the server command reads it itself ([adapters/claude.md](../../adapters/claude.md)) | The MCP server's environment. Recommended: the server command reads it itself (`sh -c 'MM_REG_SECRET="$(gopass show -o …)" exec uv run …'`, [adapters/cursor.md](../../adapters/cursor.md)). By reference (`${env:MM_REG_SECRET}`) it must be in Cursor's own environment, and so in the agent's shell too (on macOS Cursor takes the login shell's) | The MCP server's environment. Recommended: the server command reads it itself ([adapters/opencode.md](../../adapters/opencode.md)). By reference (`{env:MM_REG_SECRET}`, Codex's `env_vars`) it must be in the harness's own environment (OpenCode 2.x: the process that serves the location, its service unless `--standalone`), and so in the agent's shell too |
| What `MM_REG_SECRET` holds (ADR-003) | The host's human's enrollment token (`/agent-token create`): a bound bot. Or the shared secret: an unbound bot, foreign to others and without owner split | The session human's enrollment token, or the shared secret, as on the left | The same | The same |
| Bot token in agent environment | No: the MCP server runs on the host, the sandbox has its endpoints and key (RT-6.18) | No; the 0600 session file holds it (RT-6.10) | No; the session file holds it | No; the session file holds it |
| MCP server runs | On the host, one process per session; a relay in the sandbox | In the session, one per session | One per workspace (window) | OpenCode 2.x: one per location (project directory), started by the OpenCode process that serves it, with its environment; Codex: one per instance |
| Shell scripts find the server by (RT-6.17) | `MM_BUS_ENDPOINT` / `MM_BUS_KEY` | `CLAUDE_CODE_SESSION_ID` | `CURSOR_PROJECT_DIR` / `MM_WORKSPACE`, else walking up from `$PWD` to the nearest git top | Walking up from `$PWD` to the nearest git top |
| Automatic registration | Only with project channel, or `MM_REGISTER=always` | Agent registers only with project channel or when asked | Same | Same |
| Turn hooks (presence, drain, listen) | Yes | Yes | No | OpenCode 2.x with its plugin: presence, drain, sign-off, listen window with wake-ups (RT-6.20); OpenCode 1.x, Codex: no |
| Messages pushed into the session (RT-6.12) | When started as a channel | When started as a channel | No: `wait_for_events` | No: `wait_for_events`; OpenCode's plugin starts a turn that tells the agent to call it (RT-6.20) |
| Teardown safety net | `postEnd` stops the host MCP server (its exit handler unregisters), then unregisters again; the server's watchdog when corral dies | MCP exit handler (keeps the bot for `--continue`, reaped after 24 h); it runs when stdin closes, on SIGTERM/SIGHUP, or when the server's parent dies (RT-6.10) | MCP exit handler, the same | MCP exit handler, the same: a server orphaned by its harness (Codex) ends within seconds |
| Reachability | Agent checks + `Stop` hook (drain, listen window with a background listener, no model calls) | Same | Agent checks before each new activity and before the turn ends; listen window with one-shot timers and backoff (each wake-up a turn) | Agent checks; OpenCode with its plugin also drain and sign-off at the end of a turn and wake-ups in the listen window, as long as OpenCode serves the location (RT-6.20) |

**pi** with its extension (RT-6.27, [adapters/pi.md](../../adapters/pi.md)) is like OpenCode with its plugin, with these differences: one MCP server per pi session, which pi starts with its own environment (the registration secret is then in the agent's shell too, unless a same-named server in pi's `mcp.json` fetches it with a `!command` for the server alone); under `corral run pi` the host session hooks register the bot and the server is the relay to the host process, as for Claude Code (the token stays on the host); the turn cycle runs on pi's events, and a wake-up while a human's run goes follows that run (`deliverAs: followUp`) instead of waiting for its end; the context use reaches the status (ADR-008), which OpenCode's plugin does not report.

Without turn hooks nothing holds a turn open or reminds the agent, so the skill and the MCP `instructions` make the check before each new activity mandatory there ([SKILL.md, *Staying reachable*](../../skills/mattermost-ai-agent-bus/SKILL.md#staying-reachable), [runtime-no-hooks.md](../../skills/mattermost-ai-agent-bus/runtime-no-hooks.md)).

**A harness started from inside a Claude Code session** (`codex exec`, `opencode`, `cursor-agent` from the agent's shell, or a terminal Claude Code opened) inherits more than `CLAUDE_CODE_SESSION_ID`: `CLAUDE_CODE_OAUTH_TOKEN`, `CLAUDE_CODE_MESSAGING_TOKEN`, `CLAUDE_PID`, `CLAUDECODE` and `CLAUDE_PROJECT_DIR` as well, and its shell scripts look for the Claude session's server by its session id. Start it from a clean shell, or with all of them removed:

```bash
env -u CLAUDE_CODE_SESSION_ID -u CLAUDE_CODE_OAUTH_TOKEN -u CLAUDE_CODE_MESSAGING_TOKEN \
    -u CLAUDE_PID -u CLAUDECODE -u CLAUDE_PROJECT_DIR codex exec …
```

The MCP server guards its own side: a client that does not name itself `claude-code` in the handshake gets no channel mode, even with `MM_CHANNEL_PUSH=1` (RT-6.12), and its server drops an inherited `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR`, keys its files on the workspace and takes over no bot of that Claude session (RT-6.10, RT-6.17).

---

## 7.3 Trust Assumptions

The bus treats a trusted human's Mattermost account like the session's terminal (`#secure` Approval integrity, Section 1.2). That holds only under the assumptions below; where one of them fails, enable the bus there only with the limit in mind.

| Assumption | Holds when | Where it breaks |
|------------|------------|-----------------|
| A trusted account is as safe as the terminal | The instance is under your control, `MM_TRUSTED_HUMANS` is set on purpose (nobody is trusted by default), and every listed account has 2FA | A shared or taken-over account approves any action of every session that trusts it, tool calls included (RT-6.13) |
| What reaches Mattermost may be stored there | The instance's retention, search, exports, admin access and backups are acceptable for every post, DMs included | The permission relay DMs tool input to the responsible human (RT-6.13): a shell command in full, paths and short arguments, which the terminal shows only to the developer; file content only as its size. A secret in a command that Claude Code does not recognise and mask reaches the server |
| The profile says whom an agent answers to | The bot registered with an enrollment token its owner minted (`/agent-token`, registrar 0.2.0, ADR-003): the registrar makes that human the bot's native owner and writes `for @<owner> · trusts @…`. Since 0.60.0 every MCP server reads it to tell a peer from a foreign agent (ADR-001, RT-6.16 step 1); since 0.61.0 it reads the native owner (`owner_id` of `/api/v4/bots/<id>`) as well where the operator grants bot accounts `read_others_bots` ([registrar setup](https://github.com/dembaca/mattermost-agent-registrar#setup-for-enrollment-tokens), step 6) | A bot registered with the shared secret is `unbound` (`claims @…`, never `for @`): foreign to every other agent, and its own session has no owner split (`bus_info` and `session_start` warn). A registrar before 0.2.0 writes whatever owner the caller names, unchecked (RISK-02). A leaked enrollment token registers bots bound to its owner within its scope until it expires or is revoked. The registrar may write only part of a long trusted list into the profile (RT-6.8 warns); a list marked `+N` makes a foreign agent. An owner that cannot be read either way counts as a peer's (fail open). Without `read_others_bots` the native owner is not read (403) and the description alone decides, as in 0.60.0 |
| An agent's shell acts as its bot only through its own MCP server | Every runtime since 0.40.0: no token in any agent's shell; the scripts find their server by session id or workspace (RT-6.17) and get the closed set of its routes, posting included | Outside corral there is no sandbox: every process of the user can read the state directory, the endpoint file with its key and the 0600 session file with the token, as before 0.40.0. Keeping both out of the environment stops slips and leaks through logs and child processes, not a determined agent. A registration secret in the harness's own environment (a `${env:…}` reference) is in the agent's shell too (Section 7.2) |
| A session's hook API key acts as its bot, within the API's routes | Whoever holds `MM_BUS_KEY` (the endpoint file, or a corral sandbox's environment) is the session as far as the routes go (RT-6.17 step 3) | With the key one can post, reply in any thread and DM any agent as the bot (`/v1/post`, `/v1/reply`: the tools' rules, not the relay's slot), set its status, report its context numbers for the status text (`POST /v1/usage`, numbers only, RT-6.17), read its counters and the posts of the channels the bus watches — work, project and default channel and the guest channels (RT-6.22), never a DM, a group or the stats channel (`/v1/posts`, `403`). Never the token, the registration secret or a route beyond these. Under corral another sandbox of the same user can read a session's key from its process arguments and environment (`KERN_PROCARGS2` on macOS): a documented residual, accepted for sandboxes of one developer |
| The sandbox can act as its bot only through the session's MCP server | corral session (RT-6.18): the agent has the relay's and the hook API's endpoints and key in its environment | It can call both itself with that key and gets exactly what its runtime gets: the MCP tools of its session (the relay only while Claude Code's is not connected) and the hook API's routes (RT-6.17, the row above) — never the token, so nothing beyond them. The handshake's values only name the session's files; the server's files stay in the directory the host chose |
| No credential is in any process environment on the host | The host hooks keep the bot token and the registration secret in shell variables they do not export; `mm-agent-sweep.sh` likewise for a session file's token and the operator's token and secret; every helper (`mm-agent-register.sh`, `-channels`, `-status`, `-unread`, `-unregister`) gets its credential on fd 3 (`MM_BOT_TOKEN_FD`, `MM_REG_SECRET_FD`), curl on stdin, jq from a pipe (`--rawfile`, never a here-string, which bash up to 5.0 writes to a temporary file), the host process on stdin; no message prints a success answer (it holds the bot token); only an error answer's reason is printed (RT-6.1 step 3), sanitised and never when it contains the secret; the secret comes from gopass | On macOS a process in a corral sandbox can read the initial argv and environment of every process of the same user that runs a non-platform binary (`sysctl KERN_PROCARGS2`; `ps` is denied, the sysctl is not). A secret in the login environment (`MM_REG_SECRET`) is therefore visible in the hooks' own processes (the start hook warns). A session's `MM_BUS_KEY` is in its sandbox's environment and so readable by other sandboxes, which can then drive that session's relay and hook API — the closed set of RT-6.17/RT-6.18 actions — as its bot, never get the token. The root fix belongs in corral's sandbox profile |
| A session's token ends with the session | `postEnd` runs, or the host process's watchdog sees corral gone (every 5 s) | corral SIGKILLed and the watchdog not running, or a reboot: an orphan holds the token until the next `preStart` stops it, or the bot stays until `mm-agent-sweep.sh` removes it |
| The sandbox reaches the host process on loopback | corral's network mode is `net: open`, its only one today | A corral that cuts the sandbox off from `127.0.0.1` leaves the relay and the hooks without the server: the session has no bus (fail closed), never a token |
| Only the registrar creates bots with its prefix (ADR-003, relied on since 0.60.0) | Users may not create bot accounts in Mattermost, or the prefix is reserved for the registrar | A bot with the prefix made by someone else counts as a bus agent and can carry any description, owner included |
| Only the registrar account and system admins edit bot descriptions and owners (ADR-003, relied on since 0.60.0) | No other account may manage bots (the registrar's setup, step 5) | Whoever can edit a description can name any trusted list, and any owner while the native owner is not read; with `read_others_bots` a `for @` that disagrees with `owner_id` makes a foreign agent, so only someone who can also reassign the bot (an admin) names another owner (RISK-03, Section 11). With the registrar's keys pinned, an agent with a valid attestation is judged by it: neither a description nor `owner_id` changes its owner or trusted list (RT-6.24) |
| The registrar's signature says whom an agent answers to (RT-6.24, since 0.66.0) | The registrar keeps its signing key to itself, every host pinned keys it compared out of band (`MM_REGISTRAR_KEYS`, Section 7.1), and the registrar binds owners with enrollment tokens (`via: enrollment`) | A leaked signing key signs any owner for any bot until every host unpins it. A key pinned without comparing trusts whoever answered `…/register/v1/keys`. `via: secret` is only the caller's claim and makes no agent more trusted than without it: its owner counts only where a readable native owner agrees, so a shared-secret bot stays foreign (RISK-02). An attestation counts until its `exp`, which the registrar chooses, whatever changes in Mattermost meanwhile. It is public on the bot's account: a copy on another bot fails its `bot_id` and `username`. A host without pinned keys judges by the profile, as before |
| Everyone who can post in the channel may steer the agents | One human per channel, or only people you would let type into the session; with trusted humans configured, the wake gate holds it for the workers (RT-6.16) | Without trusted humans, any channel member wakes an agent with a mention, and the agent reads the text. With them, a post by anyone else wakes no worker and is no task, but the coordinator still reads it. An agent of another owner, or one that trusts a human this session does not, is foreign: its request is no task (ADR-001), so it cannot relay such a human's request. A peer that was itself injected still can pass a request on as its own (RISK-01); and with the MCP server down nothing is gated. Only approvals are verified by the server; that the rest of a post is data is a rule for the agent, not a technical barrier |

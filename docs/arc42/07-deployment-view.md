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
| Registrar | Remote, HTTPS (`…/register/v1/agents`) | Creates and deletes `agent-*` bots | Shared |

**Bot username prefix.** The registrar names each bot `<prefix><name>`, the prefix
from its `BOT_USERNAME_PREFIX` (default `agent-`); `agent-*` in these docs stands
for whatever prefix it uses. The bus tells its own bots by it (roster and crown, the
sweep, the mentions it leaves in place, a task's assignee) and strips it to
unregister. It learns the prefix from its own registration: the username the
registrar returns minus the name it sent. A host-managed sandbox never sees that
name, so the host session hooks hand the prefix in as `MM_AGENT_PREFIX`; the session
files carry it for the MCP server and the shell CLI. With neither, `agent-`. It is
derived, not a setting.

### Software-to-Infrastructure Mapping

| Building Block (Section 5) | Deployed On | Instances | Notes |
|--------------------|-------------|-----------|-------|
| Host session hooks | Developer host (corral `preStart` / `postEnd`), only from the host copy `~/.local/share/mm-agent-bus/current/` | 1 run each per session | Only block that sees `MM_REG_SECRET`; puts no credential into any process's environment or argv (Section 7.3) |
| Shell CLI | Developer host (host copy) and corral sandbox (plugin) | Per call | Same scripts on both sides; session files under `~/.local/state/mm-agent-bus/`. In the sandbox it has no token: the listener, `mm-agent-status.sh` and the status line ask the MCP server |
| MCP server | Developer host (host copy, `--host`), started by `preStart`, stopped by `postEnd`; in the corral sandbox only its relay (the plugin's entry point, child of the agent runtime) | 1 host process per session | Long-lived; holds the WebSocket and the bot token; serves the relay and the hook API on `127.0.0.1`, two ephemeral ports, one per-session key; state in `~/.local/state/mm-agent-bus/hosted/<session>/` (RT-6.17, RT-6.18) |
| Turn hooks | corral sandbox, run by Claude Code | Per turn | Must finish within the hook timeout; ask the MCP server, hold no token |
| Agent skill | corral sandbox, loaded by the agent runtime | 1 per session | Plain Markdown, part of the plugin |

**Host copy.** corral approves a hook's `exec` file by its content, but not the
scripts it sources and starts. The plugin directory lives in the sandbox's private
home, the host's marketplace clone and plugin cache in `~/.claude`, and a checkout may
be a session's workdir; all are writable from inside a sandbox. Host hooks that ran
from there would run agent-changeable code on the host with the registration secret.
`bin/mm-agent-host-install.sh`, run on the host, copies `adapters/corral/` and `bin/`
from a git clone the developer reviewed (the one it runs from, or `--from`) to
`~/.local/share/mm-agent-bus/current/` (`MM_BUS_HOST_DIR`), which no sandbox sees. It
copies the MCP server (`mcp/`) and the skill as well and prepares the server's
environment there with `uv sync --locked` (RT-6.18): without it corral sessions get
no bus, and the host process runs that code with the bot token. It copies only what is
committed (`git archive HEAD`, so no untracked file and no cache), and refuses a clone
with uncommitted changes or untracked files in the copied paths and a symlink among
them. Host-side code takes no input from the corral homes or the session's workdir
that decides what runs on the host: the installer never looks its source up among the
installed plugins, and refuses a source (symlinks resolved) under `~/.cache/corral/`,
`~/.claude/` (which the homes share by default) or `$CORRAL_WORKDIR`. corral's `exec`
points there once; after a plugin update the developer updates the clone, reviews it
and reruns the installer, and corral asks to approve the changed hook. The start hook
says when a newer plugin is installed than the host copy; it names that plugin by
version only, with no path, as does `--check`.

**The installed copy is the only mode.** Before they source anything, both hooks
check, inline, where they run from: the path corral ran, every symlink on the way to
the hook file, the real tree and the Python the start hook would run (with its
symlinks) must lie outside `~/.cache/corral/`, `~/.claude/` and `$CORRAL_WORKDIR`.
Otherwise the start hook contributes only `MM_BUS_UNAVAILABLE` with how to install
(fail closed, no bot), and the stop hook runs nothing but a note on stderr. Pointing
`exec` at the marketplace clone, which earlier versions allowed by choice, is no
longer supported: it ran sandbox-writable code on the host with the registration
secret.

---

## 7.2 Environment Differences

| Aspect | corral + Claude Code (recommended) | Claude Code without corral | Cursor | OpenCode |
|--------|-------------------------------------|----------------------------|--------|----------|
| Who registers | Host session hooks (`preStart`) | Agent via MCP `session_start` | `sessionStart` hook | Agent via CLI |
| Secret in agent environment | No | Yes (`MM_REG_SECRET`) | Yes | Yes |
| Bot token in agent environment | No: the MCP server runs on the host, the sandbox has its endpoints and key (RT-6.18) | Yes (the session file, RT-6.10) | Yes | Yes |
| MCP server runs | On the host, one process per session; a relay in the sandbox | In the session | In the session | In the session |
| Automatic registration | Only with project channel, or `MM_REGISTER=always` | Agent registers only with project channel or when asked | Only with project channel, or `MM_REGISTER=always` | Agent decides per skill |
| Turn hooks (presence, drain, listen) | Yes | Yes | No | No |
| Teardown safety net | `postEnd` stops the host MCP server (its exit handler unregisters), then unregisters again; the server's watchdog when corral dies | MCP exit handler | `sessionEnd` hook | `mm-agent-session.sh stop` |
| Reachability | Agent checks + `Stop` hook (drain, listen window with a background listener, no model calls) | Same | Agent checks before each new activity and before the turn ends; listen window with one-shot timers and backoff (each wake-up a turn) | Agent checks only |

Without turn hooks nothing holds a turn open or reminds the agent, so the skill and the MCP `instructions` make the check before each new activity mandatory there ([SKILL.md, *Staying reachable*](../../skills/mattermost-ai-agent-bus/SKILL.md#staying-reachable)).

---

## 7.3 Trust Assumptions

The bus treats a trusted human's Mattermost account like the session's terminal (`#secure` Approval integrity, Section 1.2). That holds only under the assumptions below; where one of them fails, enable the bus there only with the limit in mind.

| Assumption | Holds when | Where it breaks |
|------------|------------|-----------------|
| A trusted account is as safe as the terminal | The instance is under your control, `MM_TRUSTED_HUMANS` is set on purpose (nobody is trusted by default), and every listed account has 2FA | A shared or taken-over account approves any action of every session that trusts it, tool calls included (RT-6.13) |
| What reaches Mattermost may be stored there | The instance's retention, search, exports, admin access and backups are acceptable for every post, DMs included | The permission relay DMs tool input to the responsible human (RT-6.13): a shell command in full, paths and short arguments, which the terminal shows only to the developer; file content only as its size. A secret in a command that Claude Code does not recognise and mask reaches the server |
| The profile says whom an agent answers to | Host-managed session: the registrar writes `for @… · trusts @…` and the agent holds no registration secret (Section 7.1) | A session that registers itself holds `MM_REG_SECRET` and names its responsible and trusted humans at registration (RT-6.10, Section 7.2): the registrar writes what the caller names, nobody verifies it. The registrar may write only part of a long trusted list into the profile (RT-6.8 warns) |
| The sandbox can act as its bot only through the session's MCP server | corral session (RT-6.18): the agent has the relay's and the hook API's endpoints and key in its environment | It can call both itself with that key and gets exactly what its runtime gets: the MCP tools of its session (the relay only while Claude Code's is not connected) and the hook API's routes (RT-6.17) — never the token, so nothing beyond them. The handshake's values only name the session's files; the server's files stay in the directory the host chose |
| No credential is in any process environment on the host | The host hooks keep the bot token and the registration secret in shell variables they do not export; every helper (`mm-agent-register.sh`, `-channels`, `-status`, `-unread`, `-unregister`) gets its credential on fd 3 (`MM_BOT_TOKEN_FD`, `MM_REG_SECRET_FD`), curl on stdin, jq from a pipe (`--rawfile`), the host process on stdin; the secret comes from gopass | On macOS a process in a corral sandbox can read the initial argv and environment of every process of the same user that runs a non-platform binary (`sysctl KERN_PROCARGS2`; `ps` is denied, the sysctl is not). A secret in the login environment (`MM_REG_SECRET`) is therefore visible in the hooks' own processes (the start hook warns). A session's `MM_BUS_KEY` is in its sandbox's environment and so readable by other sandboxes, which can then drive that session's relay and hook API — the closed set of RT-6.17/RT-6.18 actions — as its bot, never get the token. The root fix belongs in corral's sandbox profile |
| A session's token ends with the session | `postEnd` runs, or the host process's watchdog sees corral gone (every 5 s) | corral SIGKILLed and the watchdog not running, or a reboot: an orphan holds the token until the next `preStart` stops it, or the bot stays until `mm-agent-sweep.sh` removes it |
| The sandbox reaches the host process on loopback | corral's network mode is `net: open`, its only one today | A corral that cuts the sandbox off from `127.0.0.1` leaves the relay and the hooks without the server: the session has no bus (fail closed), never a token |
| Everyone who can post in the channel may steer the agents | One human per channel, or only people you would let type into the session; with trusted humans configured, the wake gate holds it for the workers (RT-6.16) | Without trusted humans, any channel member wakes an agent with a mention, and the agent reads the text. With them, a post by anyone else wakes no worker and is no task, but the coordinator still reads it; an agent that was itself injected can pass a request on as its own; and with the MCP server down nothing is gated. Only approvals are verified by the server; that the rest of a post is data is a rule for the agent, not a technical barrier |

# 7. Deployment View

## Overview

Everything in this repository runs on the developer's machine, split across one
trust boundary: the **host** (may hold the registration secret) and the **agent
session** (inside a corral sandbox when host-managed; holds only the bot token).
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
files carry it for the turn hooks and the listener. With neither, `agent-`. It is
derived, not a setting.

### Software-to-Infrastructure Mapping

| Building Block (Section 5) | Deployed On | Instances | Notes |
|--------------------|-------------|-----------|-------|
| Host session hooks | Developer host (corral `preStart` / `postEnd`), from the host copy `~/.local/share/mm-agent-bus/current/`, or by choice from the host's marketplace clone | 1 run each per session | Only block that sees `MM_REG_SECRET` |
| Shell CLI | Developer host (host copy) and corral sandbox (plugin) | Per call | Same scripts on both sides; session files under `~/.local/state/mm-agent-bus/` |

**Host copy.** corral approves a hook's `exec` file by its content, but not the
scripts it sources and starts. The plugin directory lives in the sandbox's private
home, and a checkout may be a session's workdir; both are writable from inside a
sandbox. Host hooks that ran from there would run agent-changeable code on the host
with the registration secret. `bin/mm-agent-host-install.sh`, run on the host,
copies `adapters/corral/` and `bin/` from the installed plugin to
`~/.local/share/mm-agent-bus/current/` (`MM_BUS_HOST_DIR`), which no sandbox sees.
corral's `exec` points there once; after a plugin update the developer reruns the
installer, and corral asks to approve the changed hook. The start hook warns when
it runs from the plugin directory or the session's workdir, and when the host copy
is older than the installed plugin.

**From the marketplace clone, by choice.** corral's `exec` may point straight at
the host's marketplace clone,
`~/.claude/plugins/marketplaces/mattermost-ai-agent-bus/adapters/corral/`. Its path
is stable and `/plugin update` moves it, so updates take effect with the next session
and there is no installer to rerun. This gives up the protection above: corral no
longer asks before new hook code runs, and where `~/.claude` is shared with the
corral homes (the default) a sandboxed agent can change the code the host then runs —
outside the sandbox, with the registration secret. It suits a host where that is
already true for other reasons (agents that never run in corral, such as Cursor), and
a registration secret that can do no more than create bots that may delete
themselves. The start hook does not warn for this path; it still says when the
installed plugin is newer than the clone (`/plugin update`), and it still warns for
a versioned plugin directory, the corral home's plugins and the session's workdir.
| MCP server | corral sandbox, child of the agent runtime (`uv run`) | 1 per session | Long-lived; holds the WebSocket |
| Turn hooks | corral sandbox, run by Claude Code | Per turn | Must finish within the hook timeout |
| Agent skill | corral sandbox, loaded by the agent runtime | 1 per session | Plain Markdown, part of the plugin |

---

## 7.2 Environment Differences

| Aspect | corral + Claude Code (recommended) | Claude Code without corral | Cursor | OpenCode |
|--------|-------------------------------------|----------------------------|--------|----------|
| Who registers | Host session hooks (`preStart`) | Agent via MCP `session_start` | `sessionStart` hook | Agent via CLI |
| Secret in agent environment | No | Yes (`MM_REG_SECRET`) | Yes | Yes |
| Automatic registration | Only with project channel, or `MM_REGISTER=always` | Agent registers only with project channel or when asked | Only with project channel, or `MM_REGISTER=always` | Agent decides per skill |
| Turn hooks (presence, drain, listen) | Yes | Yes | No | No |
| Teardown safety net | `postEnd` + MCP exit handler | MCP exit handler | `sessionEnd` hook | `mm-agent-session.sh stop` |
| Reachability | Agent checks + `Stop` hook (drain, listen window with a background listener, no model calls) | Same | Agent checks before each new activity and before the turn ends; listen window with one-shot timers and backoff (each wake-up a turn) | Agent checks only |

Without turn hooks nothing holds a turn open or reminds the agent, so the skill and the MCP `instructions` make the check before each new activity mandatory there ([SKILL.md, *Staying reachable*](../../skills/mattermost-ai-agent-bus/SKILL.md#staying-reachable)).

---

## 7.3 Trust Assumptions

The bus treats a trusted human's Mattermost account like the session's terminal (`#secure` Approval integrity, Section 1.2). That holds only under the assumptions below; where one of them fails, enable the bus there only with the limit in mind.

| Assumption | Holds when | Where it breaks |
|------------|------------|-----------------|
| A trusted account is as safe as the terminal | The instance is under your control, `MM_TRUSTED_HUMANS` is set on purpose (nobody is trusted by default), and every listed account has 2FA | A shared or taken-over account approves any action of every session that trusts it, tool calls included (RT-6.13) |
| What reaches Mattermost may be stored there | The instance's retention, search, exports, admin access and backups are acceptable for every post, DMs included | The permission relay DMs tool input to the responsible human (RT-6.13): a shell command in full, paths and short arguments, which the terminal shows only to the developer; file content only as its size. A secret in a command that Claude Code does not recognise and mask reaches the server |
| The profile says whom an agent answers to | Host-managed session: the registrar writes `for @… · trusts @…` and the agent holds no registration secret (Section 7.1) | A session that registers itself holds `MM_REG_SECRET` and names its responsible and trusted humans at registration (RT-6.10, Section 7.2): the registrar writes what the caller names, nobody verifies it |
| Everyone who can post in the channel may steer the agents | One human per channel, or only people you would let type into the session; with trusted humans configured, the wake gate holds it for the workers (RT-6.16) | Without trusted humans, any channel member wakes an agent with a mention, and the agent reads the text. With them, a post by anyone else wakes no worker and is no task, but the coordinator still reads it; an agent that was itself injected can pass a request on as its own; and with the MCP server down nothing is gated. Only approvals are verified by the server; that the rest of a post is data is a rule for the agent, not a technical barrier |

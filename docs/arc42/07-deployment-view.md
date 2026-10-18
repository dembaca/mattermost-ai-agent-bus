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

### Software-to-Infrastructure Mapping

| Building Block (Section 5) | Deployed On | Instances | Notes |
|--------------------|-------------|-----------|-------|
| Host session hooks | Developer host (corral `preStart` / `postEnd`) | 1 run each per session | Only block that sees `MM_REG_SECRET` |
| Shell CLI | Developer host and corral sandbox | Per call | Same scripts on both sides; session files under `~/.local/state/mm-agent-bus/` |
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

# 1. Introduction and Goals

## 1.1 Requirements Overview

The Mattermost AI agent bus lets coding agents (Claude Code, Cursor, OpenCode) that
run in separate sessions coordinate their work through Mattermost, each under its
own short-lived bot identity.

### Essential Features

- One ephemeral bot per session, registered through a registrar and removed when the session ends
- Host-managed lifecycle: the registration secret stays on the host, only the bot token enters the sandbox
- Post, reply in threads and receive mentions, DMs and work-channel posts (MCP server with a WebSocket inbox)
- Presence: 🛠 working, 👂 listening, 💤 idle, offline, without publishing any content
- A turn cannot end with a mention or DM unanswered; in a project channel the agent stays reachable for a listen window
- Coordination manifest: announce, coordinate, sign off (`HELLO`, `IDLE`, `BYE`, `HANDOVER`, …)
- Only sessions with a project channel register automatically: no noise from quick questions

### References

- Wire protocol and message vocabulary: [docs/protocol.md](../protocol.md)
- Agent-facing rules: [SKILL.md](../../skills/mattermost-ai-agent-bus/SKILL.md)

---

## 1.2 Quality Goals

> Top quality requirements of highest importance to major stakeholders.
> All architectural decisions must support these goals.
> ⚠️ **Draft derived from the code — must be reviewed and signed off by the maintainer (1.3).**

| Priority | Quality Goal | Concrete Scenario |
|:--------:|-------------|-------------------|
| 1 | `#reliable` Never wedges a coding session | Mattermost, the registrar or `jq` unavailable: every hook exits 0 within its timeout (Stop 10 s, UserPromptSubmit 5 s) and the session runs as if no bus existed |
| 2 | `#secure` Secret confinement | In a host-managed session `MM_REG_SECRET` is absent from the sandbox environment in 100 % of sessions, so an agent can create no bot and remove none but its own; no bot outlives its session unnoticed (`mm-agent-sweep.sh` finds every leftover) |
| 3 | `#usable` Reachability | A mention to a working agent is collected before its turn ends; in a project channel a mention arriving within 15 min after the turn ends is answered without human action |
| 4 | `#usable` Low noise | A session without a project channel posts nothing and registers no bot, unless the human tells the agent to do so (or sets `MM_REGISTER=always`); status changes carry no prompt, file or task content |

See Section 10 for detailed quality scenarios (not yet written).

---

## 1.3 Stakeholder

| Role / Name | Contact | Expectations from Architecture |
|-------------|---------|--------------------------------|
| Maintainer | repository owner | Changes stay consistent across code, runtime texts and docs |
| Developer running agents | — | The bus never blocks work; quick questions stay off the bus |
| Coding agents (Claude Code, Cursor, OpenCode) | via SKILL.md / MCP instructions | Unambiguous rules: when to register, post, listen, sign off |
| People reading the channel | Mattermost | See at a glance who works on what, without chatter |
| Operator of Mattermost and the registrar | — | Bots are ephemeral; the registration secret can be rotated |

**Quality goal sign-off:** Maintainer

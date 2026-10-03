# Agent bus protocol

## Overview

Agents coordinate in a shared Mattermost team/channel (commonly `#agents`) using
**ephemeral bots** created through a registrar service
([mattermost-agent-registrar](https://github.com/dembaca/mattermost-agent-registrar)).

```
┌─────────────┐   MM_REG_SECRET    ┌────────────┐   admin PAT   ┌────────────┐
│ Cursor/etc. │ ─────────────────► │ Registrar  │ ────────────► │ Mattermost │
└─────────────┘   POST/DELETE bot  └────────────┘               └────────────┘
       │                                                              ▲
       └──────── bot token: posts / WS / threads ─────────────────────┘
```

## Lifecycle

1. **Register** — `POST /register/v1/agents` with JSON `{ "name", "display_name" }`  
   Auth: `Authorization: Bearer $MM_REG_SECRET`  
   Response: `{ url, username, bot_token, user_id }`  
   Username is typically `agent-<name>`.
2. **Work** — use `bot_token` against Mattermost API v4 (and optional WebSocket).
3. **Unregister** — `DELETE /register/v1/agents/<name>`  
   Auth: registration secret **or** the bot’s own token.  
   Idempotent (`204` / `404` treated as success).

CLI wrappers: `bin/mm-agent-session.sh start|stop`, `register`, `unregister`.

## Who owns the lifecycle

Steps 1 and 3 can run **on the host, outside the agent's sandbox** — a corral
`preStart` / `postEnd` hook pair (see [adapters/corral.md](../adapters/corral.md)).
Three properties follow, and they are the reason to prefer it:

- `MM_REG_SECRET` never crosses into the sandbox. Only the minted, short-lived bot
  token does, so a compromised agent cannot create or delete agents.
- `MM_BOT_NAME` — the registrar's delete key — is withheld too.
- Teardown is the host's job, so it still happens when the agent crashes or the
  launch aborts.

Under that model the agent must **not** register or unregister. The signal is
`MM_BOT_TOKEN`: already set ⇒ a session exists and is managed elsewhere.

## Channels

| Variable | Role |
|----------|------|
| `MM_CHANNEL` | The registrar's default channel. `ensureTeamChannel` adds every new bot to the team and to this channel at register time, so membership is guaranteed. The agent stays addressable here. |
| `MM_PROJECT_CHANNEL` | Optional. Where this session works; posts default here. Unknown to the registrar, so the bot joins it itself. |

Both are watched by the inbox, so a request may arrive in either.

**Neither is ever created.** `bin/mm-agent-channels.sh` resolves names to ids and joins
an existing project channel; a missing channel is an error naming the channel, so an
operator creates it deliberately. Note `MM_TEAM` / `MM_CHANNEL` must agree with the
registrar's `DEFAULT_TEAM_NAME` / `DEFAULT_CHANNEL_NAME`.

Joining is not creating, but it is required: Mattermost checks `create_post` against
channel membership, so a non-member is refused even in a public channel.

## Job threads

| Convention | Detail |
|------------|--------|
| Channel | `MM_PROJECT_CHANNEL` when set, else `MM_CHANNEL` |
| Job root | Top-level post describing the job (who owns it, goal, links) |
| Progress | Replies in the same thread (`root_id` = root post id) |
| Mentions | `@username` or `<@user_id>` to wake a specific bot |
| DMs | Channel type `D` — always treated as inbox events for the bot |
| Handoff | Reply `@other-agent …` with context; that agent’s inbox classifies the mention |

Suggested prefixes in threads: `ACK`, `PROGRESS`, `BLOCKED`, `RESULT`, then
orchestrator `DONE id=…` / `CANCEL id=…`.

## Inbox classification

The MCP `wait_for_events` tool (and `classify_post`) marks a post as relevant when:

1. **Mention** — message contains `<@BOT_USER_ID>` or `@BOT_USERNAME` (case-insensitive), or
2. **DM** — channel type is `D`, or
3. **Watched channel** — post is in a configured watched channel id. By default that is
   the default channel plus, when set, the project channel.

Own posts from the bot are ignored.

## Polling vs WebSocket

- **CLI poll** (`mm-agent-poll.sh`) — REST `channels/{id}/posts?since=` JSON lines.
- **MCP inbox** — Mattermost WebSocket `posted` events with classification filter.

Prefer WebSocket for interactive agents; poll is fine for cron-style loops.

## Security

- Treat `MM_REG_SECRET` like a deploy credential (creates/destroys bots). Prefer the
  host-managed lifecycle above, which keeps it out of the agent's reach entirely.
- Ephemeral bots reduce leftover identities; rotate the secret if leaked.
- Do not VSO-sync or commit bot tokens / session `.env` files.
- Agents never create channels or teams — a missing channel is reported, not worked
  around.

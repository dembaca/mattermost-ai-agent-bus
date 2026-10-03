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

## Job threads

| Convention | Detail |
|------------|--------|
| Channel | `MM_CHANNEL` (default from env; site-specific, e.g. `agents`) |
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
3. **Watched channel** — post is in a configured watched channel id (usually `#agents`).

Own posts from the bot are ignored.

## Polling vs WebSocket

- **CLI poll** (`mm-agent-poll.sh`) — REST `channels/{id}/posts?since=` JSON lines.
- **MCP inbox** — Mattermost WebSocket `posted` events with classification filter.

Prefer WebSocket for interactive agents; poll is fine for cron-style loops.

## Security

- Treat `MM_REG_SECRET` like a deploy credential (creates/destroys bots).
- Ephemeral bots reduce leftover identities; rotate the secret if leaked.
- Do not VSO-sync or commit bot tokens / session `.env` files.

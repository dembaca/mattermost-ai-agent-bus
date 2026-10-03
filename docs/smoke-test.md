# Smoke test

End-to-end check against a real Mattermost + [registrar](https://github.com/dembaca/mattermost-agent-registrar):
register an ephemeral bot, post, get addressed, unregister.

Replace the placeholders with your own site, and pull `MM_REG_SECRET` from
whatever secret store you use — never inline it.

## CLI

```bash
export MM_CHAT_URL=https://chat.example.com
export MM_REGISTER_URL="$MM_CHAT_URL/register/v1/agents"
export MM_REG_SECRET="…"          # e.g. from a password manager / vault
export MM_TEAM=yourteam MM_CHANNEL=agents

eval "$(./bin/mm-agent-session.sh start smoke-bus)"
```

`start` exports `MM_BOT_TOKEN`, `BOT_USER_ID`, `BOT_USERNAME` and `CHANNEL_ID`.

```bash
./bin/mm-agent-post.sh "$CHANNEL_ID" "smoke: hello from the bus"
./bin/mm-agent-poll.sh --once
```

Then mention the bot from a human account in `#agents` (`@agent-smoke-bus ping`)
and confirm the next poll returns that post.

```bash
./bin/mm-agent-session.sh stop
```

`stop` unregisters the bot and removes the session file. Verify the
`agent-smoke-bus` account is gone from the team.

## MCP

Same env, then:

```bash
cd mcp && uv sync
uv run mattermost-ai-agent-bus-mcp
```

Drive `session_start` → `post_message` → `wait_for_events` → `session_end` from
your runtime (see [`adapters/`](../adapters)). `wait_for_events` should surface
the mention via WebSocket without polling.

## Checklist

- [ ] Bot appears in the team after `session_start`
- [ ] Post lands in `MM_CHANNEL`
- [ ] Mention and DM both classify as inbox events
- [ ] Thread reply uses the root post id
- [ ] Bot is removed after `session_end`
- [ ] No token or session file left in the working tree

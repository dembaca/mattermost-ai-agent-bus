# Smoke test

End-to-end check against a real Mattermost + [registrar](https://github.com/dembaca/mattermost-agent-registrar):
register an ephemeral bot, post, get addressed, unregister.

Replace the placeholders with your own site, and pull `MM_REG_SECRET` from
whatever secret store you use — never inline it.

## MCP and shell clients

Configure the MCP server for your runtime ([`adapters/`](../adapters)) with
`MM_CHAT_URL`, `MM_REGISTER_URL`, `MM_TEAM`, `MM_CHANNEL` and `MM_REG_SECRET` (a
reference in its config, never a literal). Drive `session_start` → `post_message` →
`wait_for_events` → `session_end` from your runtime. `wait_for_events` should
surface a mention (`@agent-smoke-… ping` from a human account) via WebSocket
without polling. Before `session_end`, from the agent's shell (no token there):

```bash
./bin/mm-agent-post.sh "smoke: hello from the shell"   # through the MCP server
./bin/mm-agent-poll.sh --once                           # shows the mention too
```

## Checklist

- [ ] Bot appears in the team after `session_start`
- [ ] Post lands in `MM_CHANNEL`
- [ ] Mention and DM both classify as inbox events
- [ ] Thread reply uses the root post id
- [ ] `mm-agent-post.sh` / `-poll.sh` reach the server from the agent's shell (exit 3 without it)
- [ ] Bot is removed after `session_end`
- [ ] No token or session file left in the working tree

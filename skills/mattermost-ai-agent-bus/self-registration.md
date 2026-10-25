# Registering the session yourself

Read this when `MM_BUS_MCP_ENDPOINT`, `MM_BOT_TOKEN` and `MM_BUS_UNAVAILABLE` are unset
and `MM_PROJECT_CHANNEL` is set: no host
hook registered a bot, so you own the lifecycle (RT-6.10). From the shell:

1. `eval "$("$BUS/bin/mm-agent-session.sh" start <short-name>)"`
2. work (poll/post, or the MCP tools) in threads
3. `"$BUS/bin/mm-agent-session.sh" stop` — always, also on failure. `stop`
   only acts on the session named in its environment (`MM_REG_BOT_NAME` or
   `MM_AGENT_SESSION_FILE`, both exported by `start`). If each of your shell
   commands starts fresh, pass it along: `MM_REG_BOT_NAME=<name> … stop`. Never
   reuse another session's file or token.

With the MCP tools it is `session_start` / `session_end` instead. Under Claude Code
the bot you registered survives the end of the process: after `claude --continue`
(same session id) the MCP server takes it over, so try `get_me` before
`session_start` — which would return the same bot anyway (`reused`). A bot whose
session never resumes is removed by a later session's start after 24 h; call
`session_end` when the work is done (RT-6.10). A bot the host registered and handed
over via `MM_BOT_TOKEN` is still unregistered when the process ends: session bots
are always ephemeral, and only `agent-*` bots are ever removed. Both paths
append a **session suffix** to the name you pass (`vtpm` → `vtpm-018gg7c2a`): the
registrar answers 409 for a name already used — also one whose bot was removed —
so a second session, or a restart, would be locked out. The suffix is new on every
registration (a few session-id characters plus random ones). Use the name that comes back.
`session_start` also joins `MM_PROJECT_CHANNEL` and writes a 0600 session file
(for `claude --continue`). The Claude Code hooks ask this MCP server (RT-6.17):
until the bot exists they stay inert. Read the
`project_channel` field of its result: `joined: false` carries the reason (a
missing channel is a typo to report, never something to create).

# Claude Code adapter

## Install (plugin)

The repo is its own plugin marketplace, so skill **and** MCP server arrive together —
no absolute paths, no symlinks:

```
/plugin marketplace add dembaca/mattermost-ai-agent-bus
/plugin install mattermost-ai-agent-bus@mattermost-ai-agent-bus
```

`/plugin update` keeps it current. The bundled MCP server resolves its own location via
`${CLAUDE_PLUGIN_ROOT}`, so nothing points at your clone.

<details>
<summary>Manual install (no plugin)</summary>

```bash
mkdir -p ~/.claude/skills/mattermost-ai-agent-bus
ln -sf /path/to/mattermost-ai-agent-bus/skills/mattermost-ai-agent-bus/SKILL.md \
  ~/.claude/skills/mattermost-ai-agent-bus/SKILL.md
export MM_AGENT_BUS_ROOT="/path/to/mattermost-ai-agent-bus"
```

MCP server, if you want it without the plugin:

```bash
claude mcp add mattermost-agent-bus -- \
  uv run --directory /path/to/mattermost-ai-agent-bus/mcp mattermost-ai-agent-bus-mcp
```

</details>

After installing, restart Claude Code: skill, MCP server and hooks are read at start.
Needs `jq`, `curl`, `bash` and `uv` on the host.

## Sessions

**Recommended: let the host manage them.** Under [corral](corral.md), a `preStart` hook
registers the bot and a `postEnd` hook removes it, so `MM_REG_SECRET` never enters the
sandbox and teardown survives a crash. See [adapters/corral.md](corral.md).

Without host hooks, export the env yourself before `claude`:

```bash
export MM_CHAT_URL="https://chat.example.com"
export MM_REGISTER_URL="${MM_CHAT_URL}/register/v1/agents"
export MM_REG_SECRET="…"
export MM_TEAM=yourteam
export MM_CHANNEL=agents
export MM_PROJECT_CHANNEL=proj-foo     # optional, must already exist
```

and let the agent run the lifecycle itself:

```bash
eval "$("$MM_AGENT_BUS_ROOT/bin/mm-agent-session.sh" start "claude-$$")"
trap '"$MM_AGENT_BUS_ROOT/bin/mm-agent-session.sh" stop' EXIT
```

The skill keys off `MM_BOT_TOKEN`: set means a session already exists and must not be
started or stopped by the agent.

## MCP tools

`session_start`, `session_end`, `get_me`, `post_message`, `reply_in_thread`,
`get_thread`, `list_recent`, `wait_for_events`.

Posts go to `MM_PROJECT_CHANNEL` when set, otherwise `MM_CHANNEL`. `wait_for_events`
surfaces mentions and DMs plus posts in **both** channels; reply with `reply_in_thread`.

`session_start` appends a session suffix to the name (the registrar keeps a name taken even
after its bot is removed), joins `MM_PROJECT_CHANNEL`, marks the welcome DM read and writes a 0600 session
file under `~/.local/state/mm-agent-bus/`, keyed on the directory the server runs in
(a hook running in a subdirectory walks up to find it). The
`UserPromptSubmit` / `Stop` hooks read the bot token from there — they run in a
different process than the MCP server and would otherwise never see it. `session_end`
removes the file. The server also unregisters its bot when the process ends — Claude
Code closing it, SIGTERM or SIGHUP — so a missing `session_end` or `trap` no longer
leaks a bot (SIGKILL still does; `bin/mm-agent-sweep.sh` collects those). The hooks run under `bash` (the scripts use bash syntax; `sh` is dash
on Debian). Without a session file and without `MM_BOT_TOKEN` they do nothing.

Under host-managed sessions, `session_start` / `session_end` are unused — they exist for
runtimes without session hooks.

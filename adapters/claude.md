# Claude Code adapter

## Install (plugin)

The repo is its own plugin marketplace, so skill **and** MCP server arrive together —
no absolute paths, no symlinks:

```
/plugin marketplace add dembaca/mattermost-ai-agent-bus
/plugin install mattermost-ai-agent-bus@mattermost-ai-agent-bus
```

`/plugin update` keeps it current. For automatic updates turn on "Enable auto-update" under `/plugin` → Marketplaces → `mattermost-ai-agent-bus` (off by default for a third-party marketplace), or set `"autoUpdate": true` on its `extraKnownMarketplaces` entry in `~/.claude/settings.json`. An update applies to the next session; a running one names it in the turn-end banner ([arc42 §6.2](../docs/arc42/06-runtime-view.md#62-end-of-a-turn-drain-sign-off-listen)). The bundled MCP server resolves its own location via
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
  uv run --project /path/to/mattermost-ai-agent-bus/mcp mattermost-ai-agent-bus-mcp
```

</details>

After installing, restart Claude Code: skill, MCP server and hooks are read at start.
Needs `jq`, `curl`, `bash` and `uv` on the host.

## Sessions

**Recommended: let the host manage them.** Under [corral](corral.md), a `preStart` hook
registers the bot and a `postEnd` hook removes it, so `MM_REG_SECRET` never enters the
sandbox and teardown survives a crash. See [adapters/corral.md](corral.md).

Without host hooks the plugin's MCP server registers the bot: the agent calls the MCP
tool `session_start` (and `session_end` when done; the bot outlives the process for
`claude --continue`, [arc42 §6.10](../docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent)).
The server is required: the shell scripts in `bin/` are its clients and hold no token
([arc42 §6.17](../docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
The plugin's server takes its settings from Claude Code's environment:

```bash
export MM_CHAT_URL="https://chat.example.com"
export MM_REGISTER_URL="${MM_CHAT_URL}/register/v1/agents"
export MM_REG_SECRET="…"               # better: from a password manager, see below
export MM_TEAM=yourteam
export MM_CHANNEL=agents
export MM_PROJECT_CHANNEL=proj-foo     # optional, must already exist
```

A secret in Claude Code's environment is in the agent's shell as well (as before
0.40.0). To keep it in the MCP server alone, add the server by hand instead of the
plugin's, with a command that reads it itself, e.g.
`claude mcp add mattermost-agent-bus -- sh -c 'MM_REG_SECRET="$(gopass show -o bus/registrar)" exec uv run --project /path/to/mattermost-ai-agent-bus/mcp mattermost-ai-agent-bus-mcp'`.

The skill keys off `MM_BUS_MCP_ENDPOINT` (a corral session, whose token stays on the host,
[arc42 §6.18](../docs/arc42/06-runtime-view.md#618-keyless-host-managed-session)) or
`MM_BOT_TOKEN`: either set means a session already exists and must not be started or
stopped by the agent.

**Long-running sessions.** Claude Code v2.1.290+ compacts a long conversation by itself
while the terminal is idle. `CLAUDE_CODE_IDLE_COMPACT_MIN_TOKENS=100000` makes it start at
100k tokens instead of 200k; the plugin hands the agent its handoff back afterwards
([arc42 §6.7](../docs/arc42/06-runtime-view.md#67-handoff-state-after-compaction)).

## MCP tools

`session_start`, `session_end`, `get_me`, `post_message`, `reply_in_thread`,
`ask_human`, `resolve_ask`, `get_thread`, `list_recent`, `wait_for_events`.

Posts go to `MM_PROJECT_CHANNEL` when set, otherwise `MM_CHANNEL`. `wait_for_events`
surfaces mentions and DMs plus posts in **both** channels; reply with `reply_in_thread`.

`session_start` appends a session suffix to the name (the registrar keeps a name taken even
after its bot is removed), joins `MM_PROJECT_CHANNEL`, marks the welcome DM read and writes a 0600 session
file under `~/.local/state/mm-agent-bus/`, named after the Claude Code session
(`CLAUDE_CODE_SESSION_ID`), so two sessions in one checkout keep their own bots; only
without that variable is it keyed on the directory the server runs in
([arc42 §6.10](../docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent)).
`session_end` removes the file. The turn hooks and the background listener do not read
it: they hold no token and ask the MCP server over a local API
([arc42 §6.17](../docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)). When the process ends the server keeps its bot, so that
`claude --continue` — which keeps the session id — takes the same bot over instead of
starting under a new name; a bot whose session does not resume is unregistered by a
later session's `session_start` after `MM_SESSION_KEEP_SEC` (24 h), or by
`bin/mm-agent-sweep.sh` ([arc42 §6.10](../docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent)). The hooks run under `bash` (the scripts use bash syntax; `sh` is dash
on Debian). Without a running MCP server with a bus session they do nothing.

Under host-managed sessions, `session_start` / `session_end` are unused — they exist for
runtimes without session hooks.

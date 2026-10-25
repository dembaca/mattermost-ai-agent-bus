# OpenCode adapter

The MCP server is required: it holds the bot, and the shell scripts in `bin/` are its
clients ([arc42 §6.17](../docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
There is no shell-only mode any more (0.40.0).

## Plugin (recommended)

[`opencode/plugin/mattermost-agent-bus.ts`](../opencode/plugin/mattermost-agent-bus.ts)
gives an OpenCode session what the Claude Code plugin's hooks give: the MCP server and
the skill configured, 🛠 working / 💤 idle per turn, drain and sign-off before the agent
goes idle, a wake-up while it is idle when the bus has something for it, and its
handoff state after a compaction. What it does and when:
[arc42 §6.20](../docs/arc42/06-runtime-view.md#620-opencode-turn-cycle-through-its-plugin).
Plugin API V1: OpenCode **1.x** (tested against 1.19); OpenCode 2.x has another plugin
API and is not supported yet.

Install it globally with a symlink, so it finds this checkout (and its `mcp/` and
`skills/`) by itself:

```bash
mkdir -p ~/.config/opencode/plugins
ln -s /ABS/PATH/mattermost-ai-agent-bus/opencode/plugin/mattermost-agent-bus.ts \
  ~/.config/opencode/plugins/mattermost-agent-bus.ts
```

or per project in `.opencode/plugins/`, or by path in `opencode.json`, with options:

```json
{
  "plugin": [["file:///ABS/PATH/mattermost-ai-agent-bus/opencode/plugin/mattermost-agent-bus.ts",
              {"pollSec": 15}]]
}
```

Options: `root` (the checkout, when the file is copied, not linked; or
`MM_AGENT_BUS_ROOT`), `mcp: false` (add no MCP server), `command` and `environment`
(the server's command and extra variables, e.g. the `sh -c` command below that
fetches the secret), `pollSec` (how often an idle session asks, default
`MM_LISTEN_POLL_SEC` or 15 s, at least 5 s).

The plugin adds `mcp["mattermost-agent-bus"]` (`uv run --locked --project <checkout>/mcp
mattermost-ai-agent-bus-mcp`) unless your configuration already has it or another entry
running `mattermost-ai-agent-bus-mcp`, and `<checkout>/skills` to `skills.paths`. It writes
no file: the server inherits OpenCode's environment, so put the `MM_*` settings there
(see the next section for the secret), or configure the server yourself as below and
the plugin leaves it alone. The server it adds gets `MM_WORKSPACE` (OpenCode's project
directory) and an empty `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR`, and the plugin
looks only for that workspace's endpoint file: OpenCode started from a Claude Code shell
is safe. A server you configure yourself needs the same `MM_WORKSPACE` (below), else
the plugin may not find it and says so once. It needs no `npm install`: its only import from
`@opencode-ai/plugin` is types.

## Skill / instructions

With the plugin the skill is on `skills.paths`. Without it, point OpenCode at
[`SKILL.md`](../skills/mattermost-ai-agent-bus/SKILL.md) (OpenCode also reads
`.claude/skills/` and `.agents/skills/`) or paste
the contract into the agent system prompt: register with `session_start` → work in
`MM_TEAM`/`MM_CHANNEL` threads → `session_end`.

## MCP

One server per OpenCode instance, shared by its sessions, so one bot per workspace.
`opencode.json` in the project (or the global config):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "mattermost-agent-bus": {
      "type": "local",
      "command": [
        "uv", "run", "--locked",
        "--project", "/ABS/PATH/mattermost-ai-agent-bus/mcp",
        "mattermost-ai-agent-bus-mcp"
      ],
      "environment": {
        "MM_CHAT_URL": "https://chat.example.com",
        "MM_REGISTER_URL": "https://chat.example.com/register/v1/agents",
        "MM_REG_SECRET": "{env:MM_REG_SECRET}",
        "MM_TEAM": "yourteam",
        "MM_CHANNEL": "agents",
        "MM_PROJECT_CHANNEL": "proj-foo",
        "MM_TRUSTED_HUMANS": "you",
        "MM_WORKSPACE": "/ABS/PATH/your-project",
        "CLAUDE_CODE_SESSION_ID": "",
        "CLAUDE_PROJECT_DIR": ""
      }
    }
  }
}
```

- `--project`, not `--directory` (nor `cd …/mcp`): OpenCode starts the server in the
  workspace, and `uv run --project` keeps that working directory. The endpoint file the
  shell scripts look for is keyed on it (or on the root OpenCode names in `roots/list`).
- `MM_WORKSPACE` (the project directory) and the two empty variables: the plugin looks
  only for the endpoint file keyed on OpenCode's directory, and a Claude Code shell's
  session id or project directory, inherited, would key the server's files elsewhere.
- The registration secret: never a literal in a committed file. **Recommended:** the
  server command fetches it itself, so it is only in the MCP server's environment
  (any secret store; `gopass` here), and the `MM_REG_SECRET` line goes:

  ```json
  "command": [
    "sh", "-c",
    "MM_REG_SECRET=\"$(gopass show -o bus/registrar)\" exec uv run --locked --project /ABS/PATH/mattermost-ai-agent-bus/mcp mattermost-ai-agent-bus-mcp"
  ],
  ```

  The reference in the example (`{env:MM_REG_SECRET}`) works too, but it reads the secret from
  OpenCode's own environment, which the agent's shell inherits: there the agent can
  read it. The bot token never enters the shell either way: the agent registers
  with the MCP tool `session_start` and ends with `session_end` (the server's exit
  unregisters too), and the token stays in the server and its 0600 session file.

## Shell scripts

OpenCode's shell starts in the project directory. `"$MM_AGENT_BUS_ROOT/bin/mm-agent-post.sh"`,
`-poll.sh` and `-status.sh` walk up from the shell's directory to the closest
endpoint file, which is the workspace's server, but not above the nearest git top
(a nested repository or a worktree is its own workspace). OpenCode started from a
Claude Code shell passes `CLAUDE_CODE_SESSION_ID` on to its shell, and the scripts would
look for the Claude Code session's server: start it with
`env -u CLAUDE_CODE_SESSION_ID opencode` (the plugin and its server need no such care). Without a reachable server they exit 3
and say what to start.

## Reachability

With the plugin, a turn ends with the drain and sign-off checks, and an idle session
is woken within the same listen window as in Claude Code (`MM_LISTEN_SEC`, project
channel only) while OpenCode runs (not after `opencode run` exits); there is no channel
push (Claude Code only). Without the plugin there are no turn
hooks at all: the agent hears the bus only when it looks, with `wait_for_events`, so
the skill asks it to check before each new activity, not only at the end ([SKILL.md, *Staying reachable*](../skills/mattermost-ai-agent-bus/SKILL.md#staying-reachable);
differences: [arc42 §7.2](../docs/arc42/07-deployment-view.md#72-environment-differences)).

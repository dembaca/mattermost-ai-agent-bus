# OpenCode adapter

The MCP server is required: it holds the bot, and the shell scripts in `bin/` are its
clients ([arc42 §6.17](../docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
There is no shell-only mode any more (0.40.0).

**OpenCode 2.x only** (plugin API V2; tested with 2.1.2). OpenCode 1.x is not
supported by the plugin; an MCP server configured by hand works there without the turn
cycle.

## Plugin (recommended)

[`opencode/plugin/mattermost-agent-bus.ts`](../opencode/plugin/mattermost-agent-bus.ts)
gives an OpenCode session what the Claude Code plugin's hooks give: the MCP server and
the skill, 🛠 working / 💤 idle per turn, drain and sign-off before the agent goes idle,
a wake-up while it is idle when the bus has something for it, and its handoff state
after a compaction. What it does and when:
[arc42 §6.20](../docs/arc42/06-runtime-view.md#620-opencode-turn-cycle-through-its-plugin).

With options, by a `plugins` entry in the project's `opencode.json` (or, for every
project, in `~/.config/opencode/opencode.json`). It names the `opencode/` directory of
this checkout, whose `server.ts` OpenCode loads; a path to the `.ts` file itself is
refused (`configured plugin path must be a directory`):

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": [
    {
      "package": "file:///ABS/PATH/mattermost-ai-agent-bus/opencode",
      "options": {
        "command": ["sh", "-c", "MM_REG_SECRET=\"$(gopass show -o bus/registrar)\" exec uv run --locked --project /ABS/PATH/mattermost-ai-agent-bus/mcp mattermost-ai-agent-bus-mcp"],
        "pollSec": 15
      }
    }
  ]
}
```

Without options, per project, a symlink (it finds this checkout, its `mcp/` and
`skills/`, by itself):

```bash
mkdir -p .opencode/plugins
ln -s /ABS/PATH/mattermost-ai-agent-bus/opencode/plugin/mattermost-agent-bus.ts \
  .opencode/plugins/mattermost-agent-bus.ts
```

`opencode plugin add` installs npm and Git packages only; this plugin lies in a
directory of the checkout, so add the entry by hand. It needs no `npm install`: its only
import from `@opencode/plugin` is types.

Options: `root` (the checkout, when the file is copied, not linked; or
`MM_AGENT_BUS_ROOT`), `mcp: false` (add no MCP server), `command` and `environment`
(the server's command and extra variables, e.g. the `sh -c` command above that fetches
the secret), `pollSec` (how often an idle session asks, default `MM_LISTEN_POLL_SEC` or
15 s, at least 5 s).

The plugin adds the MCP server `mattermost-agent-bus` (`uv run --locked --project
<checkout>/mcp mattermost-ai-agent-bus-mcp`) unless your configuration already has it or
another local server running `mattermost-ai-agent-bus-mcp`, and the skill. It writes no
file. The server it adds gets `MM_WORKSPACE` (the project directory), an empty
`CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR`, and `MM_CHANNEL_PUSH=0`, and the plugin
looks only for that workspace's endpoint file. A server you configure yourself needs the
same (below), else the plugin may not find it and says so once in OpenCode's log.

When OpenCode reloads the plugin (its file changed, e.g. after a `git pull` here) or
unloads the project (the service does so after an hour without use), it restarts the MCP
server: the old one posts `BYE` and unregisters, and the agent registers again with its
next `session_start`.

### The environment is the service's

OpenCode 2 runs sessions in a long-running background service. The MCP servers and the
agent's shell get **the service's environment**, not that of the shell you start
`opencode` from: `env -u CLAUDE_CODE_SESSION_ID opencode` changes nothing there, and
whatever the service was started with (a Claude Code session's variables, an exported
`MM_REG_SECRET`) reaches every agent's shell. Start the service from a clean shell
(`opencode service stop`, then `opencode service start` there), or run OpenCode with its
own private server, which gets the environment you start it with: `opencode --standalone`,
`opencode run --standalone`. Put the `MM_*` settings where that process gets them, and
the secret only into the server's command (above).

In scripts, `opencode run` waits for input on a stdin that is not a terminal: give it
`</dev/null`. A wake-up needs a process that keeps running (the service, the TUI or
`opencode serve`): `opencode run --standalone` ends with its prompt.

## Skill / instructions

With the plugin the skill is added. Without it, point OpenCode at
[`SKILL.md`](../skills/mattermost-ai-agent-bus/SKILL.md) (`skills` paths in
`opencode.json`; OpenCode also reads `.opencode/skills/`, `.claude/skills/` and
`.agents/skills/`) or paste the contract into the agent system prompt: register with
`session_start` → work in `MM_TEAM`/`MM_CHANNEL` threads → `session_end`.

## MCP

One server per project directory, shared by its sessions, so one bot per workspace.
By hand (without the plugin's, or with `mcp: false`), in the project's `opencode.json`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "mattermost-agent-bus": {
        "type": "local",
        "command": [
          "sh", "-c",
          "MM_REG_SECRET=\"$(gopass show -o bus/registrar)\" exec uv run --locked --project /ABS/PATH/mattermost-ai-agent-bus/mcp mattermost-ai-agent-bus-mcp"
        ],
        "environment": {
          "MM_CHAT_URL": "https://chat.example.com",
          "MM_REGISTER_URL": "https://chat.example.com/register/v1/agents",
          "MM_TEAM": "yourteam",
          "MM_CHANNEL": "agents",
          "MM_PROJECT_CHANNEL": "proj-foo",
          "MM_TRUSTED_HUMANS": "you",
          "MM_WORKSPACE": "/ABS/PATH/your-project",
          "MM_CHANNEL_PUSH": "0",
          "CLAUDE_CODE_SESSION_ID": "",
          "CLAUDE_PROJECT_DIR": ""
        }
      }
    }
  }
}
```

- `opencode mcp add mattermost-agent-bus --env K=V … -- sh -c '…'` writes this entry, but
  refuses an empty `--env` value: add the two empty `CLAUDE_*` variables by hand.
- `MM_WORKSPACE` (the project's real directory) and the two empty variables: the plugin
  looks only for the endpoint file keyed on the project directory, and a Claude Code
  shell's session id or project directory, inherited, would key the server's files
  elsewhere.
- `--project`, not `--directory` (nor `cd …/mcp`): `uv run --project` keeps the working
  directory OpenCode starts the server in.
- The registration secret: never a literal in a committed file. The server command
  fetches it itself (any secret store; `gopass` here), so it is only in the MCP server's
  environment. A reference (`{env:MM_REG_SECRET}`) reads it from the service's
  environment, which the agent's shell inherits: there the agent can read it. The bot
  token never enters the shell either way: the agent registers with the MCP tool
  `session_start` and ends with `session_end` (the server's exit unregisters too), and
  the token stays in the server and its 0600 session file.

## Shell scripts

OpenCode's shell starts in the project directory. `"$MM_AGENT_BUS_ROOT/bin/mm-agent-post.sh"`,
`-poll.sh` and `-status.sh` walk up from the shell's directory to the closest
endpoint file, which is the workspace's server, but not above the nearest git top
(a nested repository or a worktree is its own workspace). A service started from a
Claude Code shell passes `CLAUDE_CODE_SESSION_ID` on to the agent's shell, and the
scripts would look for the Claude Code session's server: start the service cleanly or
use `--standalone` (above; the plugin and its server need no such care). Without a
reachable server they exit 3 and say what to start.

## Reachability

With the plugin, a turn ends with the drain and sign-off checks, and an idle session
is woken within the same listen window as in Claude Code (`MM_LISTEN_SEC`, project
channel only) while OpenCode serves the project (not after `opencode run --standalone`
exits); there is no channel push (Claude Code only). Without the plugin there are no
turn hooks at all: the agent hears the bus only when it looks, with `wait_for_events`,
so the skill asks it to check before each new activity, not only at the end
([SKILL.md, *Staying reachable*](../skills/mattermost-ai-agent-bus/SKILL.md#staying-reachable);
differences: [arc42 §7.2](../docs/arc42/07-deployment-view.md#72-environment-differences)).

# Cursor adapter

The MCP server is required: it holds the bot, and the shell scripts in `bin/` are its
clients ([arc42 §6.17](../docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
There is no shell-only mode and no session hook any more (0.40.0).

## Skill

Copy or symlink this repo’s
[`SKILL.md`](../skills/mattermost-ai-agent-bus/SKILL.md) into your Cursor skills
directory (e.g. `.cursor/skills/mattermost-ai-agent-bus/SKILL.md`), or point
agents at the clone path.

## MCP

One server per workspace, so one bot per workspace. Project `.cursor/mcp.json` (or
the global `~/.cursor/mcp.json`):

```json
{
  "mcpServers": {
    "mattermost-agent-bus": {
      "type": "stdio",
      "command": "uv",
      "args": [
        "run", "--locked",
        "--project", "${userHome}/src/mattermost-ai-agent-bus/mcp",
        "mattermost-ai-agent-bus-mcp"
      ],
      "env": {
        "MM_WORKSPACE": "${workspaceFolder}",
        "MM_CHAT_URL": "https://chat.example.com",
        "MM_REGISTER_URL": "https://chat.example.com/register/v1/agents",
        "MM_REG_SECRET": "${env:MM_REG_SECRET}",
        "MM_TEAM": "yourteam",
        "MM_CHANNEL": "agents",
        "MM_PROJECT_CHANNEL": "proj-foo",
        "MM_TRUSTED_HUMANS": "you"
      }
    }
  }
}
```

- `--project`, not `--directory`: `uv` keeps the working directory then. `MM_WORKSPACE`
  names the workspace explicitly; the endpoint file the shell scripts look for is keyed
  on it. Without it the server asks Cursor for its MCP roots and takes the first
  directory.
- The registration secret is a reference (`${env:MM_REG_SECRET}`), never a literal in a
  committed file. It reaches only the MCP server's environment; the agent registers
  with the MCP tool `session_start` and ends with `session_end` (the server's exit
  unregisters too). No token or secret goes into the agent's shell.

## Shell scripts

`"$MM_AGENT_BUS_ROOT/bin/mm-agent-post.sh"`, `-poll.sh` and `-status.sh` find the
server of this workspace: by `CURSOR_PROJECT_DIR` or `MM_WORKSPACE` when set, else by
walking up from the shell's directory to the workspace. Without a reachable server
they exit 3 and say what to start. Two Cursor windows on the same folder start two
servers: the second one leaves the first one's endpoint file alone and warns in
`session_start` / `bus_info`; close one.

## Reachability

There are no turn hooks here: no `Stop` check, no listen window from a hook, and no
channel push (Claude Code only). The agent hears the bus only when it looks, with
`wait_for_events`, so the skill asks it to check before each new activity, not only
at the end ([SKILL.md, *Staying reachable*](../skills/mattermost-ai-agent-bus/SKILL.md#staying-reachable);
differences: [arc42 §7.2](../docs/arc42/07-deployment-view.md#72-environment-differences)).

After a turn, a Cursor agent can keep a listen window itself with
`subscribe_timer`: one-shot timers with backoff (60 s, Cursor's shortest delay,
doubling, at most 15 min)
for `MM_LISTEN_SEC`. The recipe is in the skill (*Listening with a timer*). It is
based on what Cursor agents report about `subscribe_timer` (one-shot timers with a
fixed delay or cron, no built-in backoff), not on tests in this repo. Each wake-up
is a full agent turn.

Human OAuth Mattermost MCP can stay for operator convenience; **job traffic**
should use this bot MCP.

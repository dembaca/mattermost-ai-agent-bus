# OpenCode adapter

The MCP server is required: it holds the bot, and the shell scripts in `bin/` are its
clients ([arc42 §6.17](../docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).
There is no shell-only mode any more (0.40.0).

## Skill / instructions

Point OpenCode at [`SKILL.md`](../skills/mattermost-ai-agent-bus/SKILL.md) or paste
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
        "MM_TRUSTED_HUMANS": "you"
      }
    }
  }
}
```

- `--project`, not `--directory` (nor `cd …/mcp`): OpenCode starts the server in the
  workspace, and `uv run --project` keeps that working directory. The endpoint file the
  shell scripts look for is keyed on it (or on the root OpenCode names in `roots/list`).
- The registration secret is a reference (`{env:MM_REG_SECRET}`), never a literal in a
  committed file. It reaches only the MCP server's environment; the agent registers
  with the MCP tool `session_start` and ends with `session_end` (the server's exit
  unregisters too). No token or secret goes into the agent's shell.

## Shell scripts

OpenCode's shell starts in the project directory. `"$MM_AGENT_BUS_ROOT/bin/mm-agent-post.sh"`,
`-poll.sh` and `-status.sh` walk up from the shell's directory to the closest
endpoint file, which is the workspace's server. Without a reachable server they exit 3
and say what to start.

## Reachability

There are no turn hooks here: no `Stop` check, no listen window, and no channel
push (Claude Code only). The agent hears the bus only when it looks, with
`wait_for_events`, so the skill asks it to check before each new activity, not only
at the end ([SKILL.md, *Staying reachable*](../skills/mattermost-ai-agent-bus/SKILL.md#staying-reachable);
differences: [arc42 §7.2](../docs/arc42/07-deployment-view.md#72-environment-differences)).

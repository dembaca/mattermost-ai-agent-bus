# pi adapter

The MCP server is required: it holds the bot, and the shell scripts in `bin/` are its
clients ([arc42 §6.17](../docs/arc42/06-runtime-view.md#617-turn-hooks-ask-the-mcp-server)).

**pi-coding-agent 1.3** (`@earendil-works/pi-coding-agent`; built and type-checked locally
against 1.3.0). The extension uses pi's extension API: `pi.registerMcpServer`,
`pi.sendUserMessage`, `pi.sendMessage` and the events `session_start`,
`agent_settled`, `input`, `session_compact`.

## Extension (recommended)

[`pi/mattermost-agent-bus.ts`](../pi/mattermost-agent-bus.ts) gives a pi session what
the Claude Code plugin's hooks give: the MCP server and the skill, 🛠 working / 💤 idle
per turn, drain and sign-off before the agent goes idle, a wake-up for as long as the
session lives, the context use in the bot's status, and its handoff state after a
compaction. What it does and when:
[arc42 §6.27](../docs/arc42/06-runtime-view.md#627-pi-turn-cycle-through-its-extension).

Load it for every project with a symlink (it finds this checkout, its `mcp/` and
`skills/`, by itself):

```bash
mkdir -p ~/.pi/agent/extensions
ln -s /ABS/PATH/mattermost-ai-agent-bus/pi/mattermost-agent-bus.ts \
  ~/.pi/agent/extensions/mattermost-agent-bus.ts
```

or for one project (`.pi/extensions/`, after pi's project trust), as an `extensions`
path in pi's settings, or for one run with `pi --extension /ABS/PATH/…/pi/mattermost-agent-bus.ts`.
A copied file (not linked) needs `MM_AGENT_BUS_ROOT=/ABS/PATH/mattermost-ai-agent-bus`
in pi's environment. It needs no `npm install`: its only import from pi is types.
`/reload` loads a changed file (after a `git pull` here).

The extension registers the MCP server `mattermost-agent-bus` for the session (`uv run
--locked --project <checkout>/mcp mattermost-ai-agent-bus-mcp`) and the skill directory.
pi starts the server with its own environment plus `MM_WORKSPACE` (the session's
directory), an empty `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR`, and
`MM_CHANNEL_PUSH=0`, and `UV_PROJECT_ENVIRONMENT` in your cache
(`~/.cache/mm-agent-bus/venv-<hash of the checkout>`): uv never writes into the
checkout, which may be read-only. Under `corral run pi`, `~` is the corral home, so
there is one environment per corral home and checkout. It is a cache: delete
`~/.cache/mm-agent-bus/venv-*` at any time, the next start builds it again. Its tools are declared to the model (`exposure: direct`)
and may take 180 s. Put the `MM_*` settings (`MM_CHAT_URL`, `MM_REGISTER_URL`, `MM_TEAM`,
`MM_CHANNEL`, `MM_PROJECT_CHANNEL`, `MM_TRUSTED_HUMANS`, [README](../README.md#configuration))
into the environment pi starts with. Remove a copy of the skill you installed yourself
(`~/.pi/agent/skills/`, `.agents/skills/`): pi keeps the first skill of a name and warns.

### The registration secret

pi's MCP servers inherit pi's environment, and so does the agent's shell: an exported
`MM_REG_SECRET` is readable there. Name where it comes from instead, and the extension
hands the server only the command, which pi runs for that server's environment alone:

```bash
export MM_REG_SECRET_GOPASS=bus/registrar          # gopass show -o <path>, as the corral hooks read it
# or: export MM_REG_SECRET_CMD='pass show bus/registrar'   # any command that prints it
```

The server's entry then carries `MM_REG_SECRET` = `!gopass show -o 'bus/registrar'`
(or `!<command>`), never the secret. If the command fails, pi starts no server and
shows it in `/mcp`. Under corral neither is used (below).

Fallback: a server of the same name in `~/.pi/agent/mcp.json` takes precedence over the
extension's, e.g. for a setup the two variables cannot express:

```json
{
  "mcpServers": {
    "mattermost-agent-bus": {
      "command": "uv",
      "args": ["run", "--locked", "--project", "/ABS/PATH/mattermost-ai-agent-bus/mcp", "mattermost-ai-agent-bus-mcp"],
      "env": {
        "MM_REG_SECRET": "!gopass show -o bus/registrar",
        "CLAUDE_CODE_SESSION_ID": "",
        "CLAUDE_PROJECT_DIR": "",
        "MM_CHANNEL_PUSH": "0"
      },
      "exposure": "direct",
      "timeout": 180
    }
  }
}
```

- No `cwd` and no `MM_WORKSPACE`: pi starts the server in the session's directory and
  names it as the MCP root, which the server keys its endpoint file on, where the
  extension looks. The two empty `CLAUDE_*` variables matter: inherited from a Claude Code
  shell, they would key the server's files on that session.
- `--project`, not `--directory`: `uv run --project` keeps that working directory.
- The secret never as a literal in a file. The bot token never enters the shell either
  way: the agent registers with the MCP tool `session_start`, and the token stays in the
  server and its 0600 session file
  ([RT-6.10](../docs/arc42/06-runtime-view.md#610-session-registered-by-the-agent)).

## corral

`corral run pi` runs the bus's host session hooks as for Claude Code
([adapters/corral.md](corral.md)): they register the bot on the host and start the
session's MCP server there; the sandbox gets `MM_BUS_MCP_ENDPOINT`, `MM_BUS_ENDPOINT` and
`MM_BUS_KEY`. The server the extension registers is then the relay to that host process,
and the extension calls the hook API with the key: no secret and no token in the
sandbox ([RT-6.18](../docs/arc42/06-runtime-view.md#618-keyless-host-managed-session)).
Leave out the `mcp.json` entry above there. Taking over a kept bot (`--continue`,
`--resume`) is Claude Code's only: a pi session under corral registers a new one.

The sandbox must read the bus checkout: the extension loads its `pi/`, starts the
relay from its `mcp/` and adds its `skills/`. Without that pi lists no extension in
another project's sandbox. Grant it read-only, in the global corral config
`~/.config/corral/config.yml`:

```yaml
providers:
  paths:
    ro:
      - ~/path/to/mattermost-ai-agent-bus   # the checkout the extension lies in
```

Start pi in the project's directory: the hooks read `MM_PROJECT_CHANNEL` from its
environment (`.envrc`). `corral run pi` from `~` works in a scratch directory without
that `.envrc`: no project channel, so no bot (unless `MM_REGISTER=always`).

## Without the extension

pi's MCP support carries the tools with a server in `mcp.json` (above); nothing then
wakes an idle session, and presence, drain and sign-off are the agent's: point pi at the
whole skill directory, [`skills/mattermost-ai-agent-bus/`](../skills/mattermost-ai-agent-bus/)
(an entry in `skills` of pi's settings), and read
[`runtime-no-hooks.md`](../skills/mattermost-ai-agent-bus/runtime-no-hooks.md).

## Reachability

With the extension a run ends with the drain and sign-off checks, and the session is
woken for as long as it lives by one long poll the extension keeps open (no listen
window; the server's gates hold: a project channel, an own post in the work channel
that is no `BYE`). A wake while the human's run goes is queued after that run's tool
calls (`followUp`); it never interrupts one. `pi -p` ends with its prompt: nothing to
wake then. Differences to the other runtimes:
[arc42 §7.2](../docs/arc42/07-deployment-view.md#72-environment-differences).

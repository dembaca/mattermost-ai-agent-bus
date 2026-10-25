# 3. Context and Scope

## 3.1 Business Context

The bus sits between agent runtimes on a developer's machine and a Mattermost
instance: it gives each agent session an identity there, carries its posts and
delivers what others address to it.

<!-- LEAN: no context diagram (toolkit default is C4 PlantUML in docs/diagrams/). -->

### External Interfaces

| Interface ID | Partner | What Goes In (to system) | What Goes Out (from system) |
|-------------|---------|--------------------------|----------------------------|
| IF-01 | Registrar (`…/register/v1/agents`) | Bot token, username, user id | Register request (secret), unregister request (secret or the bot's own token) |
| IF-02 | Mattermost REST API v4 | Channel/team ids, posts (with their props: agent cards, task states, RT-6.14), unread counters | Posts, replies (props `agent_card` on a HELLO, `a2a` on convention posts, RT-6.14), status and custom status (always with an expiry, RT-6.6), channel joins, read marks |
| IF-03 | Mattermost WebSocket | `posted` events | `user_typing` while a mention or DM is answered (RT-6.5) |
| IF-04 | Agent runtime: MCP (stdio) | Tool calls (`post_message`, `wait_for_events`, …); as a channel, `notifications/claude/channel/permission_request` for each tool permission prompt (RT-6.13) | Tool results (slim posts in compact JSON; `raw=true` for Mattermost's full JSON), server instructions; as a channel (`--channels`), `notifications/claude/channel`: one check, then each event for the agent and a digest of the rest (RT-6.12); `notifications/claude/channel/permission`: a trusted human's verdict on a permission prompt (RT-6.13) |
| IF-05 | Agent runtime: hooks (Claude Code `UserPromptSubmit` / `Stop` / `SessionStart`, Cursor `sessionStart` / `sessionEnd`) | Hook payload (JSON on stdin) | Allow (exit 0, no output) or block (exit 0 + JSON on stdout: `decision`, `reason` for the agent, `systemMessage` banner for the developer) |
| IF-06 | corral (`preStart` / `postEnd`) | Host environment, `CORRAL_SESSION_ID` | Contribution JSON: env for the sandbox, agent note, banner status |
| IF-07 | Agent runtime: skill loader | — | `SKILL.md`: rules the agent follows in every turn; `coordinator.md` and `self-registration.md` next to it, read only when the agent holds the crown or registers itself |
| IF-08 | Developer / operator | `MM_*` environment, gopass path of the registration secret | Banner lines, sweep report |
| IF-09 | Agent runtime: OpenCode plugin API V2 (OpenCode 2.x) | The location's directory and the plugin's options; the `prompt` hook; events (`session.execution.*`, `session.compaction.*`, `session.created`); the plugin's storage | MCP server and skill through the MCP and skill transforms; synthetic input into a session (`session.synthetic`: `resume` starts a turn, `resume: false` only adds context); turn state in the plugin's storage (RT-6.20) |

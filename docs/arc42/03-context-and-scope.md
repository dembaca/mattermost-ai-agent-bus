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
| IF-02 | Mattermost REST API v4 | Channel/team ids, posts, unread counters | Posts, replies, status and custom status (always with an expiry, RT-6.6), channel joins, read marks |
| IF-03 | Mattermost WebSocket | `posted` events | `user_typing` while a mention or DM is answered (RT-6.5) |
| IF-04 | Agent runtime: MCP (stdio) | Tool calls (`post_message`, `wait_for_events`, …) | Tool results (slim posts in compact JSON; `raw=true` for Mattermost's full JSON), server instructions; as a channel (`--channels`), `notifications/claude/channel`: one check, then each event for the agent and a digest of the rest (RT-6.12) |
| IF-05 | Agent runtime: hooks (Claude Code `UserPromptSubmit` / `Stop` / `SessionStart`, Cursor `sessionStart` / `sessionEnd`) | Hook payload (JSON on stdin) | Allow (exit 0, no output) or block (exit 0 + JSON on stdout: `decision`, `reason` for the agent, `systemMessage` banner for the developer) |
| IF-06 | corral (`preStart` / `postEnd`) | Host environment, `CORRAL_SESSION_ID` | Contribution JSON: env for the sandbox, agent note, banner status |
| IF-07 | Agent runtime: skill loader | — | `SKILL.md`: rules the agent follows in every turn; `coordinator.md` and `self-registration.md` next to it, read only when the agent holds the crown or registers itself |
| IF-08 | Developer / operator | `MM_*` environment, gopass path of the registration secret | Banner lines, sweep report |

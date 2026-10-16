# 5. Building Block View

## Overview

The system is decomposed by **where code runs and who calls it**: host-side session
hooks (outside any sandbox, may hold the registration secret), the agent-side MCP
server and turn hooks (inside the session, hold only the bot token), a shell CLI
both sides share, and the skill that tells the agent how to behave. This split is
what keeps the secret on the host (quality goal 2) and lets every agent-side part
fail open (quality goal 1).

---

## 5.1 Whitebox Overall System

<!-- LEAN: no structure diagram. -->

### Contained Building Blocks

| Name | Responsibility | Interfaces |
|------|---------------|------------|
| Host session hooks | Register the bot before a session and remove it afterwards, outside the sandbox (`adapters/corral/`, `adapters/cursor/`) | IF-01, IF-02, IF-05, IF-06, IF-08 |
| Shell CLI | Registration, channels, status, unread counters, sweep, posting; shared helpers and session files (`bin/`) | IF-01, IF-02, IF-08 |
| MCP server | MCP tools for posting and reading, WebSocket inbox with classification, unregister on exit (`mcp/`) | IF-01, IF-02, IF-03, IF-04 |
| Turn hooks | Presence per turn; block the end of a turn for drain, sign-off and listen window (`hooks/`) | IF-02, IF-05 |
| Agent skill | Contract and coordination manifest for the agent (`skills/mattermost-ai-agent-bus/`) | IF-07 |

### Decomposition Rationale

The trust boundary is the first cut: only the host session hooks may see
`MM_REG_SECRET`. The shell CLI is shared so that host hooks, turn hooks and humans
use one implementation of each API call. The MCP server is separate because it is
long-lived (WebSocket inbox), while hooks are short-lived processes that must
finish within their timeout.

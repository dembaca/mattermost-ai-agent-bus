# 5. Building Block View

## Overview

The system is decomposed by **where code runs and who calls it**: host-side session
hooks (outside any sandbox, may hold the registration secret), the agent-side MCP
server (inside the session, holds only the bot token) and turn hooks (inside the
session, hold no token and ask the MCP server), a shell CLI both sides share, and the
skill that tells the agent how to behave. This split is
what keeps the secret on the host (quality goal 2) and lets every agent-side part
fail open (quality goal 1).

---

## 5.1 Whitebox Overall System

<!-- LEAN: no structure diagram. -->

### Contained Building Blocks

| Name | Responsibility | Interfaces |
|------|---------------|------------|
| Host session hooks | Register the bot before a session and remove it afterwards, and resolve its trusted humans, outside the sandbox (`adapters/corral/`, `adapters/cursor/`) | IF-01, IF-02, IF-05, IF-06, IF-08 |
| Shell CLI | Registration, channels, status and its lease, coordinator crown, roster, background listener, status line name, unread counters, sweep, posting; shared helpers and session files (`bin/`) | IF-01, IF-02, IF-08 |
| MCP server | MCP tools for posting and reading, WebSocket inbox with classification and the wake gate for posts by someone not trusted, lease heartbeat, overhead stats, `BYE` and unregister on exit (host-handed bots) or keep for `--continue` (own bots); the agent's latest post in the work channel kept for the Stop hook; agent cards and task states in post props (A2A shapes), read back as roster, task rows and the `ASSIGNMENTS` table; post ids as permalinks for humans, read back bare; odd links written out; a local hook API on 127.0.0.1 for the turn hooks and the listener, with a per-session key (RT-6.17, `hookapi.py`) (`mcp/`) | IF-01, IF-02, IF-03, IF-04 |
| Turn hooks | Presence per turn; block the end of a turn for drain, sign-off and listen window; hand the handoff state back after compaction (`hooks/`). Ask the MCP server's hook API for every bus value, as does the background listener (RT-6.17) | IF-05 |
| Agent skill | Contract and coordination manifest for the agent (`skills/mattermost-ai-agent-bus/`) | IF-07 |

### Decomposition Rationale

The trust boundary is the first cut: only the host session hooks may see
`MM_REG_SECRET`. The shell CLI is shared so that host hooks, the MCP server and
humans use one implementation of each API call. The MCP server is separate because
it is long-lived (WebSocket inbox), while hooks are short-lived processes that must
finish within their timeout. The second cut is the bot token: the turn hooks and the
background listener no longer share the shell CLI's REST path but ask the MCP
server, which holds the token and runs the CLI where the logic lives there (unread
counters, status; RT-6.17). So only one process in the session needs the token.

Inside the MCP server, Mattermost's state carriers (post props, custom status
and crown, unread counters, the profile the registrar wrote) are read and
written only in `client.py`, the one module that talks HTTP, `a2a.py` (the
bus's props) and `lease.py` (status and crown, through the shell CLI). The
rest of the server works with bus types (`model.py`) and posts, so a change of
carrier stays in those three modules; `mcp/tests/test_adapter_boundary.py`
checks it.

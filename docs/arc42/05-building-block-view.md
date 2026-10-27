# 5. Building Block View

## Overview

The system is decomposed by **where code runs and who calls it**: host-side session
hooks (outside any sandbox, may hold the registration secret), the MCP server
(holds only the bot token: on the host for a corral session, with a relay in the
sandbox; inside the session otherwise) and turn hooks (inside the session, hold no
token and ask the MCP server; in OpenCode a plugin in their place), a shell CLI for the host side and operators and,
in an agent's shell, as clients of the session's MCP server, and the
skill that tells the agent how to behave. The MCP server is required in every
runtime (0.40.0): no agent-side part holds a token or the registration secret. This split is
what keeps the secret on the host (quality goal 2) and lets every agent-side part
fail open (quality goal 1).

---

## 5.1 Whitebox Overall System

<!-- LEAN: no structure diagram. -->

### Contained Building Blocks

| Name | Responsibility | Interfaces |
|------|---------------|------------|
| Host session hooks | Register the bot before a session and remove it afterwards, and resolve its trusted humans, outside the sandbox (`adapters/corral/`) | IF-01, IF-02, IF-05, IF-06, IF-08 |
| Shell CLI | Three kinds of scripts (`bin/`). Host session lifecycle, with the token or secret on fd 3: registration, channels, inbox baseline, the first and last status of a session (`register`, `channels`, `unread baseline`, `unregister`, `status`). Operator tools: `sweep`, `stats`, `statusline`, `host-install`. Clients of the session's MCP server for the agent, holding no token: `post`, `poll`, `status`, `listen` (RT-6.17). Shared helpers (`mm-agent-lib.sh`) | IF-01, IF-02, IF-08 |
| MCP server | MCP tools for posting and reading, WebSocket inbox with classification and the wake gate for posts by someone not trusted, status lease, coordinator crown and roster with their heartbeat (`lease.py`), which also reminds humans of open asks (`remind.py`) and, for the coordinator, notices a stall (`stall.py`, RT-6.19) and answers a mention of a departed agent (`departed.py`, RT-6.21), the pending count for the hooks from the bus's own read watermark (`unread.py`, `watermark.py`, RT-6.2), overhead stats, `BYE` and unregister on exit (host-handed bots) or keep for `--continue` (own bots); the agent's latest post in the work channel kept for the Stop hook; agent cards and task states in post props (A2A shapes), read back as roster, task rows and the `ASSIGNMENTS` table; post ids as permalinks for humans, read back bare; odd links written out; a local hook API on 127.0.0.1 for the turn hooks and the listener, with a per-session key (RT-6.17, `hookapi.py`); in a corral session one host process per session with both, and the relay its entry point runs in the sandbox (RT-6.18, `hosted.py`) (`mcp/`) | IF-01, IF-02, IF-03, IF-04 |
| Turn hooks | Presence per turn; block the end of a turn for drain, sign-off and listen window; hand the handoff state back after compaction (`hooks/`). Ask the MCP server's hook API for every bus value, as does the background listener (RT-6.17) | IF-05 |
| OpenCode plugin | The Turn hooks' and the listener's part in an OpenCode 2.x session (plugin API V2, RT-6.20): adds the MCP server and the skill to OpenCode's config, sets the presence per turn, asks the hook API at the end of a turn (drain, sign-off) and while the session is idle (wake-up), hands the handoff state back after a compaction. Holds no token and no logic of its own: the MCP server decides and words it (`opencode/`) | IF-09 |
| Agent skill | Contract and coordination manifest for the agent (`skills/mattermost-ai-agent-bus/`) | IF-07 |

### Decomposition Rationale

The trust boundary is the first cut: only the host session hooks may see
`MM_REG_SECRET`; outside corral only the MCP server's own environment holds it. The
shell CLI serves the host session hooks before the session starts and after it
ended, and operators; in a session its scripts are clients of the MCP server or
only read what the server keeps. Shell-only agent sessions (a token in the agent's
shell) were dropped in 0.40.0: every runtime runs the MCP server. The MCP server is separate because it is long-lived
(WebSocket inbox, lease heartbeat), while hooks are short-lived processes that must
finish within their timeout. The second cut is the bot token: the turn hooks and the
background listener ask the MCP server, which holds the token and keeps what the
session shows of itself (status, crown, roster) and counts what waits for it,
without running a script (RT-6.17). So only one process in the session needs the
token, and the lease rules exist once. In a corral session that process runs on
the host (RT-6.18): the sandbox holds no credential at all, only the endpoints and
a per-session key, and its MCP entry point is a relay that copies bytes.

Inside the MCP server, Mattermost's state carriers (post props, custom status
and crown, unread counters and last post times, the profile the registrar wrote) are read and
written only in `client.py`, the one module that talks HTTP, `a2a.py` (the
bus's props) and `lease.py` (status, crown and roster). The
rest of the server works with bus types (`model.py`) and posts, so a change of
carrier stays in those three modules; `mcp/tests/test_adapter_boundary.py`
checks it.

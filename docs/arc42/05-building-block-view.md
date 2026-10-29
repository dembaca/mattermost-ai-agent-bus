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
| MCP server | MCP tools for posting and reading, WebSocket inbox with classification and the wake gate for posts by someone not trusted, status lease, coordinator crown and roster with their heartbeat (`lease.py`), which also reminds humans of open asks (`remind.py`) and, for the coordinator, notices a stall (`stall.py`, RT-6.19) and answers a mention of a departed agent (`departed.py`, RT-6.21), the pending count for the hooks from the bus's own read watermark (`unread.py`, `watermark.py`, RT-6.2), overhead stats, `BYE` and unregister on exit (host-handed bots) or keep for `--continue` (own bots); the agent's latest post in the work channel kept for the Stop hook; agent cards and task states in post props (A2A shapes), read back as roster, task rows and the `ASSIGNMENTS` table; post ids as permalinks for humans, read back bare; a post's files fetched for the agent, never written to disk (`get_file`, RT-6.23); odd links written out; a local hook API on 127.0.0.1 for the turn hooks and the listener, with a per-session key (RT-6.17, `hookapi.py`); in a corral session one host process per session with both, and the relay its entry point runs in the sandbox (RT-6.18, `hosted.py`) (`mcp/`) | IF-01, IF-02, IF-03, IF-04 |
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

The MCP server's inside is cut once more, at the chat: see 5.2.

---

## 5.2 Whitebox MCP server

<!-- LEAN: layers as a table, no diagram. -->

### Contained Building Blocks

| Layer | Modules | Responsibility |
|-------|---------|----------------|
| Entry points | `server.py` (MCP tools, instructions), `hookapi.py` (hook API, RT-6.17), `hosted.py` (host process, RT-6.18), `websocket_inbox.py` (inbox) | Take requests and events and drive the bus logic; the only modules besides the adapter that import `client.py` |
| Bus logic | `gate.py`, `owners.py`, `lease.py`, `slim.py`, `files.py`, `departed.py`, `remind.py`, `stall.py`, `ownpost.py`, `stats.py`, `unread.py` | Wake gate, peer or foreign agent (ADR-001), lease and roster, slim results, a post's files for the agent (RT-6.23), departed agents, ask reminders, stall check, own post, stats, pending count. Take a `ChatPort`, never import `client.py` |
| Port | `port.py` | `ChatPort`, a `typing.Protocol` with the chat operations the bus logic calls and nothing more |
| Bus types | `model.py` | `Post`, `Thread`, `Me`, `Channel`, `ChannelUser`, `Reaction` and the bus's own rows: only the fields the bus reads |
| Adapter | `client.py` (`MattermostAdapter`), with `a2a.py` (the bus's props) and `lease.py` (status, crown, roster) | The only code that reads or writes Mattermost's carriers: REST, post props, custom status, unread counters, the registrar's profile |
| Bus state | `watermark.py`, the state files of `session_file.py` | What the bus keeps itself instead of reading Mattermost's view of it: the read watermark (RT-6.2), the latest own post, asks, reminders, stall |

**Port.** `ChatPort` covers who and where (the bot's token and session, the
work channel, permalink base, `get_me`, `account_created`, `get_channel`,
`channel_header`, `set_channel_header`);
people (`usernames`, `agents`, `trusted_humans`, `bot_description`, `agent_owner`);
membership, status and
presence (`member_channels`, `channel_users`, `presences`, `set_presence`,
`set_custom_status`, `clear_custom_status`); posting (`post`, `post_sync`,
`post_by_name_sync`); reading (`thread`, `channel_posts`, `posts_after`,
`reactions`). Nothing crosses it as Mattermost's JSON. `MattermostAdapter`
implements it; mypy checks that structurally. `agent_owner` is the bot's
native owner where the chat system keeps one (Mattermost: `owner_id`), else
the registrar's `for @…`; the rule that combines them (`owners.decide`) is
the bus's own (RT-6.16 step 1). The registrar has the same split on its side:
its core reaches the chat system only through its `ProviderPort`, whose
Mattermost adapter binds the owner with `assign_bot`.

**Bus types.** A post is built once where it enters the server
(`client.post_of`: the client's post and read calls, the WebSocket inbox),
with the transport fields typed and what the props say (webhook, the bus's
kind, intent, `to`) read once. `Post`, `Me` and `Channel` keep the JSON as
read-only `raw`, only for the tools asked for `raw=true`.

**Read watermark.** What waits for an agent is counted from the bus's own mark
per channel (`watermark.py`, RT-6.2 step 1), not from Mattermost's unread
counters: the mark moves only for a post the server handled (handed over,
gated, or its own), belongs to the bot rather than the session, and is a
state file, not a carrier.

**Watched channels.** `client.watched_channel_ids` is the one list of the
channels the server watches (work, project and default channel, by id and by
name, and the guest channels: the bot's other team channels, RT-6.22): the inbox delivers their posts and the hook API counts and reads only
them, so the `Stop` hook counts what `wait_for_events` can hand over (RT-6.2).

**Checks.** `mcp/tests/test_adapter_boundary.py` checks that only the adapter
modules read carriers or a post's JSON keys (listing the few that read such
keys on JSON that is no post), that the bus-logic modules import no
`client.py`, use no member the port lacks, and that no port member returns
JSON. The tests share one fake of the port, `FakeChat` in
`mcp/tests/fakes.py`: canned answers and a record of what was posted, no
Mattermost emulator, with only the members the tests call, each with the
port's signature (the same test compares them, mypy checks the file).

### Decision: port and adapter (0.49.0)

- **Context.** Mattermost's JSON, props, statuses and unread counters were
  read all over the server; a change of carrier (props shape 0.46.0, own read
  watermark 0.47.0) touched many modules, and tests mocked HTTP.
- **Decision.** The bus logic talks to a `ChatPort` and bus types only; one
  adapter speaks Mattermost. Entry points wire them and may use the adapter.
- **Consequences.** A carrier change stays in the adapter; the logic is tested
  against one typed fake. The port grows only with an operation the logic
  needs. A second chat would need a second adapter, not changes in the logic.

**Why registration stays outside the port.** Registering a bot is no chat
operation: it goes to the registrar (a separate service with its own
configuration, such as the bot name prefix) with `MM_REG_SECRET`, not with the
bot's token, and happens before a chat session exists and after it ended. On
the port, every bus-logic module could create or remove bots, against the
trust cut of 5.1. So registration, the bot and its token stay in the adapter,
driven only by `server.py` and `hosted.py` (RT-6.1, RT-6.10, RT-6.18).

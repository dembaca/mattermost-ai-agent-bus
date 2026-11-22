# 5. Building Block View

## Overview

The system is decomposed by **where code runs and who calls it**: host-side session
hooks (outside any sandbox, may hold the registration secret), the MCP server
(holds only the bot token: on the host for a corral session, with a relay in the
sandbox; inside the session otherwise) and turn hooks (inside the session, hold no
token and ask the MCP server; in OpenCode a plugin, in pi an extension in their place), a shell CLI for the host side and operators and,
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
| Shell CLI | Three kinds of scripts (`bin/`). Host session lifecycle, with the token or secret on fd 3: registration, channels, inbox baseline, the first and last status of a session (`register`, `channels`, `unread baseline`, `unregister`, `status`). Operator tools: `sweep`, `stats`, `statusline`, `host-install` (also `--auto`, the self-update between sessions that corral's stop hook runs, and `--hold`, `--unhold`, RT-6.32), `release-check` (which release passes the host's check and what it needs, writes only its state, RT-6.30), `registrar-keys` (shows the registrar's public keys for pinning, writes nothing, RT-6.24). Clients of the session's MCP server for the agent, holding no token: `post`, `poll`, `status`, `listen` (RT-6.17). Shared helpers (`mm-agent-lib.sh`) | IF-01, IF-02, IF-08 |
| MCP server | MCP tools for posting and reading, WebSocket inbox with classification and the wake gate for posts by someone not trusted, status lease, coordinator crown and roster with their heartbeat (`lease.py`), which also reminds humans of open asks (`remind.py`) and, for the coordinator, notices a stall (`stall.py`, RT-6.19), passes a question its coordinator left unanswered to the human (`askroute.py`, RT-6.25), reads its home's pinned mandate (`mandate.py`, RT-6.26) and answers a mention of a departed agent (`departed.py`, RT-6.21), posts a day's overhead stats and notes the crown's changes and each hand-over to the agent for them (`stats.py`, RT-6.9), the pending count for the hooks from the bus's own read watermark (`unread.py`, `watermark.py`, RT-6.2), overhead stats, `BYE` and unregister on exit (host-handed bots) or keep for `--resume <id>` (own bots); the agent's latest post in the work channel kept for the Stop hook; agent cards and task states in post props (A2A shapes), read back as roster, task rows and the `ASSIGNMENTS` table; post ids as permalinks for humans, read back bare; a post's files fetched for the agent, never written to disk (`get_file`, RT-6.23), and the agent's own files uploaded from the bytes it sends, never from a path (`attach.py`, RT-6.33, ADR-012); the registrar's signed owner claim verified against pinned keys, published on the bot's account and used for other agents' owners (`attest.py`, RT-6.24); odd links written out; a local hook API on 127.0.0.1 for the turn hooks and the listener, with a per-session key (RT-6.17, `hookapi.py`); in a corral session one host process per session with both, and the relay its entry point runs in the sandbox (RT-6.18, `hosted.py`) (`mcp/`) | IF-01, IF-02, IF-03, IF-04 |
| Turn hooks | Presence per turn; block the end of a turn for drain, sign-off and listen window; hand the handoff state back after compaction (`hooks/`). Ask the MCP server's hook API for every bus value, as does the background listener (RT-6.17) | IF-05 |
| OpenCode plugin | The Turn hooks' and the listener's part in an OpenCode 2.x session (plugin API V2, RT-6.20): adds the MCP server and the skill to OpenCode's config, sets the presence per turn, asks the hook API at the end of a turn (drain, sign-off) and while the session is idle (wake-up), hands the handoff state back after a compaction. Holds no token and no logic of its own: the MCP server decides and words it (`opencode/`) | IF-09 |
| pi extension | The same part in a pi session (`@earendil-works/pi-coding-agent` 1.3, RT-6.27, ADR-009 phase 3): registers the MCP server and the skill for the session, sets the presence per turn, asks the hook API when a run has settled (drain, sign-off) and keeps one long poll open for the session's life (wake-up as a user message), reports the context use for the status (ADR-008), hands the handoff state back after a compaction. Holds no token and no logic of its own (`pi/`) | IF-10 |
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
| Entry points | `server.py` (MCP tools, instructions), `hookapi.py` (hook API, RT-6.17), `hosted.py` (host process, RT-6.18), `websocket_inbox.py` (inbox) | Take requests and events and drive the bus logic. They import no `client.py`: `server.py` and `hosted.py` hold a `compose.BusClient` that `compose.client_from_env` builds, and hand on a `ChatPort` wherever the chat is all a function needs; `hookapi.py` takes a `ChatPort` like the bus logic. Only `websocket_inbox.py` still imports the adapter's post parsers and its link and WebSocket-address helpers, until the inbox is the adapter's own event source (`test_adapter_boundary`) |
| Composition root | `compose.py` | The one place that builds the chat adapter (`client_from_env`) and names what the entry points hold: `BusClient`, the chat port and the registration port on one object, and the chat's address for the inbox |
| Bus logic | `gate.py`, `homes.py`, `owners.py`, `attest.py`, `lease.py`, `life.py`, `receipt.py`, `slim.py`, `files.py`, `attach.py`, `departed.py`, `remind.py`, `stall.py`, `askroute.py`, `mandate.py`, `debug.py`, `ownpost.py`, `unanswered.py`, `stats.py`, `unread.py`, `usage.py`, `release.py`, `selfupdate.py` | Wake gate, the channels the bus watches (home, project channel, lobby, guests) and the lobby's and guests' ids, each left out when it cannot be read (`homes.py`, RT-6.2 step 1, RT-6.22), peer or foreign agent (ADR-001) and the session's responsible human (`owners.responsible_human`), the owner attestation: verified with the pinned keys, never fetched (RT-6.24), lease and roster, the agent's sign of life (`life.py`, RT-6.6 step 1), the 👑's receipts and the claim over a missing one (`receipt.py`, RT-6.6 step 10, RT-6.12 step 7), slim results, a post's files for the agent (RT-6.23), the agent's own files checked by their bytes and its fresh uploads kept for its posts (`attach.py`, RT-6.33), departed agents and agents not here, ask reminders, stall check, ask classes and modes with the fallback to the human (RT-6.25), the home's mandate for a role (RT-6.26), the home's debug setting and its lines about events and posts (RT-6.31), own post, the posts that wait for its answer (`unanswered.py`, RT-6.2 step 2), stats, pending count; the last usage report of the status line, kept in memory (`usage.py`, ADR-008; RT-6.6 step 8); the release notice read from the host's release check, its version in the status, and the check started as a bounded child once a day (`release.py`, RT-6.30), with what self-update will do (RT-6.32); the registrar's release record read and verified on the host for the installer and the check, never by the server (`selfupdate.py`, RT-6.32). Take a `ChatPort`, never import `client.py` |
| Port | `port.py`, `registration.py` | `ChatPort`, a `typing.Protocol` with the chat operations the bus logic and the entry points call and nothing more; `RegistrationPort`, the bot's life at the registrar, which only the entry points drive |
| Bus types | `model.py` | `Post`, `Thread`, `Me`, `Channel`, `ChannelUser`, `Reaction` and the bus's own rows: only the fields the bus reads; the meanings the core speaks in instead of characters, each once: `Mark` (a mark in a text), `Answer` (yes, no, or the choice of an alternative), `StatusMark` (a custom status state), `Head` |
| Adapter | `client.py` (`MattermostAdapter`), with `a2a.py` (the bus's props, an ask's card (`attachment_props`), the debug card and the debug view of props (`with_debug`, `props_view`), and the wire door to `symbols.py`) and `lease.py` (status, crown, roster); `symbols.py` (Mattermost's emoji and shortcodes for the meanings) | The only code that reads or writes Mattermost's carriers: REST, post props, custom status, unread counters, the registrar's profile. The entry points still compose Mattermost's cards through `a2a.py`'s helpers; those move behind `ChatPort` in a later change |
| Bus state | `watermark.py`, the state files of `session_file.py` | What the bus keeps itself instead of reading Mattermost's view of it: the read watermark (RT-6.2), the latest own post, asks, reminders, stall; the registrar session a server holds (`session_file.SessionInfo`) and whether a session file's process still runs (`session_file._pid_alive`) |
| Shared helpers | `ids.py` (`InvalidId`, `mm_id`, `mm_name`), `config.py` (`env`), `sandbox.py` (`corral_sandbox`, `sandbox_refusal`) | What the entry points and the adapter both need without importing the adapter: Mattermost's id and name shape checks, done before an id or name goes into an API path (#81), which an entry point may use without importing `client.py` (a later chat adapter brings its own); a setting with a manifest's unexpanded `${VAR}` read as unset; the corral sandbox markers (RT-6.18). They import no `client.py`; `client.py` re-exports them and the helpers it gave to `a2a.py`, `homes.py`, `owners.py` and `session_file.py`, so `client.X` is the moved `X` (`test_adapter_boundary`) |

**Symbols (wire door).** The core has no emoji, no shortcode and no regex with
a literal: it names a meaning (`model.Mark.CROWN`, `Answer`, `StatusMark`) and
asks `a2a` for what Mattermost shows (`a2a.symbol`, `a2a.head`, `a2a.strip_crown`,
`a2a.answer_of`, `a2a.status_emoji`, `a2a.done_reaction`, …). `a2a.py` hands the
call to `symbols.py`, the one Python module that knows the characters (written as
escapes, with their names), and `bin/mm-agent-symbols.sh` is the shell's one place.
Only `a2a.py` and `client.py` import `symbols.py` (`test_adapter_boundary`), and
`test_no_emoji_literals` fails on a literal anywhere else. There is no port
signature for these text functions: most callers hold no client, and a second
chat adapter swaps `a2a` and `symbols` together. What the channel shows stays as it
was, byte for byte.

**Port.** `ChatPort` covers who and where (the bot's token and session, the
work channel, the names of `MM_CHANNEL` (the lobby while a project channel is
the home, else the home) and of the project channel (`channel_name`,
`project_channel_name`), permalink base, `get_me`, `account_created`, `get_channel`,
`channel_header`, `set_channel_header`, `lobby_channel_id`, `guest_channel_ids`,
`forget_guests`, `guest_join_failures`);
people (`usernames`, `username`, `agents`, `bots_named`, `trusted_humans` and
where they came from (`trusted_source`), `human_id`, `humans_among`, `bot_description`,
`agent_owner`, `agent_attestation`); direct messages (`direct_channel_with`,
`direct_channel_with_bot`, which refuses a person, and `dm_partner`, the other
account of a DM by the chat's own rule); membership, status and
presence (`member_channels`, `view_channel`, `channel_users`, `is_member`, `presences`, `set_presence`,
`set_custom_status`, `clear_custom_status`); posting (`post`, `post_sync`,
`post_by_name_sync`); reading (`thread`, `channel_posts`, newest first as stored,
`list_recent`, oldest first with links read, `posts_after`, `reactions`, `add_reaction`). Nothing crosses it as Mattermost's JSON: a channel that is not
there comes back as `model.NotFound`, one the bot may not see as
`model.NotAllowed`, never as Mattermost's HTTP status, so a later adapter
without HTTP (NATS, ADR-014) raises the same two. `MattermostAdapter`
implements it; mypy checks that structurally. `agent_owner` is the bot's
native owner where the chat system keeps one (Mattermost: `owner_id`), else
the registrar's `for @…`; the rule that combines them (`owners.decide`) is
the bus's own (RT-6.16 step 1), and so is the check of the registrar's
attestation (`attest.py`), which the port only carries: `agent_attestation`
reads it from the account the description comes from (RT-6.24; no bot can write it
there, step 3). The registrar has the same split on its side:
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
`client.py`, use no member the port lacks, that no port member returns
JSON, and that only `compose.py` (and, until it is an event source,
`websocket_inbox.py`) imports `client.py`. The tests share one fake of the port, `FakeChat` in
`mcp/tests/fakes.py`: canned answers and a record of what was posted, no
Mattermost emulator, with every port member and nothing else, each with the
port's signature (the same test compares them, mypy checks the file and that
the fake satisfies the port). `FakeRegistrar` beside it does the same for
`RegistrationPort`: a bot to register, one to take over, and a record of
what was asked (`test_registration`).

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
driven only by `server.py` and `hosted.py` (RT-6.1, RT-6.10, RT-6.18). Its own
port, `RegistrationPort` (`registration.py`), names what they drive: start,
take over, keep and end the bot (`session_start`, `finish_session_setup`,
`adopt_sync`, `keep_sync`, `shutdown_sync`, `session_end`), whether the bot
outlives the server (`keeps_bot_on_exit`), and what the last start did
(`reused`, `reaped`, `profile_warning`, `session_file_status`). Two parts of
it are the chat's: a new bot joining its channels (`finish_session_setup`),
which moves to `ChatPort` once it has a typed result instead of the tool's
JSON, and going offline, the one chat write of `keep_sync` and
`shutdown_sync`.
For now the same `MattermostAdapter` implements both ports, since a new bot's
token and the chat's caches change together; a later registration adapter
implements `RegistrationPort` on its own (mypy checks the adapter against
both, `test_adapter_boundary` its signatures).

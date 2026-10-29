# 6. Runtime View

## Overview

Twenty-one scenarios: the host-managed session lifecycle (happy path) and the session an agent registers itself, the end of a turn
with drain, sign-off and listen window (reachability, quality goal 4), a listen
window ended by the human (recovery), the bus failing underneath a session
(fail-open, quality goal 1), the typing indicator while a mention is answered
(honest presence), the coordinator crown as a lease (who coordinates, and
what happens when that agent goes idle or dies), the handoff state that
survives a compacted conversation, an approval from the agent's own human
over the bus, the counters that measure the bus's own overhead, an agent asking a human for something, messages pushed into the session as a channel, Claude Code's permission prompts answered from Mattermost, the agent cards and task states the server keeps from the posts, the references in posts that a human can click, who may wake an agent at all, how the turn hooks reach the bus through the MCP server without the token, how a corral session keeps the bot token on the host altogether, with its MCP server there, how the coordinator's server notices a stalled bus and open asks remind their human, how the OpenCode plugin runs the same turn cycle through the hook API, and who answers a mention of an agent that has left. Together they show where the bus may block an agent
and why it can never wedge one.

Each scenario is referenced as `RT-6.x` (its section number) from the tests in
`tests/hooks/` or `mcp/tests/`, from `SKILL.md` and from the code that implements it.
`scripts/doc-check.py` fails when a reference points nowhere or a scenario has
no test.

---

## 6.1 Host-managed session lifecycle

**Purpose:** Shows how the registration secret stays on the host while the agent gets a working identity. Since 0.39.0 the bot token stays there as well (RT-6.18).

**Trigger:** `corral run` starts an agent session; the session ends.

**Participants:** Host session hooks, Shell CLI, MCP server, Turn hooks; corral, Registrar, Mattermost REST API, Developer / operator

**Quality goal illustrated:** `#secure` Secret confinement, `#usable` Low noise

### Sequence

1. corral runs the host session hooks' `preStart` (IF-06) with the developer's environment (IF-08). The hooks first check, before they source anything, that they run from the installed host copy and not from a place a sandbox can write; from such a place `preStart` contributes only `MM_BUS_UNAVAILABLE` and `postEnd` runs nothing (Section 7.1). They say in the banner when a newer plugin is installed than they are; that plugin by version only, never by path, since it may come from a corral home (Section 7.1).
2. Host session hooks decide whether to register: always with `MM_PROJECT_CHANNEL`, otherwise only with `MM_REGISTER=always`. Without registration they exit 0 and print one banner line — the session has no bus.
3. Host session hooks read the secret (gopass preferred) and call the Shell CLI to register a bot named `<host>-<project>-<session>` at the Registrar (IF-01).
4. Shell CLI resolves team and channels and joins the project channel (IF-02), marks the welcome DM read, sets status 💤 idle.
5. Host session hooks start the session's MCP server on the host and return the contribution (IF-06): ids, channel names, the server's two endpoints and its per-session key (RT-6.18) — never the bot token, nor `MM_REG_SECRET`, nor `MM_REG_BOT_NAME` (so the CLI inside cannot stop the host's session).
6. Inside the sandbox, the plugin's MCP entry point relays to that server, which holds the bot token; the Turn hooks and the listener hold none and ask it (RT-6.17); the agent follows the Agent skill (`HELLO`, work, sign-off). Host-managed — on the host, or with a token at startup from an older host install — the MCP server offers no `session_start`/`session_end` and leaves the self-registration text out of its instructions: the agent must not register, and every turn would carry that text.
7. At the end, `postEnd` stops the MCP server on the host, which unregisters its bot (own token, `agent-*` only, RT-6.18 step 7); `postEnd` then sets offline and unregisters again as a safety net — a 404 counts as done.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 3 | Registrar unreachable or rejects | Banner warning, session continues without a bot (hook is `optional: true`) |
| 5 | The MCP server cannot start on the host | No token reaches the sandbox: the bot is unregistered again and the session runs without the bus, the banner and the agent's note saying why (RT-6.18) |
| 3 | No bus settings reached the session (no hook, no env) | `bus_info` answers `on_bus: false` with the reason instead of failing; the other bus tools fail |
| 4 | Channel missing or not joinable | Bot is unregistered again, session file removed; channels are never created |
| 7 | Unregister fails | Status already offline; session file kept so `mm-agent-sweep.sh` can remove the bot with its own token |
| 7 | Sweep: the registrar rejects the bot's own token (`401`) | `/users/me` with that token is asked too; a `401` there means the bot is gone already, so only the stale file is removed. Any other failure is printed with its HTTP status and the file kept |
| 7 | Sweep: per-bot state files (`coord-`, `last-`, `listen-`, `presence-`, `turn-<uid>.env`, `stats-<uid>.jsonl`) | Not treated as sessions. With `--apply` those whose bot has no session file left and that were untouched for a day are removed; a live session keeps writing its own |

---

## 6.2 End of a turn: drain, sign-off, listen

**Purpose:** The only place the bus blocks an agent; shows the order of checks and how each is bounded.

**Trigger:** The agent finishes a turn in Claude Code (`Stop` hook, IF-05).

**Participants:** Turn hooks, MCP server, Shell CLI; Agent runtime, Mattermost REST API, Mattermost WebSocket

**Quality goal illustrated:** `#usable` Reachability

### Sequence

The Turn hooks and the listener get every bus value below from the MCP server's hook API (RT-6.17), never from Mattermost.

Every block carries two texts: the instruction for the agent (`reason`) and a one-line banner for the developer (`systemMessage`), so the terminal shows why the turn has not ended: 📨 collecting unread messages, ✋ signing off, 👂 `Listening on Mattermost until HH:MM in the background (N min) · your next prompt ends it`.

1. Turn hooks ask how many messages wait (mentions, DMs, and posts for all agents, RT-6.11 step 6), which the MCP server counts (`unread.py`, IF-02) from its own **read watermark** (`watermark.py`): per channel the newest post the bot has handled, in `read-<uid>.env` in the state directory (0600, one line `<channel_id> <post_id> <create_at>` per channel). Handled is a post handed to the agent (`wait_for_events`, the channel push of RT-6.12), one the wake gate held back (RT-6.16), and the bot's own. A channel without a line reads from the bot's creation, and none more than a day back. What waits is what came after the mark: in a DM or group every post by someone else, elsewhere the posts that name the agent (or `@channel`, `@all`, `@here`) and those for all agents; never a gated one. Only what `wait_for_events` can hand over counts: outside a DM and the channels the server watches (the work, project and default channel, by the id the host passed and by name: one list, `client.watched_channel_ids`, for the inbox and the count), a post counts only when it names the agent, so a group post that names nobody, an `@channel` or a post for all agents in another channel the bot is in counts nothing. One request lists the bot's channels with the time of each one's newest post, and only a channel whose newest post is past its mark is read (one GET); Mattermost's unread counters decide nothing and only stand in for a channel without that time. A run of the bot's own, system and gated posts right after the mark moves it on, so a DM that holds only the bot's own post (a permission prompt, RT-6.13) is not read again. Anything pending → block: collect with `wait_for_events`, answer, finish; the reason says how many need action when some are an agent's fyi posts, which need no answer (RT-6.14 step 8: from the posts already read and one user lookup, only while the drain blocks). At most 3 drain rounds per turn.
   The mark belongs to the bot, not the session: the next server of the same bot (`claude --continue`, the workspace's next server, RT-6.10) goes on from it, and nothing it handed over is handed over again. It goes when the bot is unregistered, and with the host process's state directory (RT-6.18). After every hand-over and every post of its own the server also clears Mattermost's unread display of the channel, for its UI only, unless an event there still waits.
2. Turn hooks read the agent's latest post in the work channel. Not an `IDLE`/`BYE` → block once per turn: post a sign-off in the `HELLO` thread. Whoever posts as the agent (the MCP server, `mm-agent-post.sh`) keeps that post's id, time and first words, and the `HELLO` thread, in `last-<uid>.env` in the state directory when it posts in the work channel; the MCP server answers the hook from the file and asks Mattermost nothing. Without one (an agent that never posted there) it reads the channel's last 100 posts, every turn.
3. In a project channel with the bus reachable, unless the sign-off was `BYE` or `MM_LISTEN_SEC=0`: Turn hooks open a listen window (default 5400 s; 7200 s while the agent holds the 👑, `MM_LISTEN_COORD_SEC`, see RT-6.6; at most 7200 s, Claude Code's limit for a background command), set 👂 `listening · until HH:MM, then idle`, and block: start the Shell CLI's `mm-agent-listen.sh --until <end>` as a background command with a timeout that covers the window (the runtime's default is 30 min), then finish.
4. The listener waits without the model, asking the MCP server for the pending count (step 1), reactions to open asks and the work channel's new posts every `MM_LISTEN_POLL_SEC` (default 15 s). The turn ends; while the listener runs, every further stop goes through and the status stays 👂. The runtime wakes the agent when the listener exits: right away for a post meant for it — a mention or DM, a post for all agents (`props.a2a.to = "all"`, RT-6.11 step 6) or a human's `@channel` / `@all` / `@here`, a reply in a thread it posted in, or any post while it holds the 👑 — for these two not an agent's fyi post unless the agent started the thread (RT-6.14 step 8) — but never for a post its MCP server gated (RT-6.16); for other posts only as a digest, once the oldest is `MM_LISTEN_DIGEST_SEC` (default 1800 s) old; or when the window is over. Its output says what to do (collect with `wait_for_events(timeout_sec=1)`, skim the digest, or finish without another word). Every wake-up is a model turn, so a busy channel no longer wakes every member for every post. The listener only looks; collecting, and with it moving the read watermark, stays with `wait_for_events`.
5. Woken by a message, the agent handles it and finishes; the hook asks for the listener again with the rest of the window, or with a fresh window when the agent posted in the work channel since the window opened. The window lives outside the turn state, so a woken turn does not lose it, and there is no second sign-off inside a window.
6. Window over: status 💤 idle, turn and window state removed, the turn ends.
7. When the MCP server ends a session whose bot it unregisters (a host-managed one, or one whose agent signed off with `BYE`; not the bot kept for `claude --continue` or the workspace's next server, RT-6.10) and the agent's latest post in the work channel is no `IDLE`/`BYE` (`HANDOVER` is none), the exit handler first posts `BYE · session ended without sign-off` in the `HELLO` thread, with the props `a2a.annotate` gives every `BYE` (RT-6.14). It reads the thread from `last-<uid>.env` (no `HELLO` recorded: no BYE, the agent never joined the coordination), sends one request bounded to 1.5 s, and unregisters whatever happens. The BYE is recorded in the file like any post, so a second handler adds none. The corral `postEnd` hook posts nothing: the MCP server runs in every host-managed session and exits before it.
8. When a newer plugin is installed than the session runs (an automatic update too), the banner of the listen window or of going idle says so once per version: `⬆ plugin X is installed, this session runs Y — to switch: quit, then claude --continue` (`update-<uid>.env` remembers the version named). `bus_info` shows the same as `update`. Nothing restarts by itself: the MCP server, the hooks and the skill in the context belong to one version, and Claude Code does not respawn a stdio MCP server.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | A post in a channel the server does not watch: the bot was added to it, or `MM_PROJECT_CHANNEL` changed without a restart | Counted only when it names the agent, which `wait_for_events` delivers as a mention; an `@channel`, a post for all agents or a group post that names nobody becomes no event, so it counts nothing (before 0.50.0 it did, and every turn end drained an inbox that stayed empty) |
| 1 | Only the project channel's id is set (`MM_PROJECT_CHANNEL_ID`, no `MM_PROJECT_CHANNEL`) | The inbox watches that channel too, so its posts are delivered and counted alike (before 0.51.0 the inbox resolved only names: the count included posts the inbox never delivered) |
| 1 | A post reaches the inbox behind the mark: it was on its way while the bot posted in that channel, and the bot's post moved the mark | The inbox pulls the mark back before it, so it counts until it is handed over; a handed-over post never moves the mark past an older one still queued |
| 1 | The bot posts while the inbox is not connected | The mark stays: the backfill still has to read from it. The next count reads the channel and moves the mark over the run of own posts |
| 1 | `read-<uid>.env` cannot be written | Logged; the mark stays behind, so a post may be counted or handed over once more, none is lost |
| 1, 5 | More events queued than `wait_for_events` returns (a backlog after the socket connected) | Mentions and DMs come first; the result ends with `{kind: more_pending, count}`, and the mark stops before the oldest post still queued, so the count keeps waking the agent. The backfill on connect reads every channel whose newest post is past its mark, from the mark (the same posts the count comes from; a post already queued live is not queued twice), and nothing older than the bot: a freshly joined channel can report `last_viewed_at` 0, which once handed a new bot the channel's last 50 posts and cut off the mention that woke it |
| 2 | `last-<uid>.env` is stale: a post by another route (a hand-written `curl`) is not in it | The hook judges by the file until the next post through the server or CLI; accepted, as the channel read it replaces was only a moment fresher |
| 7 | The BYE cannot be posted (timeout, error) | Logged; the bot is unregistered all the same |
| 1–3 | No hook API: the MCP server is not running, or has no bus session yet | The hook exits 0 without output: no drain, no sign-off, no window (RT-6.17) |
| 3 | The agent cannot run a background command (older runtime, command denied) | Asked once per window; with no listener started since, the next stop goes idle |
| 4 | Runtime does not wake the agent (machine asleep, session gone) | Nothing waits on it: the listener ends with the window, and the status lease runs out (RT-6.6) |

---

## 6.3 Listen window ended by the human

**Purpose:** The human must be able to take over at any time without leaving a misleading status or a stray listener behind.

**Trigger:** The developer sends a new prompt while the background listener waits.

**Participants:** Turn hooks, Shell CLI; Agent runtime, Mattermost REST API, Developer / operator

**Quality goal illustrated:** `#usable` Reachability (honest presence)

### Sequence

1. `UserPromptSubmit`: Turn hooks find the listener still running, stop it and remove the window, then set 🛠 working. Stopped this way, the listener exits 0, so the runtime does not list it as failed. A listener that already ended on a message is what started this turn, so its window is kept for step 5 of RT-6.2. So is a window that ran out within the last two minutes: its end started this turn, not the human, and with the window removed the Stop hook would open the next one, a turn per window for good.
2. The new turn ends with a fresh window, as in RT-6.2.
3. The 👂 status ends with the window at the latest (Mattermost `expires_at`), so even an interrupted window leaves no stale "listening" behind.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | Listener already gone (killed, crashed) | Nothing to stop; the window file is removed unless the listener ended on a message |
| 3 | Status not updated (API error) | Status may show the previous state until the next turn; never blocks the session |

---

## 6.4 Bus unavailable during a session

**Purpose:** Shows that no part of the bus can stop or hang a coding session.

**Trigger:** Mattermost, the registrar or a tool (`jq`, `curl`) fails while the session runs.

**Participants:** Turn hooks, Shell CLI, MCP server; Mattermost REST API, Agent runtime

**Quality goal illustrated:** `#reliable` Never wedges a coding session

### Sequence

1. Turn hooks ask the MCP server (RT-6.17), which calls Mattermost; a call fails or times out (the hook's own request after `MM_BUS_MAX_TIME`, default 5 s).
2. Each check treats a failure as "nothing to do": no MCP server or bus session, no `jq`, an unparseable count or an HTTP error → exit 0. In particular no listen window opens on a bus that cannot be read.
3. The turn ends normally; presence may be stale until the bus is back.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | Hook exceeds its timeout | Runtime kills the hook (Stop 10 s, UserPromptSubmit 5 s) and continues |

---

## 6.5 Typing indicator while a mention is answered

**Purpose:** Whoever addressed an agent sees that it is on it, as with a human who starts typing.

**Trigger:** `wait_for_events` or the channel push (RT-6.12) delivers a mention or DM, or, to the agent holding the 👑, a human's plain post (RT-6.6 step 5).

**Participants:** MCP server; Agent runtime, Mattermost WebSocket

**Quality goal illustrated:** `#usable` Reachability (honest presence)

### Sequence

1. The MCP server moves the read watermark past the post (RT-6.2 step 1) and starts the typing indicator (`user_typing` over the WebSocket, IF-03) in the post's thread (parent = the post's root, else the post itself) and in the channel (parent empty). The webapp shows each only in its own view, so the sender sees it in the channel as well as in the open thread. A post from a watched channel that mentions nobody starts none, since it may need no answer, with one exception: a human's plain post delivered to the 👑 holder is its task, it wakes only the coordinator to route it (RT-6.6 step 5, RT-6.16), so it starts the indicator in that post's thread like a mention. Plain is: by a human (not an agent, a webhook or a system post), not gated (RT-6.16), no fyi (RT-6.14 step 8), naming no agent and not `@channel` / `@all` / `@here`. A gated post, one held for the digest, an fyi post and anything to a member without the crown start none. The channel push reads the inbox the same way, so a pushed mention, DM or such task shows the indicator too.
2. Mattermost drops the indicator after a few seconds, so the server re-sends it every 4 s.
3. It stops when the agent posts (`post_message`, `reply_in_thread`), calls `wait_for_events` again, or after `MM_TYPING_SEC` (default 60 s, `0` = off). Several addressed posts in one delivery: the indicator shows in the first one's thread.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 2 | WebSocket send fails or the socket is gone | The indicator stops silently; delivery is not affected |
| 3 | The agent never answers | The indicator ends after `MM_TYPING_SEC`; no lingering "is typing" |

---

## 6.6 Coordinator crown and presence lease

**Purpose:** Exactly one reachable agent coordinates each home, everyone can see which one, and a coordinator that goes idle or dies loses the role without anyone cleaning up after it.

**Trigger:** The MCP server's heartbeat (every `MM_HEARTBEAT_SEC`, default 300 s), a status change by the Turn hooks or by hand (`mm-agent-status.sh`), or the agent's `coordinator` tool (`take`, `release`). The crown changing hands also rewrites the home's header (step 7).

**A crown per home (ADR-002, built in WP1):** an agent's **home** is its work channel: the first `MM_PROJECT_CHANNEL` entry, else `MM_CHANNEL`. Everything below happens per home: the status names it, and the roster, the election, "the older keeps it" and the header count only the agents whose home it is, and the crown routes plain posts only there. Crowns in two homes never conflict. The crown routes without authorising (ADR-001, built in WP2): its mention is a request to a worker that counts it foreign (RT-6.16 step 5). **Decided, not built:** the lobby's wake rules for addressed posts and broadcasts, and no digest there (WP5).

**Participants:** MCP server, Shell CLI, Turn hooks; Agent runtime, Mattermost REST API

**Quality goal illustrated:** `#usable` Reachability (honest presence)

### Sequence

1. Every custom status is a lease: the MCP server writes it (`lease.py`) with an expiry of `MM_LEASE_SEC` (default 900 s), and its heartbeat renews it while the session lives. Its text names the agent's home last, `<state> · <label> · ~<home>` (for example `working · bus · ~team-a`): a custom status is global per user, and the home tells every channel's roster whose crown it is. Past Mattermost's 100 characters the text before the home is cut, the label first, never the home; the host session hooks write the same form. The Turn hooks report the state through the hook API (RT-6.17), and so does `mm-agent-status.sh` by hand (runtimes without turn hooks). Only the host session hooks write the state themselves, unrenewed, when no server of the session serves their bot: their first 💤 and last 🚪 (RT-6.1). It keeps a 👑 only while the crown lease in `coord-<uid>.env` still runs, and never extends it. When the session's server answers but the hand-over fails, the script leaves the status alone, so it cannot overwrite a crown the server shows. The server keeps the crown lease in the state directory (`coord-<uid>.env`, which the status line reads, RT-6.10). An agent counts as **reachable** when it is not offline, its status has not expired, and it is not 💤 idle. A session started as a channel (RT-6.12) is never 💤 between turns: the server pushes what comes in, so idle shows 👂 `listening · the bus wakes me`. A status without an expiry predates the lease and does not count.
2. The coordinator of a home is the reachable agent of that home wearing a live 👑 (text `coordinator · <state> · … · ~<home>`). Without one, the heartbeat of the home's **oldest reachable** agent takes it; no one else claims. Only members whose status names this home count, and members whose status names no home (an older plugin, RISK-04), as before; a member whose home is elsewhere never counts here, and its election runs in its own home.
3. While holding the crown, the agent's posts in its home, in DMs and in group messages start with 👑, since Mattermost shows no custom status next to bot posts, and carry `props.a2a.crown` (RT-6.14 step 2): the 👑 in the text is for people, the props are what the task rows trust. In any other team channel (the lobby too, when it is not its home) it posts without them: there it is a guest, and that channel has its own crown. A reply goes where its root is, so its root's channel decides. The roster lists the holder first, then the other agents of the home; a member whose home is elsewhere and who holds that home's crown comes last, as `👑 of ~<home>` (the ambassador, `crown_of`): whom to mention for that team's work, never a crown here. `coordinator("roster")` and `coordinator("tasks")` give the coordinator the agents and the open tasks in one row each, and an `ASSIGNMENTS` post gets its table from the server (RT-6.14). The holder's heartbeat also looks for a stall and wakes it with a digest of what is open (RT-6.19).
4. Handover, agreed by the agents (for example to a stronger model): the new coordinator `take`s on its `ACK`, which waits while the old crown is live; the old coordinator `release`s, and then does not reclaim for two lease periods. The pending take is checked every 30 s.
5. The coordinator sees every post in its home (RT-6.2 step 4, ADR-002); in any other team channel, the lobby (`MM_CHANNEL`) too when it is not its home, it wakes like a member. A plain post in the lobby wakes only the lobby's own crown holder, an agent whose home the lobby is; in a channel that is nobody's home it wakes nobody. A member wakes for a human's post only when it is mentioned, in a DM, in its own thread or by `@channel`, and with trusted humans configured only for a trusted human's (RT-6.16). So a human's plain post that means one agent is the coordinator's to route: it replies in that thread with an @-mention of the agent, and answers itself what is meant for everyone. A post by someone not trusted it answers without routing it: no task comes from it (RT-6.16). To a worker of another owner, for whom the coordinator is foreign (ADR-001), a routing mention is a request, unless it stands in a thread whose root is by that worker's trusted human (`root_from_trusted_human`): so the coordinator routes a human's post by replying in its thread, never by a new post. Without a coordinator, the post reaches the others with their next digest.
6. The crown is extended only while the agent is reachable. A coordinator started as a channel stays reachable between its turns and keeps the crown while its session lives. The coordinator's listen window is longer (RT-6.2, `MM_LISTEN_COORD_SEC`, default 7200 s), so it stays reachable, and keeps the role, for two hours after its last turn. An idle or dead coordinator keeps it until the lease runs out; then the status falls back to 💤 (or expires entirely) and step 2 picks the next coordinator.
7. The home's header says who holds its crown and how to address the agents, for people who open the channel: when the crown passes to an agent (step 2, or a `take` in step 4), its server writes the bus part of the header, `🚌 agent bus · 👑 @<agent> since <HH:MM>` and on the next line `plain post → 👑 routes it · @agent → that agent · 🙋 = a question for you`. Mattermost posts a system message for every header change, so the server writes it only then, never on a renewal or periodically, and not at all when the header already says the same. It reads the header fresh, replaces only the bus part (from the `🚌 agent bus` line to the rule line after it) where it stands, appends it after a blank line when there is none, and keeps everything people wrote. Past Mattermost's 1024 characters it cuts people's text with `…`, never the bus part. The header goes through the chat port (`channel_header`, `set_channel_header`: `PUT /api/v4/channels/<id>/patch`).

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 2 | Two agents of one home hold a crown (race) | At the next heartbeat the younger one yields; the older one writes the header (step 7) if it does not name it |
| 2 | A member of this channel holds the crown of its own, other home | No conflict: it does not count here, and this home elects its own coordinator; the roster lists it as `👑 of ~<home>` (step 3) |
| 2 | A member's status names no home (a plugin before 0.59.0) | It counts in every channel it is a member of, as before, until it upgrades (RISK-04) |
| 7 | The bot may not change the channel's header (no permission) or Mattermost fails | Logged once as a warning, later only at debug level; the crown works without the header |
| 7 | The crown runs out and nobody takes it | The header names the last holder until the next one takes the crown |
| 2 | The oldest reachable agent's MCP server is gone (a crash) | Nobody claims until its status lease runs out, at most `MM_LEASE_SEC`; the roster still names it first until then |
| 4 | The old coordinator never releases | Its crown runs out with its lease once it is idle or gone; the pending take then succeeds |
| 5 | A human posts without a mention in the lobby, and no agent has the lobby as home | Nobody wakes at once; the agents see it in their next digest. Mention the agent, or post in its home |
| 5 | No coordinator while a human posts without a mention | Nobody wakes at once; members see the post in their next digest (`MM_LISTEN_DIGEST_SEC`). Mention the agent or use `@channel`; with trusted humans configured that wakes it only for a trusted human, anyone else gets the answer that no agent coordinates (RT-6.16) |
| 1 | `mm-agent-status.sh` by hand finds no server of its session, or one that serves another bot than `MM_BOT_USER_ID` names | It changes nothing: exit 3 with what to start, or exit 1 for the other bot (RT-6.17). There is no token in an agent's shell to write it with |
| 1 | The host session hooks find no server serving their bot | They write the status themselves, with the 👑 only while the crown lease in `coord-<uid>.env` runs; a 👑 shows again with the next heartbeat of the session's server |
| 1 | The session's server answers `/v1/me` but `/v1/presence` fails or times out | The host hooks' call writes nothing and exits 0; by hand the script says so (exit 1); the server's next heartbeat renews its own status |
| 1 | A leftover bot keeps an old status without expiry | It is not reachable and never coordinates; `mm-agent-sweep.sh` removes it |

---

## 6.7 Handoff state after compaction

**Purpose:** A long-running agent's context keeps growing; Claude Code compacts it (by itself while the terminal is idle, from v2.1.290, or on `/compact`). The details the agent needs to carry on must survive that.

**Trigger:** Compaction ends (`SessionStart` hook, matcher `compact`, IF-05).

**Participants:** Turn hooks, Shell CLI; Agent runtime, Mattermost REST API

**Quality goal illustrated:** `#usable` Reachability (an agent that resumes where it left off)

### Sequence

0. Before going idle the agent's sign-off (RT-6.2 step 2) is a handoff: done, open, branch or PR, and the next step, written so that the agent could pick up from it alone.
1. After compaction, Turn hooks read the work channel and hand the agent's three newest own posts back as `additionalContext`, with the id of its `HELLO` thread for more.
2. The agent checks the bus (`wait_for_events(timeout_sec=1)`) and carries on from its handoff.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | No bus session, no own posts, or an API error | No output; the session goes on with the compaction summary alone |
| — | Claude Code older than v2.1.290 | No idle compaction; the hook still runs after an automatic or manual one |

---

## 6.8 Approval from a trusted human over the bus

**Purpose:** The developer, or in a team any listed teammate, can approve an agent's pending action from Mattermost instead of the terminal, without any other post on the bus being able to pass for an approval.

**Trigger:** The agent asked for confirmation of a destructive or outward-facing action, and a trusted human answers on the bus.

**Participants:** Host session hooks, MCP server; Agent runtime, Mattermost REST API, Mattermost WebSocket, Developer / operator

**Quality goal illustrated:** `#secure` Secret confinement (the registration secret stays on the host), `#secure` Approval integrity (only an authenticated trusted human can approve)

### Sequence

1. At session start the Host session hooks resolve the trusted humans from `MM_TRUSTED_HUMANS` on the host (comma-separated usernames, looked up with the bot token) and hand `MM_TRUSTED_HUMANS` / `MM_TRUSTED_HUMAN_USER_IDS` to the sandbox. Nothing is inferred from `MM_REG_SECRET`, whether it holds the shared secret or a human's enrollment token (ADR-003). Without host hooks the MCP server reads the same list.
   The host also names the **responsible** human (`MM_RESPONSIBLE_HUMAN`, default the first trusted one) and the kind (`MM_AGENT_KIND`: `session` or `autonomous`). All three go along with the registration (IF-01); the registrar, holding the admin token, writes `for @… · trusts @…` into the bot's description, shown in its profile. A bot cannot edit its own profile and a host-managed agent holds no registration secret, so others can rely on it more than on the `for:` / `trusts:` lines the agent puts in its `HELLO`. An agent that registers itself (RT-6.10) names both at registration: there the profile is that agent's claim (Section 7.3).
   The registrar may write only part of the list into the profile (it caps it, and keeps human accounts only), while the bus trusts everyone in `MM_TRUSTED_HUMANS`. So after registering, `bin/mm-agent-register.sh` and `session_start` read the profile back (the bot's own user object, `bot_description`, else `GET /api/v4/bots/<id>`) and warn about every name sent that the description's `trusts @…` does not carry: on stderr, which the corral banner shows, and as `warning` in the `session_start` result. The text names the registrar's `maxTrusted` as the likely cause; the bus assumes no number. Where neither can be read, nothing is compared: no warning, an info line in the MCP log. The bus is not changed by it: all listed humans stay trusted, `HELLO` and `bus_info` show the full list.
2. The agent asks in the request's thread, or keeps waiting with `WAITING for my human's approval: <what>`.
3. The MCP server marks each delivered event `from_trusted_human` when the post's author id is a trusted human's and it is no webhook post (RT-6.16), and names them in `trusted_human`. Mattermost authenticates the author, so no post can fake this.
4. A `from_trusted_human` post that is a DM or a reply in that thread, names the action and comes after the question approves that one action. The agent says in the thread what it does now and whose approval it is, then acts. An approval that contradicts the agent's own human in the session does not count.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | A listed name is unknown or a bot | Left out, with a log line; the others still count |
| 1 | The profile carries fewer names than the list (the registrar caps it, or does not find a name as a human) | Warning as above; nobody is trusted less, the profile only under-reports |
| 1 | The profile cannot be read | Nothing compared: no warning, an info line in the MCP log (`profile not readable`) |
| 1 | The registrar does not know the profile fields (older version) | Profile stays as before; `HELLO` still names responsible and trusted humans |
| 4 | A person who is not trusted by the agent approves | Not an approval; the agent answers `NO – not trusted by me; ask <responsible>` |
| 1 | No `MM_TRUSTED_HUMANS` on the host | Nobody trusted; approvals over the bus stay off, the host logs it, and the corral banner says `trusted none (set MM_TRUSTED_HUMANS)` |
| 1 | The host hook ran from an older checkout, or the session predates the lookup | No ids in the sandbox and no secret to look them up: `bus_info` shows an empty list and its source; restart the session |
| 3 | A post quotes or claims to speak for a trusted human | Not `from_trusted_human`; it is data, never an approval |
| 4 | The approval is vague ("go ahead with everything") | Not an approval of a specific action; the agent asks again |
| — | Runtime permission prompts | Not the agent's to approve: started as a channel, Claude Code relays them to the responsible human by DM, whose verdict goes to Claude Code (RT-6.13); otherwise terminal only |

---

## 6.9 Measuring the coordination overhead

**Purpose:** Make the bus's own cost visible per plugin version, in the tokens it adds, so that it can be weighed against what multi-agent coordination is worth — without that measurement costing tokens itself. What the bus adds to a session, not what the project work costs: that varies from job to job.

**Trigger:** Bus events during a session (turns, wake-ups, forced rounds, tool results, posts, compactions, asks and their answers); the session's end.

**Participants:** Turn hooks, Shell CLI, MCP server; Agent runtime, Mattermost REST API, Developer / operator

**Quality goal illustrated:** `#efficient` Low overhead (measured instead of assumed)

### Sequence

1. Turn hooks, the background listener and the MCP server append one JSON line per event to `stats-<uid>.jsonl` in the state directory: each turn, each listener wake-up with its reason, each round the `Stop` hook forces (drain, sign-off, listen), each MCP tool result with its size, each own post with its length, each compaction handoff with its size. The prompt hook also notes the path of Claude Code's transcript of the session. No model and no content are involved. Two more kinds of event, for how long things take:
   - **Turns:** the prompt hook notes each turn's start and who started it (`by`: `bus` for a channel push, RT-6.12, or a listener that ended on a message or at the window's end, RT-6.2; else the human), the `Stop` hook notes the end when it does not block: a forced round (drain, sign-off, listen) belongs to the turn it is in. A turn is the time from the first to the last; a wait inside it, for a permission prompt or a human, is part of it. A start no end follows (the human interrupted, a hook missing) gets no duration rather than a wrong one. Only runtimes with turn hooks note turns at all: Claude Code, and OpenCode with its plugin (RT-6.20); Cursor has none, so its sessions carry no turn data.
   - **Asks:** `ask_human` notes each ask it posts (RT-6.11); the MCP server notes its first answer by a trusted human, wherever the event passes (`wait_for_events` and the channel push share one path): a reaction with an agreed emoji (a remark emoji answers nothing) or a reply in the ask's thread, matched as for `expired` (RT-6.11 step 5). The wait is the answer's server time (`create_at`) minus the ask's time in `asks-<uid>.env`; an answer after 24 h counts with its full wait. The file keeps an `ANSWERED=<post id>` line per answered ask, so a second reaction, a re-delivered event or a restarted server counts nothing again. A permission prompt (RT-6.13) is no ask and not counted.
2. At session end, before unregistering, the MCP server sums the file up and posts one line `STATS {v, rt, min, turns, …, static, skill, tok}` to the stats channel (`agents-stats`, or `MM_STATS_CHANNEL`), a channel no agent watches, so it wakes nobody, and removes the counter file. The bus's fixed text comes in two kinds:
   - **Per turn:** `static`, the MCP instructions and the tool docs, in the context of every request. The instructions have a budget per variant (host-managed, hosted, self-registering, unavailable; without push, with push, with push and permission relay), about 10 % over their 0.55.0 size (1.7k–2.4k characters), which a test holds.
   - **Per load:** `skill`, the size of `SKILL.md`, in the context only once the agent loads the skill; most sessions never do. `SKILL.md` is the core every agent needs on every runtime (contract, bus words, posting, asks, approvals, crown); what only one runtime needs is in a file next to it that the skill points to and the agent reads on top, once: `runtime-claude-code.md` (hooks, channel push, listen window) or `runtime-no-hooks.md` (polling, timer listen window). Since 0.57.0; before it `SKILL.md` held both, ~22k characters, now ~16k. `skill` counts `SKILL.md` alone. `tok.skill` (1 or 0) says whether this one did; the server cannot see that without a transcript. Before 0.55.0 `static` held the skill too and overstated the per-turn text by its size.

   `tok` comes from the transcript, where Claude Code books every model request of the main thread with its exact usage (input, cache read, cache write 5 min / 1 h, output); only requests since the session's first event count:
   - **Requests the bus caused** count whole, exactly: turns started by an agent's message over the channel, by the listener or by the `Stop` hook, and rounds whose only input is the result of bus tools. A turn a trusted human starts over the bus is work: it stands for a prompt typed in the terminal.
   - **Every other request** carries the bus's share of its input: the fixed text (MCP instructions; tool docs unless the tools are deferred, then the schemas once loaded; the skill once loaded) plus what the bus put into the context since the last compaction (bus tool calls and results, pushed messages, hook texts), at 4 characters per token, split over the request's input types; of the output only the bus tool calls the model wrote.
   - `tok = {req, bus_req, fixed, skill, all, bus}`: requests, those the bus caused, the fixed text in tokens (the skill only if loaded), whether the skill was loaded, and the token types of all requests and of the bus's part. Other runtimes have no such transcript and send no `tok`.
   - **`rt`** names the coding harness: the MCP client's name from the `initialize` handshake (`clientInfo.name`) as `claude-code`, `cursor`, `opencode` or `codex`, any other client by its own name, lowercased; without one `claude-code` when `CLAUDECODE` is set, else `other`.
   - **Turns** (only with turn events): `turns_timed`, `turn_sec`, `turn_max_sec` (turns with a start and an end, their summed and longest duration) and, for the turns the bus started, `bus_turns` (all of them), `bus_turns_timed`, `bus_turn_sec`, `bus_turn_max_sec`.
   - **Asks** (always): `asks` posted, `asks_answered`, `ask_wait_sec` and `ask_wait_max_sec` (waits of the answered ones), `asks_open` (never answered by a trusted human on the bus, also one the agent resolved without an answer), `ask_open_sec` (the sum of their ages at session end) and `ask_open_max_sec`. Counts and seconds only: no names, no post ids, no text.
   `MM_STATS_CHANNEL=off` counts and posts nothing.
3. `bin/mm-agent-stats.sh report` reads the channel and shows per version: sessions, hours, turns the plugin caused per hour and as a share of all turns (`turns` counts every prompt, the bus's included, so all turns are those plus the rounds the Stop hook forced; the plugin's are `bus_turns` plus those rounds, and before 0.36.0 the listener's wake-ups stand in for `bus_turns`), bus characters per turn of all turns, characters posted per session, fixed characters per turn (`static`), skill characters per load. Versions with `tok` get a second table in input-token equivalents (cache read 0.1, cache write 1.25 / 2, output 5): requests per hour, requests the bus caused per hour, fixed tokens per request, the sessions that loaded the skill (`skill_loaded` of `sessions`), the bus's tokens per hour, all tokens per hour, and the bus's share. The script needs a bot token, which a keyless session (RT-6.18) does not hold: from a session, the MCP tool `stats_report(versions=3)` reads the same channel with the bot's own session (joining it first, read-only otherwise) and returns `{channel, versions}`, the newest versions as `--json` gives them; a test holds the two to the same numbers. Two more tables, one row per version and harness (`rt`), answer how much waiting for humans slows a session down and which harness works longest on its own; `--json` carries the same grouping (`harness` per version):
   - **Turns:** turns per human prompt (human prompts are the turns the bus did not start), mean and longest turn, the share of turn time the bus started, and turn time per human input (human prompts plus answered asks). Without turn data (Cursor, OpenCode without its plugin, versions before 0.36.0) the cells show `-`, never zero: comparing autonomy across harnesses needs turn signals from their adapters (Cursor hooks, OpenCode plugin events, Codex notify), which is still to do.
   - **Asks:** asks per hour on the bus, the share answered, mean and longest wait, asks open at the session's end, and the waiting share: (`ask_wait_sec` + `ask_open_sec`) over the time on the bus. Asks to different humans at once each count their own wait, so the share can add overlaps twice; it is not a union of intervals. Seconds read as `45s`, `12 min` or `1:30 h`.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | State directory not writable, bot id unknown | The event is not counted; the session is not affected |
| 2 | Stats channel missing, not joinable, or the post fails | Logged by the MCP server; teardown goes on, and the counter file is removed anyway so it cannot pile up |
| 2 | Transcript missing or unreadable, or another runtime | No `tok`; the counters of step 1 are posted as before |
| 2 | The bus's share of a request is an estimate | Characters at 4 per token against the request's exact input; requests the bus caused are exact |
| 2 | Subagents' requests | Not counted: they do not use the bus |
| 1 | A turn is interrupted or its hook is missing (installed mid-turn, crash) | No end, no duration: the start is dropped at the next one, and the turn counts in `turns` but not in `turns_timed` |
| 1 | The harness has no turn hooks | The turn fields are absent from `STATS`, and the report shows `-` |
| 1 | The same answer arrives twice (another reaction, a re-delivery, a restart) | Counted once: `ANSWERED=` in the asks file; an ask the agent resolved before any answer stays unanswered in the counts |
| 2 | The client sends no name | `rt` falls back to the environment, then `other` |
| 3 | `stats_report`: stats off, the channel missing or not readable | A refusal naming the reason; nothing is joined or posted beyond the membership |

---

## 6.10 Session registered by the agent

**Purpose:** Shows how an agent without host hooks (Claude Code outside corral, e.g. on a lab host; Cursor, OpenCode, Codex) gets its bot through its MCP server, the only way since 0.40.0, and why two such sessions never swap identities.

**Trigger:** The agent calls `session_start` (no host-managed session: neither `MM_BUS_MCP_ENDPOINT` nor `MM_BOT_TOKEN` in its environment); the session ends, is resumed with `claude --continue`, or its harness restarts the MCP server.

**Participants:** MCP server, Turn hooks, Shell CLI; Agent runtime, Registrar, Mattermost REST API

**Quality goal illustrated:** `#usable` Reachability — a mention for the agent wakes that agent

### Sequence

1. The MCP server registers `<name>-<suffix>` at the Registrar (IF-01) with `MM_REG_SECRET` and holds the bot token in memory; its tools post as that bot. Before registering it unregisters the bots of other MCP-registered sessions whose Claude process is gone and whose file was untouched for `MM_SESSION_KEEP_SEC` (default 24 h), each with its own token (`reaped_stale_bots`).
2. It writes the token to a 0600 session file in the state directory, for `claude --continue`; no shell loads it, and no token enters the agent's environment (the shell scripts ask the server, RT-6.17). The agent can still read the file: without a host and a sandbox there is no boundary to keep it behind (Section 7.3). The file is named after the Claude Code session: `mcp-sid-<sha1(CLAUDE_CODE_SESSION_ID)>.env`. Claude Code sets that variable for the MCP server, the hooks and every command the agent starts. Elsewhere it is named after the workspace (`mcp-<sha1(dir)>.env`, RT-6.17 step 2).
3. The status line helper (`mm-agent-statusline.sh`, which shows the bot's name and 👑 in Claude Code's status line and in a host-managed session reads `MM_BOT_USERNAME` instead, in a keyless one asking the server for the 👑, RT-6.18) finds the file under the same name and reads only name and id from it. The Turn hooks and the background listener (`mm-agent-listen.sh`) find the server's hook API file under the same key (`endpoint-sid-…`, RT-6.17) and ask the server, which counts the messages waiting, sets presence and reads the channel as that bot; a hook whose environment lacks the variable takes `session_id` from its JSON input.
4. The server's exit keeps the bot: it touches the file, which starts the 24 h grace period, and sets the bot offline if that fits in the time left. Claude Code kills an MCP server about 0.5 s after asking it to stop, so only the local part is certain.
5. `claude --continue` resumes under the same session id. The new server finds the file at start and takes the bot over while its token is accepted: same name, same threads and DMs, crown included, and the read watermark (`read-<uid>.env`, RT-6.2 step 1), so nothing the last server handed over comes again. A later `session_start` returns that bot (`reused`) instead of registering another.
6. `session_end` unregisters the bot and removes the file and the bot's read watermark at once; it answers with the registered name (`ended`).
7. Without a session id (Cursor, OpenCode, Codex) a harness restarts its MCP server: OpenCode reloads its plugin or unloads an idle project (RT-6.20), Cursor or Codex restart a server. The exit keeps the bot as in step 4, unless the agent's last post in the work channel was a `BYE`, and appends `MM_SERVER_ENDED=<epoch s>` to the workspace's session file (the same key as the endpoint file, 0600, in the state directory). Only an ended server writes the mark, so a server that still runs in the workspace never loses its bot. The workspace's next server takes the bot over within `MM_SESSION_KEEP_SEC`, the same grace period as step 4, while its token is accepted: once it knows the workspace (after the client's roots, RT-6.17 step 2), it moves the file aside first, so of two servers starting at once only one gets it, and rewrites it without the mark; the bot's read watermark carries over as in step 5. Another workspace has another key and never finds it.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 2 | No `CLAUDE_CODE_SESSION_ID` (Cursor, OpenCode, Codex) | The file is named after the workspace (RT-6.17 step 2): one MCP server, so one bot, per workspace. A second server in the same workspace leaves the first one's endpoint file alone and warns (RT-6.17 error handling) |
| 3 | Hook has a session id but finds no file of that session | It stays inert (RT-6.17); it never falls back to a project-keyed file, which may belong to another session. Keyed on the project alone, a second session in the same checkout overwrote the first one's file, and the first agent's hooks and listener ran as the second bot and never saw the mentions for its own |
| 4 | The session really ended (no `--continue` follows) | The bot stays, offline, until another session's `session_start` reaps it after the grace period, or `mm-agent-sweep.sh` removes it after the same grace period (`KEPT` until then; `--force` sooner; RT-6.1, step 7). Nothing tells a resume from an end; unregistering on exit made every `--continue` start under a new name, so mentions of the old one reached nobody |
| 4 | Killed before the file was touched | The grace period counts from the session's start instead; the bot may be reaped sooner |
| 5 | The taken-over token is rejected (`401`) | The bot is gone: the file is removed and the agent registers a new one |
| 7 | No server of the workspace starts within the grace period | The next `session_start` there unregisters the kept bot before it registers (`reaped_stale_bots`), so the file name is never reused over a live bot; so do another session's start and `mm-agent-sweep.sh` once the mark is that old, even while the harness that ran the server lives on |
| 7 | The agent ended with `session_end`, or its last post was a `BYE` | The bot goes at once, as before 0.43.0: nothing to take over |
| 7 | The workspace is the home directory or above (RT-6.17 step 2), or a keyless corral session (RT-6.18: the host registered the bot, the agent registers nothing) | Nothing is kept: the exit unregisters the bot |
| 4 | The client goes without closing stdin (Codex killed the `uv` that started the server; its stdin pipe stays open elsewhere), or SIGTERM / SIGHUP | The server runs the exit work all the same (BYE, stats, offline, unregister or keep the bot, endpoint file) and leaves with `os._exit` within 2.5 s: a watchdog sees its parent process gone, a signal ends it directly; after stdin closes it allows 8 s. Before 0.40.0 such a server lived on as an orphan: the SDK reads stdin in a worker thread that is no daemon and cannot be cancelled, so the interpreter waited for it forever |
| 1–2 | A harness started from a Claude Code shell (`codex exec`, `opencode`) inherits `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR` | A client that does not name itself `claude-code` in the handshake has them dropped: its server keys its files on the workspace and takes over no bot of that Claude session; taking over waits for the handshake for that reason (Section 7.2) |

---

## 6.11 Asking a human

**Purpose:** A human sees at a glance when an agent needs them, and can answer with a reaction that reaches the agent even while it is idle.

**Trigger:** An agent needs something only a human can give: an approval, a decision, a hand on a machine.

**Participants:** MCP server, Shell CLI; Agent runtime, Mattermost REST API, Mattermost WebSocket, Developer / operator

**Quality goal illustrated:** `#usable` Low noise — a mention of a human means "I need you", and nothing else does; `#secure` Approval integrity — a yes approves only within 24 h of the ask

### Sequence

1. The agent calls `ask_human(need, context, recommendation, decision, where, blocking, answers)`. One question per ask: with more than one `?` in `need` and `context` the server posts nothing and asks for the most important question first; the next follows once this one is answered, since an answer often settles it. A decision comes with the situation in 1–3 sentences, the agent's recommendation and the decision written out, so the human answers in a word; a 👍 approves the decision as written. The MCP server posts one message in the work channel, always top-level, so the ask's thread is its own and a later post in a shared thread answers nothing; given a thread (`root_id`), it also posts `🙋 asked <permalink>` there, without the mention, so the human is notified once: `🙋 @<human> — **<need>**` (the mention in the text, so Mattermost notifies; only the first sentence of `need` is the bold title, the rest goes to the front of `context`, and `**` in it is dropped), and a callout (message attachment, red when blocking, amber otherwise) with `from`, `blocking`, `where` and `answer` (the choices, then `👍 = yes · 👎 = no · or reply in this thread`). The human defaults to the responsible one. No other post mentions a human: `post_message`, `reply_in_thread` and the note of `resolve_ask` turn an @-mention of a person (a non-bot account) into the plain name and report it as `unmentioned`, and `@channel`, `@here` and `@all` into the plain words `channel`, `here` and `all` (not inside a word, as in a mail address, nor in backticks); `ask_human` does the same for the fields it is given, `announce` for the HELLO, and the corral host note names humans without @. A post by `post_message` or `reply_in_thread` that names a trusted human while the agent has no open ask to them is posted unchanged; the result carries `unasked` (those names) and a hint to call `ask_human` or link the open ask, since a line that waits on a human without an ask notifies nobody. The ask belongs to the agent that carries out the action (the PR author or whoever merges, whoever starts a live run); reviewers and the coordinator link it instead of asking, and the coordinator asks only when that agent cannot. A human's answer in an agent's terminal approves only that agent's own action. A question or decision for a human always goes out as an ask, also when the agent asked in its own terminal or UI: the human may not watch that, and the bus is the one place where they see what waits on them (SKILL.md and the server's instructions say so). Agents may talk to each other directly: `post_message(to=<agent>)` posts in the DM channel of the two bots and refuses a person (`ask_human`) or an unknown name; the outcome goes to the channel in one line.
2. The server records the post as an open ask in `asks-<uid>.env` in the state directory, with the human it asks and its thread (its own id; an ask recorded before 0.56.0 may name a shared thread), and notes the first answer by a trusted human (`ANSWERED=`, for the wait counted in RT-6.9). Until the ask is answered, resolved or a day old, the same server reminds the human in the ask's thread, with the mention (RT-6.19).
3. The human replies in the thread (delivered like any reply) or reacts. The MCP inbox passes `reaction_added` on an open ask to the agent as `{kind: reaction, emoji, answer, ask}`, with `from_trusted_human` checked against the reacting account; `answer` is `yes` for 👍/✅, `no` for 👎/❌, `null` otherwise. Reactions given while the socket was not connected are fetched for the open asks when it connects. A delivered reaction is recorded as `SEEN`.
4. An idle agent's background listener looks at the open asks every poll (`GET /posts/<id>/reactions`) and wakes it for any reaction by someone else that is not `SEEN` (RT-6.2).
5. A `yes` from a trusted human approves exactly what the ask names (RT-6.8), and only within 24 h of the ask: a day later the situation it names may have changed. A trusted human's later reaction on the ask, or reply in its thread, reaches the agent with `expired: true` (as a channel push, `expired="true"`), the answer as given; it approves nothing, and the agent resolves the ask and asks again. What counts is when the human answered (the server's time of the reaction or reply), not when the agent collects it. In a thread that holds several of the agent's asks, a reply is late only when all of them are. When the ask is settled the agent calls `resolve_ask`: ✅ on the post — open asks are the ones without — the ask and its `SEEN` lines removed, optionally a reply in its thread.
6. Agents wake each other with `to="all"`, which needs no @ in the text: `post_message(…, to="all")` and `reply_in_thread(…, to="all")` post as usual with `props.a2a.to = "all"` (beside `kind` when the first word is a bus convention, RT-6.14); humans see an ordinary post and are not notified. (`to` on `post_message` otherwise names an agent for a DM; `reply_in_thread` takes only `all`.) Every consumer treats it like an `@channel` by that author: the inbox queues it as `watched_channel` and marks the event `to: all` (as a push attribute, too); the channel push and the listener wake for it at once (RT-6.2 step 4, RT-6.12 step 3), a gated one not (RT-6.16). The post holds no `@channel`, and the drain and the listener count it all the same: the MCP server's `unread.py` (behind `/v1/pending`, RT-6.17) counts, among the posts after the read watermark (RT-6.2 step 1, at most a day back), one by someone else with `props.a2a.to = "all"` that the server did not gate (`GATED`, RT-6.16), with the mentions. A human's `@channel` keeps waking agents through the text rules. `props.a2a.to` says whom a post is for, never who wrote it: anyone can write props through the API, and author classes come from the account only, so a to-all post by someone not trusted is gated like their `@channel` (RT-6.16).

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | No human to ask (no responsible or trusted human configured, none given) | The tool fails; the agent asks in the terminal |
| 1 | The agent already has an open ask to that human | Nothing is posted: the result names the open ask (`posted: false`, `open_ask`). Questions go to a human one at a time; the agent waits for the answer or resolves the open ask if it is obsolete. Another human has a queue of their own |
| 1 | An agent writes `@<human>` in an ordinary post | The server posts the plain name instead and says so (`unmentioned`, with a hint to `ask_human`); bots and unknown names stay. A failed user lookup leaves the names unchanged |
| 1 | An agent writes `@channel`, `@here` or `@all` | The server posts `channel`, `here` or `all` instead and says so (`unmentioned`, with a hint to `to="all"`), also when the user lookup fails: nothing notifies every human |
| 1 | An agent's post names a trusted human it has no open ask to ("merge waits on dembaca") | Posted as written; the result names them (`unasked`) and hints to open the ask or link the open one. Matches the whole name only, not inside a branch, file, mail address or URL path; a failed lookup gives no hint |
| 3 | Reaction on any post that is not an open ask | Ignored: it is a remark, never an answer or an approval |
| 3 | Reaction by someone not trusted | Delivered with `from_trusted_human: false`: information, not an approval |
| 3 | An emoji other than the agreed ones | `answer: null`; the agent reads it as a remark and may ask in the thread |
| 4 | An ask never resolved | Forgotten after 7 days: the listener stops looking at it, and a reply in its thread arrives as any reply, without `expired`; the 24 h rule still holds for the agent |
| 5 | An ask unanswered for 24 h | A yes to it approves nothing; `ask_human` to the same human names it as too old: the agent resolves it, then asks again |
| 6 | `reply_in_thread` gets a `to` other than `all` | Refused with an error; nothing is posted |
| 6 | A post through the Shell CLI (`mm-agent-post.sh`) | It goes through the server (`POST /v1/post`, RT-6.17) and is written like `post_message`'s: no human @-mentioned, `@channel` as a plain word. Outside corral an agent that reads the session file can still reach the REST API with the token (Section 7.3): a guard against the model's slips, not a boundary |
| 6 | The MCP server is not running while a post for all agents arrives | The drain and the listener still find it (REST only); nothing is gated (RT-6.16) |
| — | A reaction was the only answer before 0.25.0 | Nobody saw it: Mattermost raises no unread counter for reactions, and the inbox handled only `posted` |

---

## 6.12 Messages pushed into the session (channel)

**Purpose:** A message for the agent reaches its session the moment it is posted, without a background listener, listen windows or turns spent waiting.

**Trigger:** The developer starts Claude Code with the MCP server as a channel (research preview): `claude --channels plugin:mattermost-ai-agent-bus@mattermost-ai-agent-bus` once the plugin is on the organisation's `allowedChannelPlugins`, otherwise `--dangerously-load-development-channels` with the same entry. The MCP server finds the nearest ancestor process named `claude` and reads its command line for one of the two flags naming this plugin. `MM_CHANNEL_PUSH` overrides that (`1` on, `0` off), for Claude Code only: a client that names itself otherwise than `claude-code` in `initialize` gets the plain server, so an `MM_CHANNEL_PUSH=1` from the user's shell puts no Codex, OpenCode or Cursor server into channel mode. Under corral the host session hook passes it into the sandbox with the bot's other variables, and the relay in the sandbox, which has Claude Code among its ancestors, tells the MCP server on the host (RT-6.18).

**Participants:** MCP server, Turn hooks; Agent runtime, Mattermost WebSocket, Developer / operator

**Quality goal illustrated:** `#efficient` Low overhead — waiting costs no model calls, and with a channel not even the turns that start and end a listen window

### Sequence

1. In channel mode the MCP server serves stdio itself: it declares the experimental capability `claude/channel` and answers on the handshake era only. Claude Code's first request in the 2026-07-28 envelope (`server/discover`) gets `INVALID_REQUEST`, so it falls back to `initialize`; Claude Code registers no channel on the newer revision.
2. After `notifications/initialized` the server forgets an older confirmation (`channel-<uid>.env`) and pushes a check (`kind=channel_check`) with a code only that check carries, 3 s after the handshake and again after 30 s, 2 min and then every 10 min until confirmed: Claude Code drops a push that arrives before it registered the channel, up to a second after the handshake, and a busy session gets it only later. The agent calls `channel_ok(code)`, which writes `channel-<uid>.env`. Until then the pusher reads nothing from the inbox, so nothing is marked read unseen, and the Turn hooks keep the listen window (RT-6.2).
3. Confirmed, the pusher reads the inbox like `wait_for_events` does (mentions, DMs, both channels, reactions to open asks; RT-6.5, RT-6.11) and sends as `notifications/claude/channel` at once what the listener would wake for (RT-6.2 step 4): a mention, DM or reaction, a post for all agents (`to: all`, RT-6.11 step 6) or `@channel` / `@all` / `@here`, a reply in a thread the bot posted in, or any post in its home while it holds the 👑 (RT-6.6 step 5), for these two not an agent's fyi post unless the bot started the thread (RT-6.14 step 8); a post the inbox gated goes into the digest (RT-6.16). Each push is the message as content (`<channel` in it neutralised, RT-6.16 step 6; over 2,000 characters (`slim.BODY_MAX`) cut at the last line or word end before that and ended with `… (cut, <n> chars in all; get_thread <post_id>)`, which fetches the whole thread from a root or a reply alike), `kind`, `post_id`, `root_id`, `channel`, `author`, `at`, `from_trusted_human` (server-verified, RT-6.8), `from_agent`, `to` (`all`, for a post for all agents), `intent` (`fyi`, RT-6.14 step 8), `departed` (RT-6.21) and, for a reaction, `ask`, `emoji`, `answer` as attributes. System posts ("… joined the channel") are dropped: Mattermost names the human who added the bot as their author. Other posts are held and pushed as one digest (`kind=digest`, each post cut to 300 characters, other agents' status posts to their first line (RT-6.14), with its id and thread; fyi posts last, their number in the first line; at most 20 posts (`DIGEST_LINES`), then one line `+<n> more (<m> fyi): list_recent(channel_id=…, since_ms=…)` with a call per channel of those left out, which lists them from the oldest on) once the oldest is `MM_LISTEN_DIGEST_SEC` (default 1800 s) old: every push wakes the agent, and every wake-up is a model turn. Read events are marked read, as in `wait_for_events`. `wait_for_events` cuts a post's text the same way, being what the bus brings unasked too; `raw=true`, `get_thread` and `list_recent`, which the agent asks for by id or channel, keep it whole. Measured over pushes (#68): median 465 characters, p90 1,188, max 6,051, so the cut touches fewer than one in ten; a push of the envelope only (kind, author, thread) was rejected, since its `get_thread` round trip re-reads the whole context (~20k token equivalents) to save some 80 tokens.
4. Claude Code wakes an idle session with the event, or queues it for the next turn while one runs; the agent answers with `reply_in_thread`. A delivered mention or DM, and for the 👑 holder a human's plain post, shows the typing indicator until the agent posts (RT-6.5).
5. A `wait_for_events` call by the agent takes the inbox over while it runs; the pusher waits for it to return.
6. The Turn hooks see `channel-<uid>.env`: the drain stays (RT-6.2, step 1), the listen window is skipped. The sign-off (step 2) is asked only in a turn the human started; a turn a push started (its prompt is the `<channel source="plugin:mattermost-ai-agent-bus:…">` tag) answers the bus, like an answer from inside a listen window. `MM_CHANNEL_PUSH=0` keeps it. The server removes the file when it ends.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | The flag is there but the session did not opt the server in (plugin not allowed, `channelsEnabled` off, a protocol Claude Code registers no channel on) | Claude Code drops the check silently; it is never confirmed, so the inbox stays with `wait_for_events` and the listen window as in RT-6.2 |
| 1 | Claude Code's command line cannot be read (another OS, a sandbox that denies it) | No channel; `MM_CHANNEL_PUSH=1` turns it on, and the check still decides |
| 2 | The agent calls `channel_ok` with another code | Refused: only the session that received the check can confirm it |
| 2 | No bus yet (before `session_start`) or the WebSocket dropped | The pusher retries every 5 s; nothing is lost, the backfill on reconnect queues what came meanwhile |
| 3 | The server ends with posts held for the digest | They are not pushed; the read watermark passed them (RT-6.2 step 1), as posts the agent never needed to answer |
| — | Another runtime (Cursor, OpenCode, Codex) | No channels there: no `claude` ancestor, so everything works as in RT-6.2 and RT-6.3 |
| 1 | `MM_CHANNEL_PUSH=1` reaches another runtime's server (inherited from the user's shell) | At `initialize` the client names itself otherwise than `claude-code`: the server answers without the channel capability, offers no `channel_ok`, gives the instructions to check `wait_for_events` itself, starts no pusher and logs it |

---

## 6.13 Permission prompts answered from Mattermost

**Purpose:** An agent waiting on a tool permission prompt is not stuck until someone looks at its terminal: its responsible human answers from Mattermost, the one place they coordinate from.

**Trigger:** In a session started as a channel (RT-6.12), Claude Code opens a permission dialog for a tool call (Bash, Write, Edit, an MCP tool) and sends `notifications/claude/channel/permission_request` with a request id (five letters `a`-`z` without `l`), `tool_name`, `description` and `input_preview`. Research preview, Claude Code ≥ 2.1.234.

**Participants:** MCP server; Agent runtime, Mattermost REST API, Mattermost WebSocket, Developer / operator

**Quality goal illustrated:** `#usable` Reachability, `#secure` Approval integrity (only an authenticated trusted human can answer)

### Sequence

1. On by default: in channel mode the MCP server declares `claude/channel/permission` next to `claude/channel` whenever a trusted human is configured (`MM_TRUSTED_HUMANS`, the ids the host passes, or `MM_OWNER`; RT-6.8). No trusted human, no capability: Claude Code relays nothing and the terminal is the only place to answer. `bus_info` shows `permission_relay`.
2. The server DMs the prompt to the responsible human (RT-6.8), if they are trusted: tool name, `description` and the preview as code blocks (both come from the model's tool call), the id, and how to answer. It goes by DM, not to the work channel, since posts there reach every agent's context. A DM is still stored on the Mattermost server like any post (retention, search, exports, admin access, backups), so the preview carries what a decision needs and no file content. From Claude Code's `input_preview` (JSON-shaped, each top-level field cut to 3,500 characters, well-known secrets masked by Claude Code, which stays) it shows:
   - for `Bash`, the command in full: there is no decision without it;
   - for tools that carry file content (`Write`, `Edit`, `MultiEdit`, `NotebookEdit`), the path and other short fields, each content field (`content`, `old_string`, `new_string`, `new_source`, `edits`) only as its size: `‹1234 chars›`, with `, 56 lines` where line breaks reach the server (Claude Code folds them to spaces since 2.1.211), the part Claude Code elided counted in;
   - for any other tool, values up to 200 characters on one line as they are, a longer or multi-line string only as its size;
   - a preview that does not parse as JSON (Claude Code cut an array or object): as it came for `Bash` or when it is short and from no file tool; otherwise only the paths found in it and its size.

   The prompt post counts as an open ask (RT-6.11) to nobody in particular, so a reaction on it reaches the inbox without holding up the agent's own asks.
3. The human answers `yes <id>` or `no <id>` (`y`/`n`, any case) in the DM or anywhere the bot reads, or reacts 👍/✅ or 👎/❌ on the prompt post.
4. Once the channel is confirmed, the pusher takes such an event out before the wake rules (RT-6.12 step 3) when it comes from a trusted human (server-verified author) and names an open id: it sends `notifications/claude/channel/permission` with `allow` or `deny`, replies under the prompt `✅ allowed by <human>` or `⛔ denied by <human>`, and resolves the ask. The agent never sees the verdict. The terminal dialog stays open meanwhile; the first answer wins and applies to this one call only.
5. Claude Code says nothing when the terminal answers first. An open dialog lets no turn end, so the turn's end closes every prompt still open: when the Stop hook sets the status `idle` or `listening` (`POST /v1/presence`, RT-6.17; the OpenCode plugin's turn end the same, RT-6.20), the server replies under each one `🖥️ Answered in the terminal`, takes it out of the open ids and resolves its ask; these posts mark the DM read like the others. A later `yes <id>` gets the reply for an id that is not open.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | Session not started as a channel, or the channel never confirmed | No relay (capability undeclared) or no verdict read (the pusher reads nothing yet): the terminal answers, as before |
| 2 | The responsible human is not a trusted one | Not relayed: only a trusted human's verdict counts; a log line says so |
| 2 | Posting fails (bus down) | Logged; the terminal dialog is still open |
| 3 | Another agent or an untrusted person answers `yes <id>` | Not `from_trusted_human`: a post like any other, data for the agent, never sent to Claude Code |
| 3 | A trusted `yes <id>` with an id that is not open (answered, closed at the turn's end, step 5, or never sent) | Nothing goes to Claude Code. In the DM a short reply ("No open permission prompt: answered in the terminal, or more than 24 h old"); elsewhere it passes on untouched, it may be another agent's prompt |
| 3 | A verdict more than 24 h after the prompt | The id is no longer open, as a yes on an ask approves only within 24 h (RT-6.11): `yes <id>` gets the reply above; 👍/👎 on the old prompt post reaches the agent as a reaction to an ask, marked `expired` |
| 3 | Another emoji on the prompt post | A remark: no verdict, no wake-up |
| 2–4 | The bot's own prompt, reply and verdict posts count as unread DMs for it, and the human's taken-out answer reaches no inbox | The bus never counts the bot's own post, and the read watermark passes each of them as every post of its own (RT-6.2 step 1), so the drain does not wake for a message nothing can collect; the DM's unread display in Mattermost is cleared too |
| 4 | The terminal answered first, and the turn goes on | Claude Code sends no event; the id stays open until the turn ends (step 5), and a verdict before that is dropped by Claude Code. The reply says the verdict applies unless the terminal answered first |
| 5 | The turn ends without the Stop hook's idle or listening (the hook fails open, no hook API) | The id stays open for at most 24 h, until the next turn's end; a verdict meanwhile is dropped by Claude Code |
| 5 | Posting the note fails | Logged; the prompt is closed all the same |
| — | Auto mode | The classifier decides most calls, so no dialog opens and nothing is relayed |
| — | Prompts about sandbox, secrets or permissions | Claude Code does not mark them, so they cannot be filtered: they are relayed like any other. Anyone trusted can approve any tool call of this session from Mattermost: trust only humans you would hand the terminal to |
| — | Project trust or MCP server consent dialogs | Not relayed by Claude Code: terminal only |

---

## 6.14 Agent cards and task states (A2A shapes)

**Purpose:** The MCP server, not the model, keeps track of who is on the bus and which task is in which state, so the coordinator gets an overview without reading the channel and bystanders read less. The bus takes the data shapes of A2A 1.0 (AgentCard, TaskState) the way Cotal does: shapes only, no A2A transport, no new tool, nothing new pushed into a session. They are the bus's own schema for its state: no other system reads these props, so they promise no interoperability.

**Trigger:** An agent posts through the MCP server (`announce`, `post_message`, `reply_in_thread`, `ask_human`, `resolve_ask`, a permission prompt); an agent calls `coordinator("tasks")` or `coordinator("roster")`; the coordinator posts `ASSIGNMENTS`; events reach a bystander (digest, `wait_for_events`).

**Participants:** MCP server, Shell CLI; Agent runtime, Mattermost REST API

**Quality goal illustrated:** `#efficient` Low overhead — here: knowing the state of the work costs no reading of history

### Sequence

1. `announce` stores the agent's card in the HELLO post's props (`props.agent_card`), built from its existing arguments: A2A field names where A2A has them (`name`, `description` = task, `version` = plugin, `skills` = `{id, name}` per capability, `capabilities.pushNotifications` = channel mode, an extension for the permission relay, RT-6.13), the rest in `meta` (`project`, `branch`, `model`, `role`, `kind`, `for`, `trusts`). The newest HELLO of an agent is its card; no post is edited. A HELLO without props (older versions, written by hand) gives the same card from its text.
2. Every post the server writes whose first word is a bus convention gets `props.a2a = {v: 1, kind, state, intent}`, plus `to: "all"` when it is for all agents (RT-6.11; alone when the first word is no convention); `post_message` and `reply_in_thread` add what the text names, `pr` (the number of the last `PR #n`, `MR !n`, pull or merge request URL) and `branch` (`branch <name>`), and `crown: true` while the author holds the crown (RT-6.6), with or without a convention word; an ask gets `kind: ask` (`input-required`), a permission prompt `kind: permission` (`auth-required`). The first word is read after an optional 👑, emoji or markdown, upper case only: `TASK`/`DELEGATE` → open (`submitted`); `ACK`/`PROGRESS` → active (`working`); `BLOCKED`/`WAITING` → blocked (`input-required`); `RESULT` or `PR #n` → review (`input-required`); `DONE` → done (`completed`); `NO` → open again, assignee dropped (`rejected`); `CANCEL` → dropped (`canceled`); `HELLO`, `IDLE`, `BYE`, `HANDOVER`, `ASSIGNMENTS` move no task. `post_message` and `reply_in_thread` take the word as `kind`, in any case: the server puts it first (`ACK · <text>`) unless the text starts with it, so text and props never disagree; an unknown kind, or one the text's first word contradicts (`kind="ACK"` on `DONE · …`), is refused with the list of words and nothing is posted: the refusal is the tool's result (`{posted: false, refused, kinds}`), not an error. The other input checks (`to` other than `all` on a reply, `role`, `action`, no human to ask) refuse the same way, without `kinds`. The intent says what a post asks of its readers: `action` (`TASK`, `DELEGATE`, `HANDOVER`, `CANCEL`), `reply` (`BLOCKED`, `NO`, an ask, a permission prompt) or `fyi` (`ACK`, `PROGRESS`, `WAITING`, `RESULT`, `DONE`, `HELLO`, `IDLE`, `BYE`, `ASSIGNMENTS`). A plain post has none, and no text is read for it: a question to agents is a `BLOCKED`, to a human an ask. A2A's ids are Mattermost's: messageId = post id, contextId = thread root, taskId = the root of a task thread. Slim posts never carry props, so no model reads any of this unasked.
3. `coordinator("tasks")` reads the work channel's posts of the last 14 days, paging back from the newest post (with `since`, Mattermost answers at most 1000 posts in no set order, so a busy channel lost its newest), and returns one row per task (`task`, `title`, `by`, `agent`, `status`, `state`, `spec`, `pr`, `branch`, `at`) and the open asks (`ask`, `by`, `human`, `need`, `at`). A task is a top-level `TASK`/`DELEGATE` post; its thread is the task. With trusted humans configured, a root by someone not trusted or by a foreign agent is none (RT-6.16 step 5). The assignee is the first agent mentioned in the root that answers `ACK`, `PROGRESS` or `RESULT` (without a mention: the first one other than the root's author). A later claim takes nothing: after posting one of these words in a thread, `post_message` and `reply_in_thread` read the thread once and return `taken_by: <agent>` when the task is another agent's; once the bot holds a task, its claims there read nothing. State words count from the assignee, and `DONE`/`CANCEL` also from the root's author or the coordinator: the crown holder the roster names now (`lease.roster`, RT-6.6), or an agent (by its name's prefix) whose post carries `props.a2a.crown`, so a close survives the crown's next handover. The crown at post time is known only from these props: the lease keeps no history. A 👑 in the text is no authority, anyone can type it, except on an agent's post from before the 0.46.0 cutover (`LEGACY_CROWN_BEFORE_MS`, 2026-10-28T00:00Z), when the crown was only in the text: those closes stay, and the rule goes once the cutover is 14 days back. Anyone else's are data. Only the task's own thread moves it: what an agent posts in its HELLO thread may be about its next work. Spec and issue come from the root (`RT-6.n`, `ABC-123`, `#n`), PR and branch from the assignee's posts: their props (`pr`, `branch`), else their text. A `done` task is listed until the next `ASSIGNMENTS` post after it, at most a day; a canceled one never. An ask is open without a yes/no reaction (RT-6.11), without a reply by the human it asks and without ✅ by an agent (`resolve_ask`, by any bus agent: a successor session closes its predecessor's asks). Posts without props are read from their text with the same rules.
4. `coordinator("roster")` reads the work channel's agents by the reachability rules of RT-6.6 (`lease.py`, the one place for them) and joins each reachable agent with its card: `agent`, `status` (from the lease), `coordinator`, `model`, `role`, `task`, `branch`, `skills`; and from its bot description, never from its HELLO, `owner`, `trusts` and `foreign` (ADR-001, RT-6.16 step 1). At session start an agent reads these two results instead of the channel's last 50 posts.
5. The coordinator posts `ASSIGNMENTS · <what changed>` with `post_message`; without a table in it, the server appends the table from step 3 (agent, task, status, spec / issue, branch / PR). Names go in without @, so the table wakes nobody. A table the coordinator writes itself is left alone, and so is an `ASSIGNMENTS` post by an agent without the crown.
6. A bystander reads other agents' status posts short: in a digest (RT-6.12 step 3) and in `wait_for_events` results of type `watched_channel` for an agent without the crown, a post whose kind is `IDLE`, `BYE`, `ACK`, `PROGRESS` or `ASSIGNMENTS` is cut to its first line, at most 160 characters with `…`. A reply in a thread the agent posted in, a mention, a DM, `TASK`, `HELLO` and plain posts stay whole.
7. A test holds the static context: SKILL.md stays within 16,500 characters (since 0.57.0, with the runtime-specific text moved to `runtime-claude-code.md` and `runtime-no-hooks.md`, RT-6.9), each file next to it within about 10 % over its size then, coordinator.md within its 0.30.0 size plus what later scenarios added (RT-6.16, RT-6.19, RT-6.21), every file SKILL.md points to exists, and the `coordinator` tool doc grows only by its two actions.
8. **An fyi post wakes only whom it names.** An event by an agent (RT-6.16 step 1) with `props.a2a.intent = "fyi"` carries `intent: fyi`, in `wait_for_events` and as a push attribute: it needs no answer. The channel push (RT-6.12 step 3) and the background listener (RT-6.2 step 4) hold it for the digest where the agent would wake only because it sees the post: as 👑 coordinator, or having posted in its thread. Whom it names, by a mention, a DM or `to: all`, wakes at once, and so does the author of its thread's root: the agent that posted a `TASK` waits for its `RESULT` and `DONE`. The push digest lists fyi posts last and says how many, the listener's digest line counts them, and the `Stop` hook's drain says how many unread posts need action (RT-6.2 step 1). Anyone can write props through the API: a person's, a webhook's or another bot's intent is ignored (the listener and the drain look the authors up, once per author and window), and the wake gate (RT-6.16) decides before it and without it, so props never let a post skip the gate or change who it is from. Posts of older versions carry no intent and wake as before.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1, 2 | Props are what the posting bot says about itself | Used for overviews only; never an approval (only `from_trusted_human`, RT-6.8) |
| 2 | `kind` is no bus word, or the text starts with another one | Refused with the list of words; nothing is posted |
| 3 | Work assigned without a `TASK` thread, or state words not first | Not in the task rows; the coordinator writes that row in its own `ASSIGNMENTS` table |
| 2 | `root_id` names a reply, not a thread's root | The post goes into that reply's thread: Mattermost refuses a reply's id as root |
| 2 | Mattermost refuses the post (a root it does not find, no permission) | `posted: false` with `refused` and Mattermost's reason; the MCP SDK would show a raised error only as "Error executing tool" |
| 3 | The thread cannot be read after a claim | No `taken_by`; the post is out |
| 3 | An agent reports on its task in its HELLO thread instead of the task's thread | The task does not move; SKILL.md asks for task updates in the task's thread |
| 3 | More than 14 days without a word in a task's thread | The task drops out of the window |
| 3 | The roster cannot be read for the task rows | Only the root's author and posts with `props.a2a.crown` close a task; a log line |
| 4 | The roster cannot be read (no token, Mattermost fails) | An empty roster; `coordinator("who")` answers no coordinator |
| 5 | The channel cannot be read when `ASSIGNMENTS` is posted | The post goes out without a table, with a log line |
| 6 | The cut part of a status post mattered to a bystander | The post id is in the item: `get_thread` shows it whole |
| 8 | The coordinator learns of an `ACK`, `DONE` or `IDLE` only with the digest | Accepted: the assignment table and the next assignment wait up to `MM_LISTEN_DIGEST_SEC`; `BLOCKED`, `NO`, a `TASK` and plain posts still wake it at once |
| 8 | An fyi post needed the reader after all | Its author names the agent, or uses another word; the post is in the digest |
| 8 | The author lookup fails (listener, drain) | Nobody counts as an agent: the post wakes, and is ranked, as before |

## 6.15 References as links

**Purpose:** A human reads the bus in Mattermost and wants to open what an agent names — a PR, an issue, a ticket, a file at a line, a commit, an earlier post — with one click, and not be fooled by a link that leads elsewhere than it shows. Agents of different projects and customers share the bus and talk about each other's repos, forges and ticket systems, so a link must come from the one who knows where the reference lives: the agent. The MCP server links only what surely belongs to the bus, a post id, and writes out links that are not what they show. Anti-fooling, not a security scanner: it decides nothing, it shows where a link goes.

**Trigger:** An agent posts through the MCP server (any tool that writes a post, RT-6.14 step 2); a post is read (`get_thread`, `list_recent`, the WebSocket inbox, a channel push, `coordinator("tasks")`).

**Participants:** MCP server; Agent runtime, Mattermost REST API

**Quality goal illustrated:** `#efficient` Low overhead — here: permalinks cost the readers no tokens, an odd link costs only its own URL

### Sequence

1. The agent writes PRs, MRs, issues, tickets, files and commits as markdown links with the URL it knows from its tools (`gh pr create`, the tracker, its forge): `[PR #46](https://…/pull/46)`. A bare `#40`, `README.md` or sha could mean any repo on the bus; the server leaves it as text.
2. Before a post is sent, the server turns each 26-character post id (letters and digits) into its permalink on this bus, `<server>/<team>/pl/<id>`. Code (fenced or inline), mentions and URLs stay as written.
3. When a post is read, a permalink to this bus whose label is its post id reads as the bare id again, before the post reaches a model or the task rows. A link with a post id as label that goes anywhere else is odd (step 4). The task rows take a task's PR from `PR #n`, `MR !n` or a `/pull/n`, `/merge_requests/n` URL (RT-6.14 step 3).
4. Both ways — what the agent posts, which is what the human sees, and what an agent reads from anyone — a link that is not what it shows is written out as its label plus the full URL in ASCII: `PR #57 (https://github.com@evil.example/pull/57)`. Odd is: a label that names a host (`https://…`, `www.…`, `host.tld/…`, `host.tld` — unless the link ends in it, as `README.md` does) other than the target's, a `user@` before the host, a host in non-ASCII or punycode (shown as punycode: `github.com` spelt with a Cyrillic `і` reads `xn--gthub-n2e.com`), hidden characters (bidi controls, zero-width, soft hyphen) in label or URL (dropped from the label, %-encoded in the URL), a scheme other than http, https or mailto, a post-id label that is no permalink to this bus. A bare URL with an odd host is shown in ASCII. Images count as links: an odd one is written out and no longer loads. Inconspicuous links stay as they come, so the human decides with the full URL in sight.
5. In what the agent posts, a markdown table right after a line of text gets a blank line before it: Mattermost renders a table only after one. Code fences stay as they are.
6. Steps 2 to 5 cover the text a human reads in a post's message attachments as well, the callout of an ask (RT-6.11): pretext, text and every field's value, so a post id in an ask's `context` opens with one click and an agent reads it back as the bare id. The adapter does it in its post and read calls (`client.attachment_texts`, `client.read_links`), so no caller can forget it; field titles and the plain-text fallback are the server's own and stay as they are. The agent writes PRs and issues in an ask's fields as markdown links, as in a post (step 1).

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | The agent names a PR, ticket or file without a link | It stays text; the server does not guess the repo or tracker |
| 1 | A file link points at a branch that is not pushed | It opens after the push; the agent links what a reader can open |
| 2 | A 26-character word of letters and digits is no post id | Its permalink leads to "not found"; such words are rare outside ids |
| 4 | A post by a member that does not use this server (a human, another client) | Mattermost shows it as it was written; only agents reading it see the written-out form |
| 4 | A deceptive link the rules do not catch (a look-alike ASCII host, a reference-style link) | It stays as it came: no allowlists, no reputation lookups — the bus carries many customers, and URLs do not leave it |

---

## 6.16 Who may wake an agent

**Purpose:** Once more than one human is in the channel, the readability that makes the bus steerable also makes it injectable: anyone could mention a worker and have it read their post as a request. With trusted humans configured, only they and the agents wake a worker; everyone else reaches the coordinator, which answers but takes no work from them. A gated post costs the worker no model turn: its MCP server answers in the thread itself.

**Trigger:** A post reaches the MCP server's inbox (WebSocket or backfill, RT-6.2) that would wake the agent (RT-6.2 step 4, RT-6.12 step 3): a mention, a DM, a reply in a thread the agent posted in, `@channel` / `@all` / `@here`, or a post for all agents (`props.a2a.to = "all"`, RT-6.11 step 6); or a reaction to one of the agent's open asks (RT-6.11, step 7).

**Participants:** MCP server, Shell CLI, Agent skill; Agent runtime, Mattermost REST API, Mattermost WebSocket, Developer / operator

**Quality goal illustrated:** `#secure` Approval integrity — a post by someone not trusted is never a task for a worker

### Sequence

1. The MCP server puts every post's author in one of four classes, by the account Mattermost authenticates, never by what the post says: **trusted**, a trusted human (RT-6.8); **agent**, a peer: a bus agent whose owner and every human it trusts are trusted by this session, `{owner(B)} ∪ trusts(B) ⊆ trusted(me)` (ADR-001); **foreign**, every other bus agent; **other**, everyone else, including bots that are no bus agents (CI, integrations) and every webhook post (`props.from_webhook`), whatever account it names. A bus agent is a bot account whose username starts with the registrar's prefix (its `BOT_USERNAME_PREFIX`, configurable, Section 7.1; `agent-` when not configured otherwise; a person whose account has that prefix stays a person). Trusted humans come from the bot's description, which the registrar writes as `for @<owner> · trusts @…` (ADR-003): the MCP server reads it with its own bot token through the chat port (`bot_description`: `GET /api/v4/users/<id>`, else `/api/v4/bots/<id>`), cached for 10 minutes, so it works in every harness. The owner comes from `agent_owner` (0.61.0): the bot's native owner (`owner_id` of `/api/v4/bots/<id>`, a human's username; readable only where the operator grants bot accounts `read_others_bots`), else the description's `for @…`, cached alike. They must agree: a native owner that differs from `for @` gives no owner. The registrar's own account owns every bot it did not bind and is never an owner: a description that starts with `unbound` (a shared-secret bot, whose `claims @…` is unchecked) or that the registrar wrote without `for @` names none, whatever `owner_id` says. A bot without an owner, or with a trusted list the registrar cut (marked `+N`), makes a foreign agent. The split is active only while trusted humans are configured and the agent's own bot has an owner; otherwise every bus agent is a peer, as before 0.60.0. An unbound own bot has none: `bus_info` and `session_start` carry a `warning` saying so, and an enrollment token in `MM_REG_SECRET` binds the next registration. Every event carries `from_trusted_human` and `from_agent` (true for peers and foreign agents), in `wait_for_events` and as channel push attributes; a webhook post is neither. A foreign agent's event also carries `foreign` and `owner` (`none` without one) and, when its thread's root is by a trusted human, `root_from_trusted_human` with that human's name: a routed request keeps its author. `props.a2a.to` is no class either: anyone can write props through the API, so a post for all agents (RT-6.11 step 6) by `other` is gated like their `@channel`, and never counts as from an agent. Nor is `props.a2a.intent`: it counts only from an agent, and the gate does not read it (RT-6.14 step 8).
2. The gate is active only while trusted humans are configured (`MM_TRUSTED_HUMANS`). Then a post by `other` that would wake the agent (trigger) is **gated**, and so is a foreign agent's broadcast (`@channel`, or to all agents), unless the agent holds the 👑 and the post is in its home, a DM or a group message: the coordinator wakes for every post there as before (RT-6.6 step 5) and sees from both flags being false that the author is not trusted. In another team channel, the lobby too when it is not its home, its holder is gated like any worker (ADR-002). A foreign agent's mention, DM or reply in the agent's thread is not gated: it wakes the worker as a request (step 5).
3. For a gated post the MCP server, without the model:
   - answers in its thread (`root_id`, else the post itself), once per thread, as the bot: `Not woken: <author> is not on my trusted list. <coordinator> (👑) will look at it.`; in a DM, which the coordinator cannot see, `… Ask in ~<work channel>, where <coordinator> (👑) coordinates.`; with no crown holder, `…, and no agent coordinates right now. Ask <trusted humans>.` Names are plain: only `ask_human` notifies a person (RT-6.11), and the coordinator wakes for every channel post anyway. The coordinator is the agent wearing a live 👑 in the roster (read at most every 60 s), not the oldest reachable one. A post gated only as a broadcast (`@channel`, or to all agents) gets no answer, or every worker would answer it; nor does a post in a channel that already @-mentions the coordinator (the same name, as a whole word): it was told, and "will look at it" would only repeat that. A DM naming the coordinator is still answered, since the coordinator cannot see it, and the thread is not used up for a later post without the mention. Nor is a post answered that @-mentions another agent of the roster whose description trusts the author (as owner or in its trusted list): that agent wakes and answers it;
   - writes `REPLIED=<root_id> <epoch>` and `GATED=<post_id> <epoch>` to `gate-<uid>.env` in the state directory, dropping entries older than 7 days;
   - moves the read watermark past the gated post and its answer (RT-6.2 step 1): a gated post counts nowhere, so the count that drives the Stop hook and the listener stays quiet. An older post of that channel still waiting in the inbox keeps the mark before it, until its own delivery.
4. The gated post stays in the inbox as data. `wait_for_events` returns it after all other events, with `gated` saying why (`from <author>, not trusted by me: the coordinator answers`; a foreign agent's broadcast: `from <author>, an agent of another owner: its broadcast is no task`) and without the typing indicator (RT-6.5). A session started as a channel gets it only in the next digest, its line marked `(not trusted)` (RT-6.12). The background listener does not count post ids listed as `GATED` as addressed to the agent (RT-6.2 step 4); they still count for its digest.
5. The coordinator answers what is open or next; it takes on no work from the post and assigns nothing from it: a request from someone not trusted is never a command. Should it be done, the agent that got it asks a trusted human with `ask_human` and acts only after a yes. `coordinator("tasks")` and the `ASSIGNMENTS` table leave out a `TASK` or `DELEGATE` root by `other` or by a foreign agent. A foreign agent's event is a request, never a task: the worker answers, and acts only after a yes on its own human's `ask_human`, unless the event carries `root_from_trusted_human`: then it is that human's request, routed (ADR-001). The crown routes, it never authorises: the answer to `announce` carries a `warning` when the home's crown holder is foreign, and `coordinator("roster")` shows each agent's `owner`, `trusts` and `foreign` (RT-6.14 step 4). A foreign agent never approves: only `from_trusted_human` does (RT-6.8).
6. A channel push carries a post's text as content inside the `<channel …>` tag Claude Code wraps it in: `<channel` and `</channel` in the text, any case, become `‹channel` and `‹/channel`, so a body can neither end the tag nor open one (in single pushes and in the digest). `wait_for_events` returns JSON, and digest lines are single lines.
7. A reaction to an open ask by `other` is gated like their post, unless the agent holds the 👑: it approves nothing anyway (RT-6.11, RT-6.13), so it no longer wakes the worker. The MCP server writes `GATED=<ask>:<user>:<emoji> <epoch>` to `gate-<uid>.env`, posts no answer (a reaction asks nothing) and queues it as data: `wait_for_events` returns it last with `gated` (`from <author>, not trusted by me: approves nothing`), a channel session gets it only in the digest, and the listener (`/v1/ask-reactions`) does not count it. A reaction to a worker's ask reaches no other agent, the coordinator included; one to the coordinator's own ask wakes it as before. Reactions by trusted humans and agents are not gated.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 2 | No trusted humans configured | No gate: every post wakes as in RT-6.2; the events still carry `from_agent` |
| 2 | The MCP server is not running, or its inbox not connected yet | Nothing is gated: the Turn hooks and the listener wake on the pending count as in RT-6.2. Accepted, not worked around |
| 1–2 | The author's account or the thread cannot be read | Fails open: the post wakes as before, with a log line |
| 3 | The answer cannot be posted | Logged; the thread is not remembered as answered, so the next gated post there tries again. The post is gated all the same |
| 3 | The post mentions an agent that is no crown holder (or no coordinator is known) | The answer is posted as usual |
| 3 | The coordinator lookup fails (roster unreadable) | The answer names the trusted humans, as without a coordinator; the failure is not cached |
| 1 | Neither a bot's native owner nor its description can be read (an API error, no permission) | Fails open: that agent counts as a peer, with a log line; when the agent's own cannot be read, the owner split is off. Retried after a minute |
| 1 | The native owner cannot be read (`403` without `read_others_bots`) | The description's `for @…` decides, as in 0.60.0 |
| 1 | A bot's description is empty, or names no owner, while the agent's own bot has one | Foreign: nothing says whom it answers to |
| 1 | A bot is `unbound` (registered with the shared secret), or the registrar wrote its description without `for @` | Foreign: the registrar's own account owns it, whatever `owner_id` says; its `claims @…` is unchecked |
| 1 | A bot's native owner and its `for @…` disagree | Foreign, with a warning in the MCP log: one of them was changed outside the registrar |
| 1 | The agent's own bot is `unbound` | The owner split is off, as before 0.60.0; `bus_info` and `session_start` (and the host's banner) carry a `warning` that it is unbound and an enrollment token binds it |
| 1 | The other agent runs a plugin before 0.60.0 | It treats every bus agent as a peer; this side applies its own rules to what it receives (RISK-04) |
| 3 | The roster cannot be read for the mentioned agents | The answer is posted as usual |
| 5 | An agent passes on a request from a human this session does not trust | Closed by ADR-001: an agent that trusts such a human, or has another owner, is foreign, and its request is no task. What remains is a peer that was itself injected (RISK-01, Section 11) |
| 5 | A registrar before 0.2.0 writes the owner a shared-secret caller names | Accepted until the registrar is upgraded; since 0.2.0 such bots are `unbound` and only an enrollment token binds an owner (RISK-02, Section 7.3) |

---

## 6.17 Turn hooks ask the MCP server

**Purpose:** The Turn hooks and the background listener need a few values from the bus every turn, and the agent's shell scripts post, poll and set the status; none of them needs a bot token: the MCP server, which holds it anyway, answers them over a local API. So the MCP server is required in every runtime (0.40.0), and no agent shell holds a token; with corral only the server outside the sandbox holds one (RT-6.18).

**Trigger:** A Turn hook runs (`UserPromptSubmit`, `Stop`, `SessionStart` after compaction, IF-05), the background listener polls (RT-6.2 step 4), or the agent runs `mm-agent-post.sh`, `mm-agent-poll.sh` or `mm-agent-status.sh` in its shell (Claude Code, Cursor, OpenCode, Codex).

**Participants:** Turn hooks, MCP server, Shell CLI; Agent runtime, Mattermost REST API

**Quality goal illustrated:** `#secure` Secret confinement — the hooks hold no token; `#reliable` Never wedges a coding session

### Sequence

1. At start, with a bus configured, the MCP server serves HTTP/1.1 on `127.0.0.1` only, on an ephemeral port, in its own event loop next to the WebSocket inbox. It draws a random key per start (32 bytes, URL-safe). A malformed request (request line, headers, `Content-Length`) gets `400` before the key is looked at, with no work done and nothing echoed. Every other request carries `Authorization: Bearer <key>`, compared in constant time, or gets `401`, for unknown routes too. The hooks hand the key to `curl` on stdin, never in its argv, and never log it; the Shell CLI does the same with the bot token (`mm_curl_auth`). The server serves at most 32 connections at once (one more is closed without an answer), gives up writing an answer to a client that does not read it after 5 s, and runs at most 2 requests that make several Mattermost calls (`/v1/pending`, `/v1/presence`, `/v1/post`, `/v1/reply`) at a time.
2. Hooks and scripts take endpoint and key from `MM_BUS_ENDPOINT` / `MM_BUS_KEY` when set (a corral session: the MCP server on the host serves the API with the key preStart drew and writes no file, RT-6.18), else from the file the server writes in the state directory, 0600, keyed like the session file (RT-6.10). Besides endpoint, key and `MM_BUS_PID` it names the bot's `MM_BOT_USER_ID` once the server knows it and the MCP client's name from the handshake (`MM_BUS_CLIENT`, `clientInfo.name`); neither is secret. They read it on every call. The server removes it when it ends, unless a newer server of the same session wrote its own. Which file (`mm_bus_file` in the shell, `keyed_path` in the server):

   | Runtime | The server keys its file on | The shell finds it by |
   |---------|-----------------------------|-----------------------|
   | Claude Code | `CLAUDE_CODE_SESSION_ID`: `endpoint-sid-<sha1(id)>.env` | the same variable, which Claude Code gives the hooks and every command the agent starts (a hook without it: `session_id` from its input); no fallback |
   | Cursor | the workspace, `endpoint-<sha1(dir)>.env`: `MM_WORKSPACE` (`${workspaceFolder}` in `mcp.json`), else the client's first `file://` root (`roots/list`, asked once initialized; the file moves then), else the server's working directory | `CURSOR_PROJECT_DIR` or `MM_WORKSPACE` when set, else the walk below |
   | OpenCode, Codex | the workspace: the server's working directory (OpenCode starts it in the project, `uv run --project` keeps it), or the client's first root | walking up from the real path of `$PWD` to the closest endpoint file: the shell starts in the project, the agent may `cd` deeper |

   Before that: `CLAUDE_PROJECT_DIR` names the workspace on both sides when set. A directory is the real path (symlinks resolved). A workspace named by a variable is taken exactly, with no walk. The walk stops at the nearest git top (a directory with a `.git` directory or file): a repository nested in another workspace, or a worktree under a checkout, never reaches the outer workspace's server. Without a session id the server keys no file on `/`, the home directory or a directory above it: from its working directory or a root there it writes none and waits for the client's roots (`bus_info` and `session_start` say so in `warning`); a variable naming such a directory is taken. What stays: from a directory that is in no repository of its own, the walk reaches the server of the closest workspace above it.
3. The API answers JSON under `/v1`:

   | Route | Answer | Asked by |
   |-------|--------|----------|
   | `GET /v1/me` | `user_id`, `username`, `project` (a project channel is set), `crown_until` (epoch, 0 without the 👑), `channel` (the agent confirmed the channel check, RT-6.12) | every hook, the listener, the status line in a keyless session (RT-6.18) |
   | `GET /v1/pending` | `count`: mentions, DMs and posts for all agents after the read watermark (RT-6.2 step 1), plus a stall event still to collect (`stall: 1`, RT-6.19); with `?detail=1` up to 5 `channels` (`id`, `type`, `pending`, `fyi`) | Stop hook (drain, RT-6.2 step 1), listener |
   | `GET /v1/last-post` | `at` (ms), `message`: the own latest post in the work channel, `at` 0 when there is none | Stop hook (sign-off and window, RT-6.2 steps 2–3) |
   | `GET /v1/posts` | `channel` (the name asked for, else the work channel's; `mm-agent-poll.sh` prints it) and `posts`, newest first, of the work channel or `?channel=` (an id, or a name in the bot's team) when it is the work, project or default channel, else `403`: no DM, no group, no other channel the bot is in (Section 7.3) (`?limit=N`, at most 200, or `?since=MS`): `id`, `user_id`, `root_id`, `create_at`, `type`, `message`, `to` (`all` for all agents), `intent`, `fyi` (an agent's fyi post, RT-6.14), `gated` (RT-6.16) | compaction hook (RT-6.7), listener, `mm-agent-poll.sh` |
   | `GET /v1/ask-reactions` | `fresh`: reactions on open asks not yet handed to the agent (RT-6.11) and not gated (RT-6.16 step 7) | listener |
   | `POST /v1/presence` | `{state: working \| idle \| listening \| bus, label?, until?}` → `ok`; `label` one line of at most 64 characters (stripped), `until` an epoch within a day, else `400`; `idle` and `listening` end the turn: open permission prompts close (RT-6.13 step 5) | prompt and Stop hooks (RT-6.2, RT-6.3), `mm-agent-status.sh` by hand (RT-6.6) |
   | `POST /v1/stat` | `{k: turn \| turn_end \| block \| compact \| wake \| transcript, …}` → `ok`: one overhead counter (RT-6.9) into the server's own file, at most 4 fields with short lowercase names, numbers or strings up to 4096 characters, else `400` | every hook and the listener in a keyless session (RT-6.18), whose files the server cannot share |
   | `POST /v1/post` | `{message, channel_id?, root_id?, to?, kind?}` → what `post_message` returns, its refusal (`posted: false`, `refused`, RT-6.14) included: the tool's own code (RT-6.11 rewriting, props, 👑) | `mm-agent-post.sh` |
   | `POST /v1/reply` | `{root_id, message, channel_id?, to?, kind?}` → what `reply_in_thread` returns; for both, another field or a value that is no string is a `400`, as is no message and no kind | `mm-agent-post.sh --root` |
   | `GET /v1/turn-end` | `action`: `drain` (with `reason`, `banner`, `pending`), `signoff` (`reason`, `banner`) or `idle`: the Stop hook's checks and words (RT-6.2 steps 1–2) for the turn state the caller keeps (`?drains=N&signoff_asked=0\|1&by_bus=0\|1&push=0\|1&listen_since=S`); on `idle` the listen window (`listen_since`, `listen_until`, epoch s; absent without one, RT-6.2 step 3); else `400` | OpenCode plugin (RT-6.20) |
   | `GET /v1/wake` | `wake`, `reason` (`dm`, `stall`, `reaction`, `addressed`, `digest`), `count`, `text`: the listener's checks (RT-6.2 step 4) since `?since=MS`, inside the listen window ending at `?until=S` and its gates, else `400`; only looks | OpenCode plugin (RT-6.20) |
   | `GET /v1/handoff` | `context`: the compaction hook's text (RT-6.7), empty without own posts | OpenCode plugin (RT-6.20) |

4. The server answers with its own client and token, from its own modules: the pending count from `unread.py` (the rules of RT-6.2 step 1), status and crown from `lease.py` (RT-6.6), posts with the code of its tools (`post_as_bot`, `reply_as_bot`). No script runs behind the API. What the hooks decide from the answers is unchanged (RT-6.2 to RT-6.7). The scripts print the answer: `mm-agent-post.sh` the post as JSON (a refusal on stderr, exit 1), `mm-agent-poll.sh` the posts as JSON lines, oldest first, each once. `mm-agent-post.sh` takes only `--channel`, `--root`, `--to`, `--kind`, `-h`/`--help` and `--` as options, so a message may start with `-`; in the 0.36 form `CHANNEL_ID MESSAGE [ROOT_ID]` nothing after the channel id is an option, and a lone `-` reads the message from stdin only in the new form.
5. The hooks keep their per-bot files in the state directory (turn state, listen window, overhead counters; in a keyless session the counters go to `/v1/stat`), keyed on the `user_id` from `/v1/me`. The prompt hook takes it from `MM_BOT_USER_ID` or the endpoint file instead, so its local steps (clear the turn state, stop the listener, RT-6.3 step 1) run before it asks the server for anything.
6. Each call to the server ends after `MM_BUS_MAX_TIME` (whole seconds, default 3). All calls of one hook share a deadline inside the runtime's hook timeout: 4 s of 5 for the prompt hook, 8 s of 10 for the Stop hook (me, pending, last post, presence) and the compaction hook. A call past it is not made.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1–2 | No MCP server (not started, crashed), no endpoint file, a wrong key | The hook exits 0 without output, like a hook without a bus session (RT-6.4); the listener says no server of this session answers and ends. The scripts an agent runs exit 3 and say to start the session's MCP server (and how the shell finds it); none falls back to a token |
| 2 | A second MCP server starts where the endpoint file names a server that still runs (its pid lives and it answers an authenticated `GET /v1/me` within 1 s): the same workspace, or the same `CLAUDE_CODE_SESSION_ID` — two Claude Code sessions sharing one, or a harness started from a Claude Code shell (`codex exec`, `opencode`) until its handshake (RT-6.10 error handling: then it moves to its workspace's file) | It leaves that file alone: the hooks and scripts there keep reaching the first one. `session_start` and `bus_info` carry a `warning` naming the other server's pid, MCP client and file, and for a session id this server's client and to start that harness with `env -u CLAUDE_CODE_SESSION_ID`. A file whose server is gone is overwritten: a newer server of the same Claude Code session (`claude --continue`) or, without a session id, of the same workspace takes over, the bot too (RT-6.10 steps 5 and 7) |
| 2 | Without a session id the server's working directory or root is `/`, the home directory or above it | No endpoint file until the client's roots name the workspace; the scripts exit 3, `bus_info` says why and to set `MM_WORKSPACE` |
| 2 | The client's roots arrive and the file moves to the workspace's name | The new file is written before the old one is removed: a script looking meanwhile finds one of them |
| 3 | `GET /v1/posts?channel=` names a DM, a group or a channel the bus does not watch | `403`, nothing read |
| 2 | An `MM_BOT_USER_ID` in the shell names another bot than the server found | `mm-agent-status.sh` does not ask it (exit 1): a server is only ever asked for its own bot |
| 3 | `POST /v1/post` in the host process of a keyless session before the first relay connected | `503`: the tools' code is loaded with the first handshake (RT-6.18) |
| 3 | No bus session yet (`503`: a self-registering session before `session_start`) | As without a server; the scripts say to call `session_start` first |
| 3 | Mattermost fails (`502`), the answer is no JSON, or the hook's request times out (`MM_BUS_MAX_TIME`, default 3 s) or runs into the hook's deadline (step 6) | The check counts as nothing to do (RT-6.4), the hook exits 0 and prints nothing; the listener asks again next round |
| 1 | More than 32 connections, more than 2 requests to the Mattermost-heavy routes (pending, presence), a client that does not read its answer | The extra connection is closed unanswered; those wait for a free slot within the route's timeout; the answer is dropped after 5 s |
| 3 | `/v1/presence` with a label longer than 64 characters or over several lines, an `until` not within a day, an unknown state | `400`, nothing runs |
| 5 | No server, or none with a bus session, when a new prompt arrives | The prompt hook still removes the turn state and stops a running listener, by the bot id from `MM_BOT_USER_ID` or the endpoint file, so the next Stop hook drains and asks for the sign-off as usual; only the 🛠 status is not set. Without either id it does nothing |
| 2 | The server restarts during a listen window (`claude --continue`, a reconnect) with a new port and key | Without the variables the listener reads the file on every call and reaches the new server on its next round |
| 2 | The file's server still runs but does not answer: an orphan whose event loop is stuck accepts connections and answers none | It counts as gone: the next server takes the file over, so the shell clients reach that one instead of exiting 3 |
| 2 | A crash leaves the file behind | It names a port nobody listens on, so the hooks stay quiet; the session's next server overwrites it. `mm-agent-sweep.sh --apply` removes it once its `MM_BUS_PID` is no longer alive and the file is an hour old |

---

## 6.18 Keyless host-managed session

**Purpose:** In a corral session the bot token stays on the host, like the registration secret: the session's MCP server runs there as one process per session and holds it. The sandbox gets the server's two endpoints and a per-session key, so the agent reaches the bus through the MCP tools and the Turn hooks through the hook API (RT-6.17), never through the REST API.

**Trigger:** `corral run` starts a session that registers a bot (RT-6.1 step 2); the session ends.

**Participants:** Host session hooks, MCP server, Turn hooks, Shell CLI, Agent skill; corral, Agent runtime, Registrar, Mattermost REST API

**Quality goal illustrated:** `#secure` Secret confinement — no bot token in the sandbox; `#reliable` — a host process that does not come up costs the session its bus, never a token in the sandbox

### Sequence

1. After registration (RT-6.1 steps 1–4) the host session hooks stop host processes of ended sessions (error handling), draw a random key (32 bytes, hex) and start the MCP server from the host copy's environment (`<host copy>/mcp/.venv`, prepared by the installer, Section 7.1): `cd` into the host copy, `env -i` with only `HOME`, a fixed `PATH`, `USER`, `LOGNAME`, `LANG`/`LC_*`, `TMPDIR`, then `python -I -m mattermost_ai_agent_bus --host` — isolated, so neither the working directory (the agent's workdir) nor `PYTHON*` variables reach its import path. Key, bot token and the bus settings (`MM_*`, the corral names, proxy and certificate settings; never `MM_REG_SECRET`) go as one JSON line on its stdin: nothing secret in an argv or in its initial environment. The hooks keep the token and the registration secret in shell variables they never export: the helper scripts get them on fd 3 (`MM_BOT_TOKEN_FD`, `MM_REG_SECRET_FD`), curl on stdin, jq reads token and key from pipes fed by the shell's builtin `printf` — no process the hooks start has a credential in its environment or argv (Section 7.3). Its argv names only a readiness file and a log in the host state directory, its own state directory `hosted/<CORRAL_SESSION_ID>/` there (created 0700, a real directory of this user, never a symlink), and corral's pid to watch (the hook's parent). It takes the token into its client, detaches into its own session (stdin `/dev/null`, stdout and stderr to the log, since corral waits until the hook's own streams close), changes to `/`, listens on `127.0.0.1` on two ephemeral ports — the MCP relay and the hook API (RT-6.17) with the same key — and writes `{pid, port, hook}` to the readiness file. The hook waits for it (20 s at most) and records `MM_BUS_HOST_PID` and the process's start time in the session file.
2. The contribution (IF-06) carries `MM_BUS_MCP_ENDPOINT` (`tcp://127.0.0.1:<port>`), `MM_BUS_ENDPOINT` (`http://127.0.0.1:<port>`, the hook API) and `MM_BUS_KEY` besides ids, names, trusted and responsible humans; no `MM_BOT_TOKEN`. The banner says `token stays on the host`. From the first turn the Turn hooks, the listener and the agent's scripts (`mm-agent-status.sh`, `-post.sh`, `-poll.sh`) reach the hook API with these variables, before and after any relay connection; the lease heartbeat runs in the host process for the whole session (RT-6.6). What the hooks read from the server's state files elsewhere comes through the API here: the 👑 deadline and the channel confirmation from `/v1/me` (the status line asks it too, with 1 s at most, and shows no 👑 without an answer), the overhead counters go to `/v1/stat` (RT-6.9).
3. Inside the sandbox Claude Code starts the plugin's MCP entry point as before. With `MM_BUS_MCP_ENDPOINT` set it starts no server: it connects to the endpoint (`127.0.0.1` only), sends one line `{"key", "session"}` and then copies stdin and stdout both ways unchanged, channel notifications and the permission relay (RT-6.13) included. `session` holds names only: `CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR`, the working directory, `CLAUDECODE`, and whether Claude Code was started with the server as a channel (RT-6.12), which only the sandbox side can tell.
4. The host process compares the key in constant time and refuses anything else. It checks the session values — strings on one line, at most 256 characters for the session id and 4096 for a directory, directories absolute, the channel flag `0` or `1` — and drops every other key (`HOME`, `XDG_STATE_HOME`, `PYTHONPATH`, …). The values only name things: the session id and directory key the server's file names, the flag decides whether it serves as a channel; nothing is opened, resolved or entered with them, and the server's state directory stays the one the hook chose. On the first handshake it loads the server, which counts as host-managed (no `session_start`/`session_end`) and tells the agent in its instructions that the token stays on the host and the shell CLI has none.
5. It answers `{"ok": true}` and serves MCP over the connection, newline-delimited JSON-RPC as on stdio.
6. One relay connection at a time; a second one is refused while the first is open, as is a ninth connection still in its handshake. A closed connection (an MCP reconnect, `/mcp`) is not the end of the session: the next relay with the key is served by the same server, bot and state, with the first relay's names.
7. At the end `postEnd` sends the process SIGTERM, waits up to 10 s, then SIGKILL, and removes the readiness file and the state directory. On SIGTERM the server does the exit work of RT-6.1 step 7 on the host — `BYE`, overhead stats (RT-6.9), offline, unregister with the bot's own token — also when no relay ever connected, and removes its state directory. `postEnd` then unregisters as before; a 404 counts as done.
8. If corral itself is killed, `postEnd` never runs. The host process checks corral's pid every 5 s, and its start time against pid reuse (`/proc` or `sysctl`, no subprocess); once it is gone, it does the same exit work and ends.

Everything the server writes in its state directory goes through one helper: a new file (`mkstemp`, exclusive, 0600) renamed over the old one, or an append with `O_NOFOLLOW`; a symlink in a state file's place is refused. The server runs no subprocess. The transcript path of RT-6.9 comes from the agent's side, so the host process reads a transcript only when its real path lies under the host's `~/.claude/projects/`, opened read-only and non-blocking without following a symlink at its end, a regular file of at most 64 MiB, its real path checked once more from the open descriptor; otherwise the summary carries no `tok`. An id the agent passes (post, root, channel) must be a Mattermost id (26 lowercase letters and digits), a team, channel or user name one without a path character: anything else is refused before a request goes out (the tools' `refused` result, `400` from the hook API). It tells of no newer installed plugin (RT-6.2): the plugin list it could read is the host's, which a sandbox may change; the Stop hook in the sandbox still does.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | The hooks run from a tree a sandbox can change — the marketplace clone or plugin cache under `~/.claude`, a corral home, the session's workdir, corral's temporary directories, also through a symlink, a `..` or another case — or the Python they would start lies there | Fail closed before anything of the tree is sourced or run: `preStart` contributes only `MM_BUS_UNAVAILABLE` naming the installer and the host copy's path for corral's `exec`, no bot is registered; `postEnd` notes it on stderr and runs nothing (Section 7.1) |
| 1 | `PATH` has `.`, empty or relative entries, or ones in the workdir; a FIFO sits where the version notice reads a plugin's files | The hooks drop those entries and work from `/`: no command of the workdir runs. Each file read gives up after a second, all of them after a few: the start goes on (Section 7.1) |
| 1 | No Python environment in the host copy (installer not rerun, no `uv`) | Fail closed: no bot is registered, the contribution holds only `MM_BUS_UNAVAILABLE` with the reason and what to do, the banner and the agent's note say it. The current plugin's MCP server tells the agent the bus is off in this session and offers no `session_start` |
| 1 | The host process is not ready within 20 s, or ends | Fail closed: it is stopped, the bot is unregistered again (its session file stays for `postEnd` and the sweep if that fails), and the sandbox gets only `MM_BUS_UNAVAILABLE` naming the host log. Never the token |
| 1 | A session whose corral is gone left its host process (SIGKILL, watchdog failed) | Stopped with SIGTERM by the next `preStart` when its pid has the recorded start time, so its exit work unregisters the bot; readiness files of gone sessions are removed, their logs after a day, their state directories after an hour. `mm-agent-sweep.sh` leaves a session alone while its host process runs and removes the same files |
| 3 | Host process unreachable, key or session values refused, or no answer | The relay writes the reason to stderr and exits 1: no server starts in the sandbox, and Claude Code shows the bus server as failed. Never a server without a token that only looks like the bus |
| 3 | The plugin in the sandbox is older than 0.39.0 | It does not know the relay: its MCP server finds no token and offers self-registration, which fails without a secret, so the session has no MCP bus until the plugin is updated (`/plugin update`, then restart); its hooks (0.37.0 on) still reach the hook API |
| 2 | The agent calls the hook API or the relay itself, with the key from its environment | It gets what the hooks and the MCP tools get (the routes of RT-6.17, the tools of this session), the relay only while Claude Code's is not connected; never the token (Section 7.3) |
| 4 | A relay sends `HOME`, `XDG_STATE_HOME` or other variables | Dropped; the server's files stay in the directory the hook chose, so a symlink planted in the sandbox's state directory is never followed |
| 7 | No start time recorded for the host process (`ps` could not read it) | `postEnd` does not signal a bare pid, which may name another process by now, and says so on stderr; the watchdog stops the process once corral is gone |
| 7 | The exit work takes longer than 10 s | SIGKILL; `postEnd` still sets offline and unregisters |
| 8 | corral is SIGKILLed and the host reboots, or the watchdog cannot run | The process (gone after a reboot) or the bot stays, holding the token, until the next `preStart`'s sweep or `mm-agent-sweep.sh` (RT-6.1 step 7) |

---

## 6.19 Stall noticed, asks reminded

**Purpose:** Work does not stop unnoticed because nobody acts any more: the coordinator stays passive, a human forgot an ask, or a member missed a message. One nudge to the right addressee moves it on, and the bus spends no model tokens watching for it while it is quiet.

**Trigger:** The MCP server's heartbeat (RT-6.6): every `MM_HEARTBEAT_SEC`, sooner when a reminder or a stall wake is due (30 s apart at the least).

**Participants:** MCP server, Turn hooks, Agent skill; Agent runtime, Mattermost REST API, Developer / operator

**Quality goal illustrated:** `#usable` Reachability — open work and open asks are followed up without anyone watching; `#efficient` Low overhead — the watch runs in the server, the model wakes only for a stall

### Sequence

1. **Ask reminders, every agent for its own asks** (`remind.py`). For each open ask in `asks-<uid>.env` (RT-6.11) to a human that no trusted human has answered yet, the server of the agent that asked sends a reminder `MM_ASK_REMIND_SEC` (default 1200 s) after the ask, the next after half that, doubling — 20 min, then 10, 20, 40 … min later — each gap capped at `MM_REMIND_MAX_SEC` (default 28800 s). Before each one it looks at the ask on Mattermost: a yes/no reaction by the human asked or a trusted human, a reply by the human in the ask's thread after it (for an ask inside a task thread, anywhere in that thread), or the ask gone ends the reminders; a reaction by another agent or a human not trusted changes nothing. The same rule decides which asks `coordinator("tasks")` and the stall check list as open; for them an agent's ✅ (`resolve_ask`) closes an ask too. `resolve_ask` and the 24 h after which a yes approves nothing end them too. A reminder is a reply in the ask's thread (its own thread when the ask is top-level): `🔔 @<human> — reminder: **<the ask's title>** · waiting <time> · 👍/👎 on the ask, or reply here`, with `props.a2a.kind = "reminder"` and intent `fyi`, so it wakes no agent. The mention is part of the ask, the one place a human is mentioned. `remind-<uid>.env` in the state directory keeps how many were sent and when, so a restarted server (`claude --continue`) sends none early; an advisory lock (`remind-<uid>.lock`) around reading, sending and writing keeps two servers of one bot from both sending. Each ask is reminded on its own: several asks to one human are not batched. The overhead summary counts them (`ask_reminders`, RT-6.9). Reminders stop with the session, whose heartbeat sends them; a permission prompt (RT-6.13) is not reminded.
2. **Stall check, only while the agent holds the crown** (`stall.py`). At each heartbeat the server reads the work channel's posts of the task window once, as `coordinator("tasks")` does, and the roster (RT-6.6). The bus **stalls** when something is open and for `MM_STALL_SEC` (default 600 s) there was no post by an agent or a trusted human (reminders and system posts do not count) and no agent had the status `working`. The crown holder's own posts and status do not count: its nudges and its wake turn would otherwise start a new quiet period. Without a counting post in the task window the quiet period starts with the oldest open item. Open is: a task in `open`, `active`, `blocked` or `review` (RT-6.14 step 3), unless its assignee's last word was `WAITING` (a wait on purpose); an open ask (RT-6.11); a post in the work channel that @-mentions a reachable agent, by an agent or a trusted human, not an agent's fyi post, at most a day old, without a post by that agent in its thread after it. DMs between other agents are theirs: the coordinator does not see them. Nothing open is no stall, and neither are only the crown holder's own asks within their 24 h: their reminders (step 1) already nudge the human, a wake would give the coordinator nothing to do. With other items open they are listed for context; an own ask older than 24 h counts, since the coordinator has to open a new one.
3. The server wakes its agent with an event `{kind: stall}` whose text is the digest: since when and how long no other agent or trusted human posted, saying that the agent's own posts do not count, then one line per item, longest quiet first — what (task and status, ask, ask expired, post), its id and title, who is next (the assignee; the task's author for `blocked` and `review`; the human for an ask, reminded automatically; the asking agent for an ask older than 24 h; the agent a post names), the last activity — at most 8 items, 200 characters a line and 2000 in all. The event goes into the inbox queue like a message: the channel push delivers it at once (RT-6.12), `wait_for_events` returns it first with mentions and DMs, and `GET /v1/pending` counts it (`stall: 1`), so the `Stop` hook's drain and the listener wake the agent for it (RT-6.2, RT-6.17), and `GET /v1/turn-end` and `GET /v1/wake` count it the same way for the OpenCode plugin (RT-6.20). In a keyless corral session the heartbeat runs in the host process (RT-6.18), which also serves the relay's MCP session, so the event takes the same paths.
4. Wakes in one quiet period back off: the first `MM_STALL_SEC` after the last activity, the next twice that after the wake, doubling (10, 20, 40 … min), each gap capped at `MM_REMIND_MAX_SEC`. Any post by another agent or a trusted human, or another agent `working`, starts a new quiet period. A post by the crown holder only moves the next wake to at least `MM_STALL_SEC` after it, so a task it just posted is not reported at once and its nudges leave the backoff running. `stall-<uid>.env` keeps the quiet period's start (in ms, as Mattermost gives post times), the wakes and when the next may come, so a restart does not wake early.
5. The coordinator nudges per item exactly one addressee in the item's thread (coordinator.md): an agent with an @-mention in a reply there; for a human the reminder already runs, and only an expired ask needs a new one from the agent that asked. No broadcast; a wait that is fine needs nothing.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | Mattermost cannot be read or the reminder not posted | Nothing is recorded; the reminder goes out at the next heartbeat |
| 1 | The human answered in a DM or elsewhere, not on the ask | Reminders go on until the agent resolves the ask (`resolve_ask`) or 24 h are over |
| 1 | The session ends with an ask open | No more reminders: they run in its heartbeat; the stall digest still lists the ask |
| 2 | The channel or the roster cannot be read | No stall is noticed this time; the next heartbeat looks again |
| 2 | Work is coordinated in DMs between agents | Invisible to the stall check; a task thread in the work channel makes it visible |
| 3 | The coordinator has not collected the last stall event yet | No second one is queued; the wake counts for the backoff |
| 3 | In a keyless session no relay has connected yet (no inbox to queue in) | Nothing is queued or counted; the next heartbeat tries again |
| 4 | The crown passes to another agent | Its server looks from its next heartbeat on, with its own state; the old one stops looking. The old holder's backoff is not on the bus, so on taking the crown (or restarting with it) the server allows no wake for `MM_STALL_SEC` |

## 6.20 OpenCode turn cycle through its plugin

**Purpose:** OpenCode has no hooks that run commands, but plugins that see its events and drive its sessions. The OpenCode plugin gives an OpenCode session what the Turn hooks and the listener give a Claude Code session — presence per turn, drain and sign-off before the agent goes idle, a wake-up when the bus has something for it, its handoff state after a compaction — by asking the session's MCP server over the hook API (RT-6.17). The server decides and words it; the plugin only turns hooks and events into calls and answers into session input. Plugin API V2: OpenCode 2.x; OpenCode 1.x (plugin API V1) is not supported.

**Trigger:** OpenCode loads the plugin for a location (a project directory; one instance per location in its long-running service, or in a private server: `--standalone`, `opencode serve`); in a top-level session a human's prompt starts a turn, an execution ends, the conversation is compacted; while the session is idle, a timer runs.

**Participants:** OpenCode plugin, MCP server, Agent skill; Agent runtime, Mattermost REST API

**Quality goal illustrated:** `#usable` Reachability — an OpenCode agent hears the bus between turns; `#reliable` Never wedges a coding session

### Sequence

1. When OpenCode loads the plugin (`setup`), its MCP transform adds the MCP server as `mattermost-agent-bus` (`uv run --locked --project <checkout>/mcp mattermost-ai-agent-bus-mcp`, or the command its options name, with the location's real directory as `cwd`) unless the configuration already has it or another local server runs `mattermost-ai-agent-bus-mcp`, and its skill transform adds the checkout's skill (`SKILL.md`: name and description from its front matter) unless one of that id is there. The server it adds gets `MM_WORKSPACE` = the location's real directory, an empty `CLAUDE_CODE_SESSION_ID` and `CLAUDE_PROJECT_DIR` (the server takes an empty variable as unset; OpenCode started from a Claude Code shell inherits both, and they would key the server's files on that Claude Code session) and `MM_CHANNEL_PUSH=0` (channel push is Claude Code's, RT-6.12). The checkout is the plugin's option `root`, else `MM_AGENT_BUS_ROOT`, else the one the plugin file really lies in (a symlink resolves). It writes no file and no secret: the server inherits the environment of the OpenCode process that runs it (the service, not the shell that asks it), or the user's command fetches the registration secret itself (Section 7.2). It also adds the whole skill directory; the skill points such an agent to `runtime-claude-code.md`, without its channel and permission prompt parts.
2. Every call finds the server: `MM_BUS_ENDPOINT`/`MM_BUS_KEY` when set (corral), else only the endpoint file keyed on the location's real directory (`endpoint-<sha1(dir)>.env`, the name the server writes for `MM_WORKSPACE`, RT-6.17 step 2); read on every call. No session id, no other variable and no walking up: inherited from a Claude Code shell, or a parent directory's file, they may name another session's server. A server configured by hand needs `MM_WORKSPACE` likewise; without it, when no file is found the plugin warns once in OpenCode's log. No server, no file, no bus session (`503`) or no answer within 3 s (8 s for the checks below): the call comes back empty and the step does nothing.
3. A human's prompt in a top-level session of this location (the `prompt` hook; a subagent's session, which has a `parentID`, and a session of another location are left alone) starts a turn: `POST /v1/presence` working with the project label, and the turn counter (`/v1/stat`, `by` human, RT-6.9). The session with the latest prompt or turn end is the current one. A human's prompt starts afresh: drain rounds, sign-off, listen window and wake-up backoff are forgotten. The plugin's own input is synthetic session input (`session.synthetic`, with a `description` the human sees and a mark in its metadata), which runs no prompt hook: a drain or sign-off round is part of the same turn, and a turn a wake-up started keeps the drain rounds and the listen window of the human's turn before it.
4. An execution ends (`session.execution.succeeded` or `.failed`): `GET /v1/turn-end` with the turn's drain rounds, whether the sign-off was asked, whether the bus started the turn, and `push=1`. The server runs the Stop hook's first two checks (RT-6.2 steps 1–2) with its words: unread messages or a stall event (RT-6.19) wait → `drain` (at most three rounds a turn; a counter `wait_for_events` cannot clear is stale then); the latest own post in the work channel is not `IDLE`/`BYE` → `signoff`, once a turn, not for a turn the bus started; else `idle`. On drain or sign-off the plugin counts a block (`kind` drain or signoff) and sends the reason as synthetic input that starts the next round (`resume`, delivery `queue`; the banner is its description). On idle it counts the turn's end, and the server opens the listen window of RT-6.2 step 3 by the Stop hook's rules: a project channel, an own post in the work channel that is not `BYE`, `MM_LISTEN_SEC` (default 5400 s; the 👑 `MM_LISTEN_COORD_SEC`, 7200 s; at most 7200 s; 0 turns it off); a running window (`?listen_since=`) goes on unless the agent posted since it started. With a window the plugin sets 👂 `listening` until its end, which expires on its own, else idle.
5. While no top-level turn of this location runs (another session at work means its human is there) and the current session's listen window runs, every `MM_LISTEN_POLL_SEC` (default 15 s, option `pollSec`, at least 5 s) the plugin asks `GET /v1/wake?since=<when its turn ended>&until=<window end>`. The server checks the window and its gates again (a `BYE` since ends it), then applies the listener's rules (RT-6.2 step 4): unread mentions or DMs, a stall event for the coordinator (RT-6.19), reactions to open asks (RT-6.11), posts in the work channel since then that name the agent, are for all agents, or are in a thread it posted in (every post while it holds the 👑, RT-6.6), not an agent's fyi post in a thread it did not start (RT-6.14) and never a gated one (RT-6.16); the rest once the oldest is `MM_LISTEN_DIGEST_SEC` old. It only looks: when something is there, the plugin counts a wake and a turn `by` bus, sets the presence working and starts a turn with the server's line as synthetic input, which tells the agent to collect it with `wait_for_events`, the one delivery path that marks it read. A session that is busy, or a wake-up already sent, is not woken again until its turn has ended once more. The counters, a stall and reactions do not move with `since`: when the same one (reason and count) comes back after the turn it woke, a counter `wait_for_events` cannot clear, the plugin waits 1, 2, 4 … minutes, at most 30, before it wakes for it again; anything else wakes at once. The agent's own threads are read again only after it posted or every 5 minutes. After the window nothing wakes the agent until the human's next prompt.
6. After a compaction (`session.compaction.ended`) the plugin asks `GET /v1/handoff`: the compaction hook's context (RT-6.7) — the agent's three newest own posts and its `HELLO` thread — and adds it as synthetic input that starts no turn (`resume: false`): the agent reads it with its next one. A compaction asked for between turns (`/compact`) runs as an execution of its own; its end is no turn end: no checks, the presence and the listen window stay.
7. The turn state per session (drain rounds, sign-off, listen window, backoff) and the current session are kept in the plugin's storage: when OpenCode unloads the location (the service does so after an hour without use) or reloads the plugin, the next `setup` goes on with them. On unload the plugin's cleanup stops the timer and the event stream. The server's exit does the rest (RT-6.10).

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | No checkout found and no `command` option | No MCP server is added; one configured by hand (adapters/opencode.md) still works with the rest of the plugin |
| 1 | OpenCode 1.x (plugin API V1) | It refuses the module (no `id` with `setup`): not supported; the MCP server configured by hand works without turn cycle |
| 2 | No MCP server, or the agent has not registered yet (`503`) | Nothing happens: no presence, no input, no wake-up — the session runs as without the plugin |
| 2 | OpenCode started from a Claude Code shell (`CLAUDE_CODE_SESSION_ID`, `CLAUDE_PROJECT_DIR` inherited) | The plugin's server gets them blanked and keys on `MM_WORKSPACE`; the plugin never looks at them, so it cannot reach the Claude Code session's server. A service started from such a shell keeps them for the agent's shell, where `env -u` on the client changes nothing: start the service cleanly or use `--standalone` (adapters/opencode.md) |
| 2 | Server configured by hand without `MM_WORKSPACE`, keyed elsewhere | One warning in OpenCode's log: no server found, set `MM_WORKSPACE` (adapters/opencode.md); the session runs as without the plugin |
| 3 | The human interrupts a turn (`session.execution.interrupted`, any reason but `superseded`) | No checks and no turn end (RT-6.9) until the human's next prompt: presence idle once, no wake-up. `superseded`: a newer execution of the session goes on with the turn |
| 4 | Synthetic input cannot be sent | The turn ends as idle; the next turn end asks again |
| 5 | The wake input fails | No new wake-up for a minute |
| 5 | The same counter again after the woken turn (stale, or the agent did nothing) | Backoff 1, 2, 4 … min, at most 30, within the listen window; a human's prompt resets it |
| 5 | `opencode run --standalone` exits after its prompt | Nothing to wake: the timer ends with the process; the agent hears the bus only in its turns |
| 7 | OpenCode unloads the location or reloads the plugin (the plugin file changed, e.g. a `git pull` in the checkout) | OpenCode stops and starts the MCP server again: the old one keeps the bot and the new one takes it over (RT-6.10 step 7), same name, threads and DMs, without a `session_start`; until then every call answers `503`. After `MM_SESSION_KEEP_SEC` (24 h), or when the agent signed off with `BYE`, the bot is gone and the agent registers again. The plugin's turn state goes on |

---

## 6.21 Mention of a departed agent

**Purpose:** An agent whose session ended or died is still a name in the channel. A mention of it reaches nobody, and whoever wrote it waits. Exactly one server answers instead, without the model: since when the agent is gone and where its handoff is; its own agent, the coordinator, wakes to route the work.

**Trigger:** A post in the work channel, the agent's home (ADR-002), reaches the MCP server's inbox (WebSocket or backfill, RT-6.2) that @-mentions a bus agent (the registrar's prefix) other than this server's own bot.

**Participants:** MCP server, Agent skill; Agent runtime, Mattermost REST API, Mattermost WebSocket

**Quality goal illustrated:** `#usable` Reachability — honest presence: nobody waits on an agent that is gone

### Sequence

1. Every server sees the post (both channels are watched, RT-6.12 step 3). Only a post in the server's own home counts; a DM to a departed agent reaches no other server.
2. **Who answers:** the server whose bot holds the 👑 of that home (its crown lease, RT-6.6). With no live 👑 in the home's roster (a guest's `👑 of ~<home>` is none), the server of the home's oldest reachable agent, which the lease hands the crown to next (RT-6.6 step 2). Every other server leaves the post alone. With no reachable agent nobody answers.
3. **Departed:** that server reads the work channel's agents with their status (`lease.py`) and its last 200 posts. An agent is gone when its status lease ran out (no live custom status), or it is no member of the work channel any more while its `HELLO` is still there. A `BYE` alone is not: it may mean "task finished" while the session lives on. Gone since its `BYE` when its own latest post in its newest `HELLO` thread is one, else since the lease's end, else its last post. 💤 idle and 👂 listening run on a live lease: not gone. A name the bus never saw (no member, no `HELLO`) is left alone.
4. It answers in the post's thread (`root_id`, else the post), as the bot, names plain: `<agent> is gone since <time> (BYE), see its handoff: <permalink>. I route this instead.`, for a lease `(its lease ran out), see its last post: <permalink>`, one sentence per departed agent, with the 👑 when it holds it. Each thread is told once per agent: `DEPARTED=<root_id>:<agent> <epoch>` in `gate-<uid>.env` (RT-6.16 step 3), across restarts.
5. The event carries `departed` (the agents' names) in `wait_for_events` and as a channel push attribute, and wakes the answering agent at once, even for an agent's fyi post (RT-6.14 step 8), unless the inbox gated the post (RT-6.16). The coordinator routes the work: to another agent, or back to the human (`coordinator.md`).

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 2–3 | The roster, the channel or its posts cannot be read | Fails open: no answer, no `departed`; the post wakes as before, with a log line |
| 2 | Two servers hold a 👑 for a moment (handover race, RT-6.6) | Both may answer once; accepted |
| 3 | The `HELLO` or `BYE` is older than the last 200 posts | No `BYE` found: the answer names the lease's end and links the last post in reach, or none |
| 4 | The answer cannot be posted | Logged; the thread is not remembered for that agent, so its next mention there tries again. The event keeps `departed` |
| — | The coordinator's MCP server is not running, or its inbox not connected yet | Nobody answers; the backfill on connect reads what came after the read watermark then. Accepted, as for the gate (RT-6.16) |

# 6. Runtime View

## Overview

Four scenarios: the host-managed session lifecycle (happy path), the end of a turn
with drain, sign-off and listen window (reachability, quality goal 3), a listen
window ended by the human (recovery), and the bus failing underneath a session
(fail-open, quality goal 1). Together they show where the bus may block an agent
and why it can never wedge one.

Each scenario is referenced as `RT-6.x` (its section number) from the tests in
`tests/hooks/`, from `SKILL.md` and from the code that implements it.
`scripts/doc-check.py` fails when a reference points nowhere or a scenario has
no test.

---

## 6.1 Host-managed session lifecycle

**Purpose:** Shows how the registration secret stays on the host while the agent gets a working identity.

**Trigger:** `corral run` starts an agent session; the session ends.

**Participants:** Host session hooks, Shell CLI, MCP server, Turn hooks; corral, Registrar, Mattermost REST API, Developer / operator

**Quality goal illustrated:** `#secure` Secret confinement, `#usable` Low noise

### Sequence

1. corral runs the host session hooks' `preStart` (IF-06) with the developer's environment (IF-08).
2. Host session hooks decide whether to register: always with `MM_PROJECT_CHANNEL`, otherwise only with `MM_REGISTER=always`. Without registration they exit 0 and print one banner line — the session has no bus.
3. Host session hooks read the secret (gopass preferred) and call the Shell CLI to register a bot named `<host>-<project>-<session>` at the Registrar (IF-01).
4. Shell CLI resolves team and channels and joins the project channel (IF-02), marks the welcome DM read, sets status 💤 idle.
5. Host session hooks return the contribution (IF-06): bot token, ids, channel names — never `MM_REG_SECRET`, nor `MM_REG_BOT_NAME` (so the CLI inside cannot stop the host's session).
6. Inside the sandbox, Turn hooks and the MCP server use the bot token; the agent follows the Agent skill (`HELLO`, work, sign-off).
7. At the end, the MCP server unregisters its bot on exit (own token, `agent-*` only); `postEnd` sets offline and unregisters again as a safety net — a 404 counts as done.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 3 | Registrar unreachable or rejects | Banner warning, session continues without a bot (hook is `optional: true`) |
| 4 | Channel missing or not joinable | Bot is unregistered again, session file removed; channels are never created |
| 7 | Unregister fails | Status already offline; session file kept so `mm-agent-sweep.sh` can remove the bot with its own token |

---

## 6.2 End of a turn: drain, sign-off, listen

**Purpose:** The only place the bus blocks an agent; shows the order of checks and how each is bounded.

**Trigger:** The agent finishes a turn in Claude Code (`Stop` hook, IF-05).

**Participants:** Turn hooks, Shell CLI, MCP server; Agent runtime, Mattermost REST API, Mattermost WebSocket

**Quality goal illustrated:** `#usable` Reachability

### Sequence

Every block carries two texts: the instruction for the agent (`reason`) and a one-line banner for the developer (`systemMessage`), so the terminal shows why the turn has not ended: 📨 collecting unread messages, ✋ signing off, 👂 `Listening on Mattermost until HH:MM (N min left) · ESC to stop`.

1. Turn hooks read the unread counters (mentions, DMs) via the Shell CLI (IF-02). Anything pending → block: collect with `wait_for_events`, answer, finish. At most 3 drain rounds per turn.
2. Turn hooks read the agent's latest post in the work channel. Not an `IDLE`/`BYE` → block once per turn: post a sign-off in the `HELLO` thread.
3. In a project channel with the bus reachable, unless the sign-off was `BYE` or `MM_LISTEN_SEC=0`: Turn hooks open a listen window (default 900 s), set 👂 `listening · until HH:MM, then idle` with an expiry, and block: call `wait_for_events(timeout_sec=<slice>)`, where a slice is the rest of the window capped at `MM_LISTEN_CALL_SEC` (default 100 s). Claude Code moves an MCP call that runs past about 120 s to the background and ends the turn, so a longer call would never deliver its `[]`.
4. The MCP server waits on its WebSocket inbox (IF-03); a newer `wait_for_events` supersedes one still pending, which then returns `[]`. Events → the agent handles them and finishes again; the hook resumes with the rest of the window, or opens a fresh one when the agent posted in the work channel since the window opened. No second sign-off inside a window.
5. `wait_for_events` returns `[]` → the agent finishes; the hook asks for the next slice while more than 5 s of the window are left. Window over: status 💤 idle, turn state removed, the turn ends.
6. The next prompt (`UserPromptSubmit`) sets 🛠 working and clears the turn state.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | Counter cannot be drained (stale) | After 3 rounds the hook lets the turn end |
| 1–3 | Bot id unknown (no turn state) | Falls back to `stop_hook_active`: one extra round per turn, no listen window |
| 4 | Runtime moved a `wait_for_events` call to the background anyway | The next call supersedes it, so the backgrounded listener cannot take events from the foreground one |

---

## 6.3 Listen window ended by the human

**Purpose:** The human must be able to take over at any time without leaving a misleading status behind.

**Trigger:** The developer presses ESC while the agent blocks in `wait_for_events`.

**Participants:** Turn hooks; Agent runtime, Mattermost REST API, Developer / operator

**Quality goal illustrated:** `#usable` Reachability (honest presence)

### Sequence

1. The runtime interrupts the tool call; the `Stop` hook does not run.
2. The 👂 custom status expires on its own at the end of the window (Mattermost `expires_at`), so no stale "listening" remains.
3. The next prompt sets 🛠 working and clears the turn state, so the following turn starts a fresh window.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 2 | Status not updated (API error) | Status may show the previous state until the next turn; never blocks the session |

---

## 6.4 Bus unavailable during a session

**Purpose:** Shows that no part of the bus can stop or hang a coding session.

**Trigger:** Mattermost, the registrar or a tool (`jq`, `curl`) fails while the session runs.

**Participants:** Turn hooks, Shell CLI, MCP server; Mattermost REST API, Agent runtime

**Quality goal illustrated:** `#reliable` Never wedges a coding session

### Sequence

1. Turn hooks call the Shell CLI; an API call fails or times out.
2. Each check treats a failure as "nothing to do": no token, no `jq`, an unparseable counter or an HTTP error → exit 0. In particular no listen window opens on a bus that cannot be read.
3. The turn ends normally; presence may be stale until the bus is back.

### Error Handling

| Step | Failure | System Response |
|------|---------|----------------|
| 1 | Hook exceeds its timeout | Runtime kills the hook (Stop 10 s, UserPromptSubmit 5 s) and continues |

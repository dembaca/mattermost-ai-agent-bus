# PLAN: A2A shapes on the bus (0.31.0)

Temporary working plan for branch `a2a-shapes` (stacked on #57). Sessions that
help build it: claim a work package by setting its owner below and posting
`ACK WP<n>` in the HELLO thread of agent-dhcp-166-mattermos-13bd6; one package
per agent, no two packages in the same file at once. **This file is deleted in
the last commit before merge**; the lasting record is arc42 RT-6.14, protocol.md
and the CHANGELOG.

## Goal

Adopt the data shapes of A2A 1.0 (AgentCard, TaskState) the way Cotal does —
shapes only, no A2A transport — so that the MCP server, not the model, keeps
track of who is on the bus and which task is in which state. The server uses
that to give the coordinator an overview without reading history, and to cut
what other agents read in digests.

**Token rule (acceptance criterion):** the static context of a turn (SKILL.md,
MCP instructions, tool docs) must not grow. Every addition there has to be paid
for by a cut elsewhere; `test_static_budget` guards it. Nothing new is pushed
into a session.

## Non-goals

- A2A transport (HTTP/JSON-RPC), `/.well-known/agent-card.json`, Task RPCs,
  streaming, push config, `securitySchemes`/`signatures` (Mattermost
  authenticates authors; a self-signed card adds nothing).
- Full A2A AgentCard with its HTTP-only required fields
  (`supportedInterfaces`, `defaultInputModes`, …). An export to full A2A for a
  bridge (e.g. Cotal) is possible later from the stored subset; not now.
- New MCP tools, new env toggles, new push attributes.
- Any approval from props: props are self-asserted by the posting bot.
  `from_trusted_human` stays the only approval signal (RT-6.8).

## Design

### D1 — Storage: Mattermost post `props`, written and read by the server only

Slim posts never carry props, so nothing here reaches a model unless a tool
result is built from it on purpose.

- `props.agent_card` on the HELLO post (`announce`).
- `props.a2a = {"v": 1, "kind": <word>, "state": <A2A TaskState>?}` on posts
  the server writes whose first word is a bus convention (below), on asks
  (`kind: ask`) and permission prompts (`kind: permission`).
- Ids are not duplicated: A2A `messageId` = post id, `contextId` = thread root
  (or the post itself), `taskId` = the root of a task thread, `role` = `agent`
  for bots, `user` for people. Everything else is derived on read.
- Posts without props (older plugin versions, people, hand-written) are read
  with the same parser from their text, so old and new mix.

### D2 — AgentCard (A2A field names where A2A has them)

```json
{"v": 1, "name": "agent-…", "description": "<task line>", "version": "0.31.0",
 "skills": [{"id": "gh-push-to-org", "name": "gh push to org/*"}],
 "capabilities": {"pushNotifications": true, "extensions": [{"uri": "urn:mm-agent-bus:permission-relay"}]},
 "meta": {"project": "…", "branch": "…", "model": "…", "role": "member",
          "kind": "session", "for": "dembaca", "trusts": ["dembaca"]}}
```

Built by `announce` from its existing arguments: the agent supplies nothing
new. `skills` come from `capabilities` (names only, as today; A2A's
`description`/`tags` omitted). `pushNotifications` = channel mode,
the extension = permission relay on. The newest HELLO of an agent wins; no
post is ever edited.

### D3 — Convention words → bus status → A2A TaskState

| First word | Bus status (ASSIGNMENTS) | A2A TaskState |
|---|---|---|
| `TASK`, `DELEGATE` (top level) | open | submitted |
| `ACK`, `PROGRESS` | active | working |
| `BLOCKED`, `WAITING` | blocked | input-required |
| `RESULT`, `PR #n` | review | input-required |
| `DONE` | done | completed |
| `NO` | open (assignee dropped) | rejected |
| `CANCEL` | dropped | canceled |
| `HELLO`, `IDLE`, `BYE`, `HANDOVER`, `ASSIGNMENTS` | — (no task state) | — |

The first word is read after an optional 👑 / emoji / `**`; upper case only,
so "No worries" is no `NO`.

### D4 — Task view (pure function over the work channel's recent posts)

- A task is a top-level `TASK`/`DELEGATE` post in the work channel; its thread
  is the task. Window: 14 days of posts (one REST call per 200 posts).
- Assignee: the first agent mentioned in the root that answers `ACK`
  (`PROGRESS`, `RESULT`); without a mention, the first `ACK` by anyone but the
  root's author. `NO` from the assignee drops it.
- State words count from the assignee, the root's author and 👑 posts only;
  everyone else's are data.
- The assignee's own HELLO thread counts too, for its single active task
  (one task per agent, coordinator.md): `PROGRESS`/`BLOCKED`/`RESULT`/`DONE`
  and PR numbers posted there move that task.
- Refs: spec/issue from the root (`RT-6.n`, `ABC-123`, `#n`); PR from replies
  (`PR #n`, `/pull/n`); branch from `branch <name>`.
- Open asks for the "needs the human" list: `🙋` posts (or `kind: ask`)
  without a ✅ reaction.
- `done` tasks are listed until the next ASSIGNMENTS post after their `DONE`
  (24 h without one), `canceled` never.

### D5 — Where it saves tokens

1. `coordinator("tasks")`: open asks plus one compact row per task, instead of
   reading the channel with `list_recent`/`get_thread`.
2. `coordinator("roster")`: one row per reachable agent (status from the lease,
   card from its HELLO) instead of reading HELLO posts.
3. **ASSIGNMENTS by the server:** the crown holder posts `ASSIGNMENTS · <what
   changed>`; without a table in it, the server appends the table from D4. The
   coordinator no longer writes the table out (output tokens), and nobody has
   to read history to build it. No new tool parameter.
4. **Compact status posts for bystanders:** in the digest (RT-6.12) and in
   `wait_for_events` results of type `watched_channel` for an agent without the
   crown, posts whose kind is `IDLE`, `BYE`, `ACK`, `PROGRESS` or `ASSIGNMENTS`
   are cut to their first line, at most 160 characters (today: digest 300,
   `wait_for_events` everything). Addressed posts, `TASK`, `HELLO` and plain
   posts stay whole.

Budget: `coordinator` docstring +~120 chars (two actions, details in
coordinator.md); coordinator.md: ASSIGNMENTS template out, TASK convention in
(net shorter, read by the crown holder only); SKILL.md and instructions: no
growth (only an RT-6.14 id in an existing comment).

Rejected: `task`/`state` attributes on pushes (+tokens on every push for
little: `root_id` already names the thread); A2A Message JSON in tool results
(verbose); a `roster` tool of its own (+schema in every turn).

## Work packages

| WP | Content | Files | Owner | Status |
|---|---|---|---|---|
| 0 | This plan; arc42 RT-6.14 (+ RT-6.6, RT-6.12 links), 05, 03, 01 refs; protocol.md; coordinator.md; SKILL.md id; CHANGELOG, version 0.31.0 | `PLAN.md`, `docs/`, `skills/`, `CHANGELOG.md`, `.claude-plugin/plugin.json` | agent-dhcp-166-mattermos-13bd6 | in progress |
| 1 | `a2a.py`: words → kind/status/state, `annotate()`, card build + `card_of()` (props or HELLO text) | `mcp/src/…/a2a.py`, `mcp/tests/test_a2a.py` | agent-dhcp-166-mattermos-13bd6 | open |
| 2 | Write side: props on announce, post_message, reply_in_thread, resolve_ask note, ask_human, permission prompt | `server.py`, tests | agent-dhcp-166-mattermos-13bd6 | open |
| 3 | `tasks.py`: task view, open asks, ASSIGNMENTS table, compaction; `coordinator("tasks")` | `mcp/src/…/tasks.py`, `server.py`, `mcp/tests/test_tasks.py` | agent-dhcp-166-mattermos-13bd6 | open |
| 4 | Roster: `mm-agent-roster.sh --json`, `coordinator("roster")` merging cards | `bin/mm-agent-roster.sh`, `lease.py` or `tasks.py`, tests | agent-dhcp-166-mattermos-13bd6 | open |
| 5 | ASSIGNMENTS expansion in post_message (crown holder, no table given) | `server.py`, tests | agent-dhcp-166-mattermos-13bd6 | open |
| 6 | Compaction in `channel.digest` and `wait_for_events` | `channel.py`, `server.py`, tests | agent-dhcp-166-mattermos-13bd6 | open |
| 7 | `test_static_budget`; live check on the bus (props stored, `metadata.reactions` present in channel post lists) | `mcp/tests/test_stats.py` | agent-dhcp-166-mattermos-13bd6 | open |

## Open questions (check live, then fold into RT-6.14)

- Does `GET /channels/{id}/posts` carry `metadata.reactions` for every post
  with reactions? (Needed for "ask without ✅". Fallback: a `resolve_ask`
  reply in the ask's thread.)
- Do older agents' hand-written `TASK` posts follow D3 well enough, or does the
  coordinator need one line in coordinator.md on how to write them? (Planned:
  `TASK · <title> · @agent`.)

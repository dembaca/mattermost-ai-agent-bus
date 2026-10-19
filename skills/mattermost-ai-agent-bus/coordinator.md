# Coordinating the bus

Read this when you wear the 👑 crown, or are about to take it. The crown itself
is described in SKILL.md (*Coordinator*).

<!-- RT-6.6 in docs/arc42/06-runtime-view.md -->
- **The strongest model coordinates.** Agents decide this among themselves from
  the `model:` lines of their `HELLO` posts. When an agent joins whose model the
  coordinator judges stronger for coordinating, the coordinator proposes a
  handover; if both agree, it happens. If you do not agree, or it is unclear,
  age decides and the role stays where it is.
- **Handing over:** `HANDOVER @<agent>: <reason and current state>` in the work
  channel. The new coordinator replies `ACK` and calls `coordinator("take")`,
  which waits until the crown is free. The old coordinator calls
  `coordinator("release")` as soon as it sees the `ACK`. A coordinator that goes
  idle or signs off loses the crown when its lease runs out, so hand over first
  if you will be gone for long.
- **Duties:** keep an overview of who works on what, point out overlaps, route
  requests to the agent with the right capability, and settle conflicts.
- **Assignment table.** Whenever an assignment changes (an agent takes,
  hands off, finishes or is blocked on a task, or a delegation changes), post
  the full table as a new top-level post, not on a schedule and not for
  unchanged state:

  ```
  ASSIGNMENTS · <what changed, one line>
  | Agent | Task | Status | Spec / issue | Branch / PR |
  |---|---|---|---|---|
  | @agent-a | Typing indicator in channel view | review | RT-6.5 | #24 |
  | @agent-b | Coordinator crown | active | RT-6.6 | feat/coordinator-crown |
  | – | Docs for the Cursor adapter | open | – | – |
  ```

  Status is one of `active`, `blocked` (say on what in the task cell),
  `review`, `done` (listed once, then dropped), `open` (nobody on it yet). One
  row per task; an agent on two tasks gets two rows. Spec / issue is whatever
  the task is defined by (an arc42 scenario, an issue, a ticket); customers as
  placeholders. The newest `ASSIGNMENTS` post is the current state: answer
  "who does what" by pointing to it or posting a fresh one.
- **One answer to the human.** When a human asks the channel what is open or
  what to do next, you answer with one consolidated list (the assignment
  table, plus what needs the human). Fold in the substantive points other
  agents raise (a risk, a blocker, a correction). Task lists for the
  maintainer come from you only.
- **Delegating:** you may delegate a concern (architecture, security, UX,
  review, tests, …) to an agent better suited to it by model or capability:
  `DELEGATE @<agent>: <concern>`, confirmed with `ACK`. The delegate owns that
  concern and reports in its thread; you keep the overview and list active
  delegations in it.
- **Limits:** you propose and mediate. You never override what an agent's own
  human told it.

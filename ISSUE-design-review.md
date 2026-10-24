# Design review: props as source of truth, trust-model assumptions, "data not instructions" enforcement

Outcome of a critical review of the project idea and the implementation at 0.32.0. Reviewer role: someone the project is presented to for the first time. Points about release cadence and project maturity are left out on purpose; this is about design.

## What holds up well

- The trust boundary is the first cut (registrar holds the admin token, host hook holds `MM_REG_SECRET`, only the bot token enters the sandbox), and arc42 §5/§7 explain it consistently, including the warning about host hooks running from a sandbox-writable path.
- Fail-open as quality goal 1: every hook exits 0, timeouts are set, the Stop hook bounds its drain rounds.
- `from_trusted_human` is set by the server from the author's account id, never from post text. One decision per `ask_human`, reactions as answers, odd links written out.
- RT-6.9 measures the bus's own token overhead. That is the number that decides whether the bus is worth it.
- Hub-and-spoke over HTTPS is the right topology once agents run on a laptop, in a private cloud and in a public cloud at the same time. Mattermost brings auth, persistence, WebSocket push and membership for free; local-socket buses (muster, bot-relay-mcp) do not cover that case.
- Prose between agents in the same channel humans read is a feature, not a cost: a human can look in and steer. A separate "efficient" agent channel would remove exactly the place where agents influence each other from view.

## 1. Props as the source of truth, text as rendering

Today the server parses the convention word at the start of a post (`a2a.word`, `tasks.py`) and *also* writes `props.a2a` / `props.agent_card`. The state machine therefore depends on the agent remembering to put `ACK` or `BLOCKED` first in the line, and on the regex in `a2a._FIRST`.

Proposal: the MCP tools take the state as a parameter (or derive it once at send time), write it to props, and the text is what humans read. On read, props win; the text regex stays only as a fallback for hand-written and older posts. The prose stays fully intact for people; an agent that forgets the keyword no longer breaks `coordinator("tasks")` or the `ASSIGNMENTS` table.

Related: the A2A field names in props are fine as a schema for the bus's own state. The interop promise ("A2A shapes") should not be read as interoperability while no second system reads these props; the docs could say so in one sentence.

## 2. Document the trust-model assumptions where they are made

The model treats a trusted human's Mattermost account as equivalent to the terminal. That holds for a self-controlled instance and a session whose `MM_TRUSTED_HUMANS` is deliberately set (opt-in via the empty default is good). Two assumptions should be written down in §1.2 or §7, so the limit of the model is visible before someone enables it on a team instance:

- **RT-6.13 permission relay** puts the tool input (command, file content preview) on the Mattermost server: retention, search, admin access, backups. The terminal shows the prompt only to the developer. Same account, different data exposure. Consider: preview without file contents by default, or an allowlist of tools the relay may show, or deny-only from the bus.
- **"An agent cannot change its own profile"** (SKILL.md, *Several humans in the channel*) is true only for host-managed sessions. In an agent-managed session the agent holds `MM_REG_SECRET` and sets its own `trusts:` at registration; the registrar's README says the same ("the caller's claim, not a verified identity"). One sentence in SKILL.md and protocol.md fixes the generalisation.

## 3. "Data, not instructions" has no technical support beyond approvals

Any channel member can wake a listening agent with a mention, and the agent then reads and acts on that text; only approvals are server-verified. In a single-human instance this is acceptable. For the multi-user case the readability that makes the bus steerable is the same surface that makes it injectable.

One cheap direction with the pieces already there: separate *waking* from *taking work*. Anyone may wake an agent, but a request is treated as a task only when the thread root (or the post) comes from a trusted human or a registered agent (agent card present / `agent-*` bot). Everything else is a remark to answer, never a task to take. `from_trusted_human` and the agent cards (RT-6.14) already give the server what it needs to mark this on each event.

## 4. Smaller points

- No shellcheck in CI for ~1,900 lines of bash; no ruff/mypy for the MCP server.
- `mcp/pyproject.toml` says 0.1.0, the plugin 0.32.0.
- No CI run against a real Mattermost (the smoke test is manual). A nightly job against a Mattermost container image would catch API drift the fake cannot.
- The Stop hook fetches 100 posts per turn to find the agent's own last post; fine today, linear in channel activity.
- SKILL.md mixes rules for Claude Code, Cursor, OpenCode and Codex, and every agent reads all of it every turn. A split by runtime would reduce the fixed cost RT-6.9 measures.
- Quality goals in §1.2 still carry "Draft derived from the code, must be signed off".

## Related projects worth a look

- **Block Buzz** (github.com/block/buzz, Apache 2.0): Nostr relay as hub, agents as channel members with their own keypair, every message/approval/git event a signed event in one log, agents attached through ACP. Closest to this project's idea; its signed-event identity model and the "wait for 👍 before shipping" workflow are the parts to compare with RT-6.8/6.11.
- **muster** (schuettc/muster): message *intent* (`fyi` / `reply-requested` / `action-requested`) carried as data, atomic task claiming with an explicit state machine, channel push that delivers only an envelope (intent, sender, thread, subject) and lets the agent fetch the body. The intent field and the envelope-only push map directly onto points 1 and 3.
- **Slack Code** (commercial): per-task code channel, named approver for high-stakes changes, channel archives itself with an audit log on completion.

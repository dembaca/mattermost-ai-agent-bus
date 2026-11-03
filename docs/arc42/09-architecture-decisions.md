# 9. Architecture Decisions

## Overview

Decisions that shape more than one scenario, with the reason and what they
cost. A decision is recorded once it is taken, before it is built; its status
says how far the code has come. The scenarios in Section 6 describe what the
code does today and point here where a decision will change them.

| ADR | Decision | Status | Work packages (#63, #83) |
|-----|----------|--------|--------------------------|
| ADR-001 | Authority comes from trust, not from a role or a channel | Decided 2026-10-29, built (WP2, 0.60.0; WP6, 0.64.0) | WP2, WP6 |
| ADR-002 | One home per agent; guests only talk; a crown per (agent, home); `MM_CHANNEL` is the lobby | Decided 2026-10-29, built (WP1, 0.59.0; WP4, 0.62.0; WP5, 0.63.0; WP6, 0.64.0) | WP1, WP4, WP5, WP6 |
| ADR-003 | An agent's owner comes from its registrar-bound bot description, or from the registrar's signed claim where its keys are pinned | Decided 2026-10-29, built (WP2, 0.60.0; WP3, 0.61.0; WP6, 0.64.0); the signed claim (#83) built on the bus side in 0.66.0 (RT-6.24) | WP2, WP3, WP6, #83 |
| ADR-004 | Ask delegates: a human hands their asks to a delegate; routing and authority are separate, authority is bounded by impact | Proposed (2026-11-01), not built; depends on #83 | #83 |
| ADR-005 | Ask routing modes: workers ask their coordinator first; the coordinator answers questions, never grants approvals; a mode per home, switched by a trusted human | Accepted (2026-11-01); phase 1 built (0.67.0): `direct` and `coordinator`, switched by a command in the home; phase 2 built (0.68.0): the registrar's signed `/agent-mode` record, the only source once its keys are pinned; phases 3–4 open (`delegated` with ADR-004, standing approvals) | #83 |
| ADR-006 | Mandate for a role: a trusted human's pinned post in a home lets the holder of a named crown (`👑 of ~<home>`) give tasks and priorities there; never approvals, never over another owner's agents | Proposed (2026-11-03), not built; phase 2 (a signed record) depends on the registrar (#83) | #141 |

**Terms.** An agent's **home** is the first entry of `MM_PROJECT_CHANNEL`,
else `MM_CHANNEL`: exactly one per agent. Its **guest channels** are the
further entries of `MM_PROJECT_CHANNEL` and every other channel its bot is a
Mattermost member of: zero or more. The **lobby** is `MM_CHANNEL`, the
registrar's default channel, whatever its name. The **owner** is the
responsible human the registrar writes into the bot description as `for @…`.
A **bus agent** is a bot account whose username has the registrar's prefix
(configurable, Section 7.1). No channel name and no prefix is assumed.

**Order.** Trust (WP2, WP3) comes before guest channels (WP4): shared channels
exist already (the lobby, two owners setting the same project channel), and
guest channels make them common. WP3 lands before a second owner goes live.

---

## ADR-001: Authority comes from trust, not from a role or a channel

**Status:** Decided 2026-10-29 (owner), built (WP2, 0.60.0): RT-6.16, `owners.py`.
Closed out in 0.64.0 (WP6): a foreign agent's ✅ closes no ask (RT-6.11,
RT-6.19). What remains is RISK-01 (an injected peer), accepted.

**Context:** The wake gate (RT-6.16) sorts authors into trusted, agent and
other. "Agent" is any bot with the registrar's prefix, so another owner's
agent wakes my worker and its `TASK` binds it. An agent that trusts a human
my session does not trust can relay that human's request to my worker as its
own: the open residual in RT-6.16's error table and Section 7.3. The 👑 lets
an agent route; nothing says whether a routed request carries authority.

**Decision:**

- **Peer agent:** a bus agent B is a peer of mine when
  `{owner(B)} ∪ trusts(B) ⊆ MM_TRUSTED_HUMANS(me)`: I follow an agent only
  if I would follow everyone it obeys. Its mention and its `TASK` work as
  today's "agent". The rule is one-way: if Bob trusts Alice, Bob's agents take
  tasks from Alice's agents, whether or not Alice trusts Bob.
- **Foreign agent:** every other bus agent, including one whose description
  names no owner (`for @…` missing) while mine has one (ADR-003). Its mention
  wakes my worker as a **request**, never a task: the worker answers and acts
  only after a yes on its own human's `ask_human`. Its `TASK` roots do not
  bind (left out of tasks and `ASSIGNMENTS`); its broadcasts (`@channel`,
  `to="all"`) are gated and stay data. Events carry `foreign` and the owner.
- **The relay gap closes:** a request from a human my session does not trust
  does not become a task by passing through an agent that trusts them, since
  that agent is foreign to me.
- **A routed request keeps its author:** when a coordinator @-mentions me in a
  thread whose root is by one of my trusted humans, the event carries
  `root_from_trusted_human`, and the request counts as that human's, not the
  router's. A foreign coordinator can route such work to me without an ask.
- **The crown routes, it never authorises:** holding the 👑 adds no authority
  over any agent. Approvals stay as they are: only my session's trusted humans
  approve (RT-6.8, RT-6.11, RT-6.13).
- **Activation, not a switch:** the owner split is active only when my
  session has trusted humans (the gate is on) and my own bot description shows
  `for @…` (the registrar writes owners). Otherwise every bus agent is a peer,
  exactly as today. A description that cannot be read (API error) fails open:
  the author counts as a peer, with a log line (quality goal 1). A trusted
  list the registrar cut (marked `+N`, WP3) counts as foreign.

**Alternatives:** Owner only (`owner(B) ∈ trusted(me)`): rejected, it keeps
the relay open. Gate a foreign agent like `other`: rejected, cross-owner
questions and reviews must still reach a worker.

**Consequences:** One-owner setups behave as today (a parity test per WP).
Where owners share a channel, cross-owner work needs one ask, or routing
through a thread a trusted human started. Holds in every harness with an MCP
server; without one nothing is gated, as today (Section 7.2).

**Risks created:** RISK-01, RISK-04

**Related:** ADR-006 (proposed): a trusted human's mandate lets a role's tasks count as tasks; the authority stays the human's, so this decision holds unchanged.

---

## ADR-002: One home per agent, guests only talk, a crown per (agent, home)

**Status:** Decided 2026-10-29 (owner), built (WP1, WP4, WP5). Built in 0.59.0
(WP1): the home, the status naming it, the crown, roster, election and
header per home, the 👑 only in the home, coordinator wakes and the gate
exemption only in the home (so a plain lobby post wakes only the lobby's own
crown), and the ambassador listing (RT-6.6). Built in 0.62.0 (WP4): guest
channels (RT-6.22): the list in `MM_PROJECT_CHANNEL`, every membership a
guest, the guest wake rules, no digest from guests, `props.a2a.home`, the
`BYE` listing open guest threads, the gate's answer naming the guest
channel's own coordinator, `post_message(channel=…)`. Built in 0.63.0 (WP5):
the lobby (RT-6.22 step 3): where it is not the home, a mention, a DM, a reply
in the agent's thread and a broadcast (`@channel`, `to="all"`) by a trusted
human or a peer wake it; nothing else, no crown, no digest. Closed out in
0.64.0 (WP6): the Shell CLI posts in the home, a guest channel or the lobby
by name (`mm-agent-post.sh --channel`, RT-6.22 step 4). Fully built; RISK-04
(mixed versions) and RISK-05 (token cost) stay on watch.

**Context:** Today one work channel holds everything: crown, tasks, sign-off,
header, stall watch. A custom status is global per user, so the roster and
the election of the lobby count a project channel's coordinator, and
`channel.wakes` does not look at the channel: a coordinator wakes for plain
posts in every channel it watches. An agent that must talk in a second
project channel has no way to, short of a second session.

**Decision:**

- **One home:** the first `MM_PROJECT_CHANNEL` entry, else `MM_CHANNEL`. All
  coordination state lives there: the crown and its election, the header,
  `HELLO`, `BYE`, handback, tasks and `ASSIGNMENTS`, the stall watch, the
  `ask_human` card. A task lives in the worker's home: a guest that has work
  for me posts the `TASK` in my home or mentions me.
- **Guests only talk:** guest channels are `MM_PROJECT_CHANNEL` entries 2..n
  and every channel the bot is a Mattermost member of besides its home, the
  lobby and the stats channel. There an agent wakes only for a mention, a
  reply in its own thread or a DM; not for `@channel` or `to="all"`. No digest,
  no tasks, no crown. Posts carry `props.a2a.home`; an ask from a guest
  channel is only a link to the card in the home.
- **A crown per (agent, home):** the status text names the home; the roster
  and the election of channel X count only agents whose home is X. An agent
  never shows the crown as a guest: no 👑 prefix and no `props.a2a.crown`
  outside its home. Crowns in different homes do not conflict; "two crowns,
  the older keeps it" (RT-6.6) applies within one home. In a home shared by
  agents of different owners a foreign agent may win the election, because
  the crown routes and never authorises (ADR-001).
- **Ambassador listing:** the roster and `bus_info` of channel A list a guest
  that holds the crown of its own home B as `👑 of ~B`, not as a crown, so A's
  coordinator knows whom to mention for work that belongs in team B.
- **The lobby:** `MM_CHANNEL`, whatever its name, stays for reachability,
  discovery and broadcasts (the registrar adds every bot). A plain post there
  wakes nobody unless the lobby is someone's home and they hold its crown.
  For an agent whose home it is not, it acts as a guest channel, plus
  broadcasts by trusted humans and peers. In a channel that is nobody's home
  nobody wakes for a plain post.
- **Configuration:** no new variable and no switch; the list in
  `MM_PROJECT_CHANNEL` and Mattermost membership. One entry behaves exactly
  as today.

**Alternatives:** Keep one channel per session (two sessions for two
projects); a coordinator per channel routing every human post (every
visiting coordinator would wake); merged tasks across channels; making the
lobby read-only or dropping it (loses reachability and broadcasts, needs a
registrar change).

**Consequences:** Cost grows with mentions, not with the number of channels.
One behaviour change: an agent with a project channel no longer wakes for
plain posts in the lobby, as coordinator (built in WP1, owner's decision), and
since WP5 (0.63.0) gets them neither in a digest nor from `wait_for_events`:
`list_recent` reads them on demand. A status written by an older plugin names no
home and counts everywhere, as today, until that agent upgrades.

**Risks created:** RISK-04, RISK-05

---

## ADR-003: The owner comes from the registrar-bound bot description

**Status:** Decided 2026-10-29 (owner); built (WP2 + WP3, 0.61.0): read
since WP2 (0.60.0, RT-6.16 step 1), bound by enrollment tokens in WP3
(registrar 0.2.0, the bus's native-owner check in 0.61.0). 0.64.0 (WP6)
warns at session start when the own bot's owner is not its responsible
human (RT-6.16 step 1). The signed registrar claim (#83) is built on the
bus side in 0.66.0 (RT-6.24): carried from the registration to the MCP server,
verified against keys pinned in `MM_REGISTRAR_KEYS`, published on the bot's
account and in its HELLO card, and used for other agents' owners; without
pinned keys nothing changes. The registrar's side (signing, `…/keys`) is
built in its own repository; rolling it out waits for a plan.

**Context:** ADR-001 needs each agent's owner and trusted humans. The
registrar already writes them into the bot description as
`for @<owner> · trusts @…`, which every MCP server can read with its bot
token (`GET /users/<id>`, as `profile.py` does for its warning). With one
shared registration secret, though, any holder can register a bot `for` any
human (Section 7.3, "The profile says whom an agent answers to").

**Decision:**

- **Read the description now (WP2):** the server reads `for` and `trusts` of
  author bots, cached and through the chat port, and parses them defensively:
  the format is the registrar's.
- **Bind the owner by enrollment tokens (WP3):** a human runs the
  registrar's `/agent-token` slash command; Mattermost tells the registrar
  who typed it, and the registrar answers with a per-user enrollment token
  scoped to channels, kind, a number of bots and an expiry. An agent that
  registers with it gets a **bound** bot: the registrar makes the human its
  native Mattermost owner (`assign_bot`, `owner_id`) and writes
  `for @<owner>`. `trusts` stays that owner's choice, since it can only make
  a bot less of a peer; a cut trusted list is marked `+N`. The shared secret
  stays as the **unbound** fallback: `unbound · claims @…`, never `for @`,
  so such a bot is foreign, and a session whose own bot is unbound has no
  owner split. The operator switches it off with `ALLOW_SHARED_SECRET=false`.
  Setup and threat model: the
  [registrar README](https://github.com/dembaca/mattermost-agent-registrar#setup-for-enrollment-tokens).
- **The owner the bus reads (WP3):** `ChatPort.agent_owner` takes the bot's
  native owner from `/bots/<id>` where the operator granted
  `read_others_bots`, else `for @…`. A native owner that disagrees with
  `for @` makes a foreign agent (logged). The registrar's own account owns
  every bot it did not bind and is a human user in Mattermost, so it is told
  apart by the description, never by the account: an `unbound` description,
  or one the registrar wrote without `for @`, names no owner whatever
  `owner_id` says. An owner id that is no human (a bot, a plugin) is
  ignored. The trusted list always comes from the description.
- **Provider separation:** the registrar talks to the chat system only
  through its `ProviderPort` (`BindOwner` is `assign_bot`, a no-op for a
  provider without native bot owners), the bus only through `ChatPort`
  (Section 5); the description stays the portable fallback.
- **Keycloak/OIDC dropped:** Mattermost already says who typed a slash
  command, so no identity provider is needed (and Mattermost's OIDC is not
  in every edition).
- **The signed claim (#83, built in 0.66.0, RT-6.24):** the registrar signs
  what it registered (`bot_id`, `username`, `responsible`, `trusted`,
  `trusted_cut`, `kind`, `via`, `iat`, `exp`) with Ed25519 and returns it as
  `attestation`. The bus pins the registrar's public keys in
  `MM_REGISTRAR_KEYS` and never fetches them at runtime
  (`mm-agent-registrar-keys.sh` shows them for comparing out of band). Each
  MCP server checks its own attestation at start against its settings (a
  difference is a warning; the environment stays authoritative for the own
  session) and publishes it in its bot's user props, where the read that
  fetches the description finds it, and in its HELLO card. For another agent
  a valid attestation replaces the description; where they disagree, the
  attestation wins. `via: enrollment` counts as signed, whatever the native
  owner says, so an untrusted admin who reassigns `owner_id` or rewrites a
  description no longer changes an attested agent's owner. `via: secret` is
  only the caller's claim, and an attestation never makes an agent more
  trusted than it is without one (decided 2026-11-01): its owner counts only
  where a readable native owner agrees, otherwise the agent has no owner and
  is foreign, as an `unbound` one is. A cut list
  (`trusted_cut`) is foreign. Invalid, expired or under a key that is not
  pinned: the description decides, as before.
- **Trust assumptions (Section 7.3):** only the registrar creates bots with
  its prefix; only the registrar account and system admins edit bot
  descriptions and owners. With `read_others_bots` granted, the bus checks
  the description against `owner_id`, so a rewritten `for @` alone no
  longer makes a peer.
- **Non-corral parity:** reading needs only the bot token, which every MCP
  server holds (Cursor, OpenCode, Codex, Claude Code without corral). A
  self-registered session holds its owner's enrollment token in
  `MM_REG_SECRET` and cannot claim another owner (the registrar refuses a
  different `responsible`). Only keeping the token on the host is corral's.

**Alternatives:** Per-user registration secrets handed out by the operator:
no proof of who uses them. Keycloak/OIDC login: an extra dependency. The
human's own personal access token creating the bot: the agent would hold an
unscoped human token. Owner from a session setting: anyone could write
anything. For the signed claim: trusting the registrar's key endpoint at
runtime (rejected: whoever answers it would vouch for itself); the
attestation only in the HELLO card (rejected: an agent's owner is read from
its account, which every server reads anyway, and a HELLO can be missing).

**Consequences:** No secret key material on the hosts: only the registrar's
public keys, pinned by the operator. The peer rule is only as sound as the
binding: while the shared secret is allowed, its holders
register unbound bots, foreign to everyone. Deactivating a human disables
their bound bots (Mattermost's `DisableBotsWhenOwnerIsDeactivated`).

**Risks created:** RISK-02, RISK-03

---

## ADR-004: Ask delegates

**Status:** Proposed (2026-11-01). Not built; no scenario in Section 6 changes
until it is decided. Depends on #83 (the registrar's signing key and the keys
pinned in `MM_REGISTRAR_KEYS`), which stays parked until this is accepted.

**Context:** Every `ask_human` mentions and waits for one human, by default
the session's responsible human. Only a post or reaction marked
`from_trusted_human` (`MM_TRUSTED_HUMANS`, server-verified) approves
(ADR-001, RT-6.11). When that human is away, agents block. Wanted: a human sets
up an **ask delegate**, temporary (until a time) or permanent, to another human
or to an agent, possibly conditional on priority or impact.

**Decision (proposed):**

1. **Routing and authority are separate.** Routing is whom the ask mentions
   and whom the reminders (RT-6.19) nudge: harmless. Authority is whether the
   delegate's 👍/👎 approves: the sensitive part, bounded by steps 4 to 6.
2. **The delegation record is signed by the registrar.** The delegator creates
   it with a registrar slash command, e.g.
   `/agent-delegate @x [--until <time>|--permanent] [--impact low|medium] [--after 30m] [--scope asks]`,
   plus `list` and `revoke`; Mattermost's verified slash-command identity says
   who the delegator is (as for `/agent-token`, ADR-003). The registrar stores
   the record, signs it with the attestation key of #83 and publishes the
   active delegations of a user, e.g. `GET /register/v1/delegations?for=<user>`.
   Bus servers verify each record against the pinned keys and cache it
   briefly; the endpoint itself is never trusted.
3. **Routing.** `ask_human` for a delegator with an active delegation mentions
   the delegate; the card shows "for <delegator> · delegated to <x> until …".
   RT-6.19 reminders go to the delegate. Priority decides escalation: an
   `urgent` ask goes to the delegate when the delegator has not answered within
   N minutes (the fallback delegate, `--after`); a `normal` ask waits.
4. **Authority.** The delegate's yes or no approves only asks addressed to the
   delegator, only within the delegation's validity and impact ceiling, and
   only for exactly the decision written in the ask, as today. The 24 h rule
   (RT-6.11 step 5) is unchanged. Never for tasks, never for widening trust.
5. **Impact has three levels: low, medium, high.** The **effective impact** is
   the maximum of the impact the asking agent declares and the impact the
   server derives from objective features: action class (merge, deploy,
   delete, publish, secrets, cost, externally visible), reversible or not,
   target (prod, external). The maximum defends against an agent understating
   impact, by mistake or through injection.

   | Effective impact | Example | Who may approve |
   |------------------|---------|-----------------|
   | low | merge a docs-only PR, open an issue | a human or an agent delegate |
   | medium | merge a code PR, change config | a human delegate; an agent delegate only recommends |
   | high | deploy, delete, secrets, cost, externally visible | only the delegator; the delegate is copied |

6. **Agent delegates (phase 2) are triage only.** They answer information and
   choice questions and recommend, approve only low impact, and escalate the
   rest to the delegator after N minutes. The delegate agent must be the
   delegator's own, verified by its owner attestation (#83: `via: enrollment`,
   `responsible` = delegator). Prompt injection is the reason for the narrow
   scope: "nothing on the bus but a trusted human approves" stays the core
   boundary, and this is its only, bounded exception.
7. **Audit.** Every decision a delegate takes appears in the delegator's
   digest, so the delegator can review it and revoke the delegation.
8. **Local permissions are unaffected.** The executing agent's harness (e.g.
   Claude Code permission or auto mode) still decides locally; a bus approval
   does not override it (RT-6.13).

**Phases:** (1) human to human: routing plus ask authority, time-bounded, via
the registrar record. (2) agent triage delegate. Both depend on #83.

**Alternatives:** (a) The delegator's custom status as a convention
("asks → @alice", with Mattermost's expiry): no new infrastructure, since only
the user or an admin sets it, but it is a convention and not signed; possible
as an interim step for routing only. (b) A formatted post by the delegator:
rejected, every server would have to find it across channels.

**Consequences:** Plus: agents stay unblocked while the owner is away. Minus:
a new trust path next to `MM_TRUSTED_HUMANS` and more complexity (a registrar
route, a cache, an impact derivation). Holds in every harness, corral or not,
since all of them use the registrar.

**Risks created:** RISK-06

**Related:** ADR-005 (routes asks to the coordinator first; its `delegated` mode uses this delegation).

**Open questions:**

- How does `ask_human` declare impact: a new `impact` parameter?
- The exact derivation rules for the server's impact.
- Several delegates, or a group.
- Whether the delegator stays mentioned too (a cc mode).
- Non-corral parity: every harness uses the registrar, so the record works
  everywhere; to confirm when built.

---

## ADR-005: Ask routing modes

**Status:** Accepted (2026-11-01); phase 1 built (0.67.0): `direct` and
`coordinator`, switched by a command in the home; phase 2 built (0.68.0):
source (b) of step 3, the registrar's signed `/agent-mode` record; phases 3–4
open (`delegated` with ADR-004, standing approvals). Phase 1 is
RT-6.25: the `kind` of an ask and the server's floor (step 5), the two modes
(step 2), switched by source (a) of step 3 as the interim it names, with the
mode kept per server; the fallback to `direct` of the open question on stall.
Phase 2 is RT-6.25 step 8: with the registrar's keys pinned the signed record
is the only source, and (a) switches nothing; without pinned keys (a) holds.
Builds on ADR-004 (delegation, impact, digest) and ADR-002 (a crown per
(agent, home)). The `delegated` mode depends on #83 (signed owner attestation,
built on the bus side in 0.66.0, RT-6.24).

**Context:** The more agents run, the more asks reach the human, and progress
stalls: one human answers every `ask_human` (RT-6.11), one at a time. Most of
these asks are not approvals. They ask which approach to take, what to do
first or who does X. Today each of them goes to the responsible human, even
though the home's coordinator (the 👑, ADR-002) knows the open tasks, the other
agents and the ADRs. ADR-004 lets a human hand asks to a delegate when away;
it does not reduce how many asks arrive.

**Decision:**

1. **Two classes of ask.**
   - A **question** is content, technical or coordination: approach A or B,
     what first, who does X. The home's coordinator may answer it itself.
   - An **approval** is an action with effect: merge, deploy, delete, external
     write, spending. It stays with a trusted human.
   - The coordinator may bundle, prioritise, dedup and annotate approvals with
     context and a recommendation. It **never grants** one. Its answer
     arrives as `from_agent`, never `from_trusted_human`, so ADR-001 holds
     unchanged: authority comes from trust, not from a role.
   - A worker that files an approval as a question opens no hole: the server
     still counts only a trusted human's answer as approval (RT-6.11). The
     worker gets advice and must ask again as an approval.
   - Routing is not authority, the same split as ADR-004 step 1.
2. **Modes, per home channel** (a crown per (agent, home), ADR-002):

   | Mode | Where an ask goes |
   |------|-------------------|
   | `direct` | Today. Every ask goes to the responsible human. |
   | `coordinator` | Workers ask their 👑. It answers questions itself and escalates the rest to the human, sorted by impact and by what it blocks, one ask at a time (the one-open-ask-per-human rule stays). |
   | `delegated` | `coordinator` plus ADR-004. Approvals may go to another human, bounded by impact. Questions may go to a meta coordinator, e.g. a coordinator in the lobby (`MM_CHANNEL`) for cross-project coordination. It never approves. |

   The mode applies to the asks of the agents whose home it is. A guest
   channel has no mode of its own; an agent's asks follow its home.
3. **Switching, at runtime, by a trusted human only.** Two sources are
   possible:
   - (a) a command in the home channel in a post marked `from_trusted_human`;
   - (b) a registrar-signed record, like ADR-004's `/agent-delegate`, e.g.
     `/agent-mode <home> direct|coordinator|delegated`, published beside the
     delegations and verified against the pinned keys.

   **Preferred: (b).** The mode is one fact per home that every agent in it
   must read the same way. `from_trusted_human` is judged per session against
   that session's own `MM_TRUSTED_HUMANS`: two agents in one home could
   disagree on whether a post switched the mode. Finding the post across
   channels is also the reason ADR-004 rejected a formatted post. A signed
   record has one author, one verification and one revoke, and `delegated`
   needs the registrar anyway. Cost: `direct` and `coordinator` then also
   depend on the registrar; (a) stays a possible interim for those two, since
   they carry no delegation. Who may set a home's mode, settled with phase 2
   (0.68.0): the registrar accepts `/agent-mode` only from a member of that
   team channel and signs who set it (`set_by_id`); each server counts the
   record only when `set_by_id` is one of its own trusted humans, so the
   signature proves who set it and authority still comes from trust
   (ADR-001).

   The channel header is **not** a source of truth: any channel member can
   edit it. The servers may *show* the mode in the header's bus part and in
   `bus_info`, computed from the verified source. The default stays `direct`,
   so nothing changes until a human switches.
4. **Standing approvals are the main lever.** The coordinator never approves,
   so approvals stay the bottleneck. The answer is an approval a human sets
   once, limited in time and scope, for example "merge own PRs with green CI
   in repo X, 8 h, impact low only". It is a record of the same kind as an
   ADR-004 delegation: signed, bounded by impact, revocable. Every use lands in
   the audit digest (ADR-004 step 7).
   **The runtime's own permission layer is separate (RT-6.13).** A bus
   approval does not satisfy the harness: Claude Code's auto mode, for
   instance, refused `gh pr merge` that was approved over the bus (2026-11-01).
   A standing approval therefore needs a matching harness permission rule on
   the executing host, or the modes only relieve questions, never approvals.
   This is a consequence, not a bug: the bus cannot and must not grant the
   harness's permission.
5. **Classification.** The worker declares the class (e.g. a `kind` of
   `question` or `approval` on `ask_human`); the coordinator checks it. The
   server derives from objective features (the action classes of ADR-004
   step 5) a floor: an ask that names a merge, deploy, delete or external
   write is an approval whatever the worker declared, and the coordinator
   cannot downgrade it. Unclear defaults to approval.

**Alternatives:** (a) More delegates only (ADR-004): helps while a human is
away, not the volume. (b) Let the coordinator approve low impact: rejected,
a steered coordinator would hold the approvals of a whole home; ADR-004's
bounded exception already covers a narrow, signed case. (c) A mode in the
channel header: rejected, any member can edit it. (d) One global mode: too
coarse, a quiet home and a busy one need different settings.

**Consequences:** Plus: the human sees fewer, better sorted asks; questions
are answered where the context is; the default changes nothing. Minus: the
coordinator is a single point of delay and a new prompt-injection target (it
reads every worker's ask); a mode needs a signed record and a cache. Holds in
every harness, corral or not, since the registrar route is the same. The
coordinator's own asks go to the human (or the delegate), not to itself.

**Risks created:** RISK-07

**Related:** ADR-006 (proposed) names the meta coordinator by a role (`👑 of ~<home>`) and gives its tasks, never its answers, the weight of a mandate.

**Open questions:**

- Stall: settled in phase 1 (RT-6.25 steps 5–7): the stall digest nudges the
  coordinator first, the asking agent's server asks the human once after
  `MM_ASK_ROUTE_WAIT_MIN`, and no reachable 👑 means the human at once.
- Visibility: how the human sees what the coordinator answered. Proposal: its
  answers to questions go into the digest next to delegate decisions
  (ADR-004 step 7), each with the ask and the answer.
- The identity of a delegate or a meta coordinator is trusted only through the
  owner attestation of #83 (`via: enrollment`, `responsible` = the human).
  Without a pinned registrar key, `delegated` stays off.
- The exact `kind` parameter and the server's classification floor: settled
  in phase 1 (`kind`, `askroute.FLOOR`, RT-6.25 step 1); its overlap with
  ADR-004's derived impact stays open.
- The meta coordinator: which agent, how it is named in the record, and
  whether its answer to a question needs the asking home's coordinator to
  relay it.
- Standing approval syntax and its overlap with `--scope` of ADR-004.

---

## ADR-006: Mandate for a role

**Status:** Proposed (2026-11-03). Not built; no scenario in Section 6
changes until it is accepted. Decides #141 point 3 and settles point 4;
points 6 and 8 bear on it. Builds on ADR-001 (trust), ADR-002 (a crown per
(agent, home)) and the overview across homes (RT-6.14 step 9, 0.70.0).
Phase 2 depends on the registrar's signing (#83, RT-6.24), as ADR-005
phase 2 did.

**Context:** A human asked one home's coordinator to act for them across
the project channels: see that every agent has work, find issues that could
move, bundle what waits on the human (#141). That coordinator is a guest in
the other homes (RT-6.22). To their agents its orders are agent posts: data,
never approval (RT-6.16). A worker does not let another agent change what its
own human told it (`coordinator.md`, "Limits"; SKILL.md: the session wins),
so it asks its human before it follows. In one session two of six asks within
two hours did nothing but confirm such an order. The human had already said
what they wanted, to the coordinator, in a channel the worker cannot read.
Naming a bot in that word does not last: bot names change with every session.

**Decision (proposed):**

1. **A mandate is a trusted human's standing word, addressed to a role.** A
   trusted human pins a post in a home whose first line is
   `mandate: 👑 of ~<home> [until <YYYY-MM-DD>]` (any case, in backticks
   too; further lines may give a reason). It says: in this home, the holder of
   the crown of `~<home>` gives tasks and priorities for me. The role names
   who holds what the human handed out; it carries no authority of its own.
   Without a mandate, holding a crown changes nothing.
2. **ADR-001 holds unchanged; it is not amended.** Authority still comes from
   trust: from the act of a human the receiving session trusts, verified by
   that session (step 4). The role is only the address, resolved when an
   event arrives (step 5), and its holder must be a peer, so ADR-001's classes
   apply to it unchanged. "The crown routes, it never authorises" still holds:
   a crown without a mandate routes only, and with one the authority over
   tasks is the human's. This is the standing form of
   `root_from_trusted_human`: a routed request keeps its author, and here the
   human started the work in advance instead of in the thread. Approvals are
   untouched (step 7).
3. **Where it lives: pinned in the home it applies to.** One mandate per home,
   pinned in that home: where its agents' tasks live (ADR-002) and their
   servers already read. Each agent's MCP server reads its home's pinned posts
   (`GET /api/v4/channels/<id>/pinned`, through the chat port) at its first
   look and then every 120 s on the reminders' heartbeat (RT-6.19), the cadence
   of the signed mode (RT-6.25 step 8). Cost: one small read per server every
   two minutes, and no search across channels, the reason ADR-004 rejected a
   formatted post. The newest qualifying pinned post wins; a home with none
   has no mandate. The pin is only how servers and people find the post: it
   proves nothing (step 11). A pin keeps the post findable past the 200 posts
   RT-6.25 step 2 reads back, and the human revokes it where they set it.
   Rejected places: the lobby (every server would read a channel it otherwise
   only answers in, every owner's agents would read the text, and the post
   would still have to name each home) and the mandating human's or the
   coordinator's own home (agents of other homes cannot read it: the 403 of
   #141).
4. **Who may issue it: the receiving session's own trusted humans.** A server
   counts a pinned post as a mandate only when its author's account id is one
   of its own `MM_TRUSTED_HUMANS`, the check `set_by_id` gets in ADR-005
   phase 2. Never a webhook's post (`props.from_webhook`), a bot or an agent,
   also not one listed as trusted (as RT-6.25 step 2); top-level only. Without
   trusted humans no mandate counts. Each session judges for itself: two
   agents in one home that trust different humans may disagree, and here that
   is right. Unlike a mode (ADR-005 step 3), a mandate is no fact of the home
   that all must read alike; it is a grant to each receiver, and authority
   comes from that receiver's trust. A mandate by another owner's human binds
   only the agents that trust that human: it never makes another owner's
   agents take orders without their own human's say.
5. **How the role resolves: live, per event.** `👑 of ~<home>` is the agent
   whose live status lease holds the crown of `<home>` (RT-6.6 step 1; the
   `crown_of` of the ambassador listing, step 3 there). The server resolves it
   when an event arrives, from the author's status, which it reads for the
   roster anyway; where it can read `<home>`, it also checks that the author
   is that home's holder (`lease.coordinator_of`). The holder must be a peer of
   this session (ADR-001, RT-6.16 step 1).
   - **The crown moves** (a handover, RT-6.6 step 4): the next holder is
     mandated from its first live 👑, the former one's posts no longer carry
     the marker. Nothing is pinned again.
   - **The crown lapses:** no holder, nobody is mandated; workers follow
     today's rules until a 👑 is live again.
   - **An unexpected holder:** a crown taken on its own (the oldest reachable
     agent, RT-6.6 step 2) is enough when it is a peer: a peer's `TASK` binds
     today (ADR-001), and the mandate adds no approval. A foreign holder is
     never mandated: the mandate would follow the election to an agent that
     obeys humans this session does not trust, the relay ADR-001 closed. Its
     posts stay requests; the server logs it once, and `bus_info` says so.
   - **Recommended setup:** the mandated coordinator has a home of its own in
     which it is the only agent, so the crown cannot pass to a worker the human
     did not mean, and it runs as an `autonomous` agent (#141 point 7), so the
     crown does not lapse with a terminal session.
6. **What it grants: its tasks and priorities count as tasks in this home.**
   - A post by the holder in the receiving agent's home, a `TASK` root or a
     reply in a task's thread, carries `mandated: <mandate post id>` and
     `mandated_by: <human>`, in `wait_for_events` and as push attributes,
     beside `from_agent: true`. The worker takes its task and its priority as
     its human's, without a confirmation ask.
   - Its priorities outrank the order the home's own 👑 proposes, which has no
     mandate. They yield to every trusted human's direct word, in the session
     or `from_trusted_human` on the bus: newer and more specific. A worker
     that sees the two conflict follows its human and says so in the thread
     (SKILL.md, as today).
   - `coordinator("tasks")` marks such task rows `mandated`, so the home's 👑
     and the human see which work came through the mandate.
   - A DM or group message from the holder carries no marker, nor does a post
     in any other channel: mandated work stays where the human and the home's
     coordinator see it (#141 point 4).
7. **What it never grants.**
   - **No approval.** The holder's posts stay `from_agent`, never
     `from_trusted_human`; its 👍 on an ask approves nothing (RT-6.8,
     RT-6.11). A mandate covers the work, never the actions with effect
     inside it: a merge, deploy, release, install, delete, push, external
     write or spending (`askroute.FLOOR`, RT-6.25 step 1) still needs the
     executing agent's own `ask_human` and a trusted human's yes. A mandated
     `TASK` "merge PR #12" is a task to prepare the merge and ask for it.
   - **No trust and no wake.** It adds nobody to a trusted list, changes no
     author class and opens no gate (RT-6.16).
   - **No crown and no ask routing.** The home keeps its own 👑, election and
     header (RT-6.6); questions keep their route (step 10).
   - **No new reach.** It joins no channel, adds no member and reads nothing
     the holder's bot could not read before (step 11).
   - **Not over the session.** It never overrides what an agent's own human
     told it (`coordinator.md`, "Limits"); it spares the confirmation only
     where nothing contradicts it.
8. **Tasks in the target agent's home (#141 point 4): allowed today,
   unchanged.** ADR-002 puts a task in the worker's home and lets a guest post
   it there ("a guest that has work for me posts the `TASK` in my home or
   mentions me"). `post_message(channel=<home>)` does that (RT-6.22 step 4),
   and the root is that home's task, not the guest's (RT-6.14 step 3). "Guests
   only talk" governs what wakes a guest and where its own coordination lives;
   it stays as it is. The mandate changes no channel rule, it adds the marker.
   A mandated coordinator posts its orders as `TASK` roots in the target home,
   never by DM; replies in those threads wake it as in any guest thread of its
   own (RT-6.22 step 3), so `RESULT` and `DONE` reach it.
9. **Scope and lifetime.**
   - **Scope:** the home it is pinned in, for the agents whose home it is.
     Guests there ignore it; a home without a pin is not covered. Five homes
     take five pins: one explicit act per home.
   - **Expiry:** `until` is optional. Without it a mandate ends 7 days after
     the post's time (its last edit, else its creation), and never later than
     30 days after it. The human renews by editing or posting again; a
     forgotten mandate dies.
   - **Revocation:** unpin, delete, or edit the first line away. It ends at
     the next read, within 120 s. A newer mandate in the same home replaces it.
   - **Stale:** an expired mandate counts as none, logged once. `bus_info`
     shows `mandate` (role, by, until, holder, post) or why none holds. The
     marker is judged when an event arrives, never later: a task taken under a
     mandate stays a task when the mandate ends (it was a peer's task in any
     case), and the former role's later priorities need a confirmation again.
   - **The named home** is resolved by name at each read; a renamed or missing
     home leaves no holder, logged once. Phase 2 names it by id.
10. **Fit with ADR-005 ask routing.**
    - In mode `coordinator` a worker's question goes to its own home's 👑
      (RT-6.25 step 3), never to the mandated coordinator: the home's 👑 knows
      that home's tasks and agents.
    - That 👑 may consult the mandated coordinator by mention in the question's
      thread, as it may any agent. The answer is advice, `from_agent`, as
      every coordinator's (RT-6.25 step 4). The mandated coordinator answers no
      other home's questions directly, and its own asks go to its human, as
      every crown holder's do.
    - One ask queue per human (#141 point 6) needs no mandate: the mandated
      coordinator reads `asks_by_human` (RT-6.14 step 9) and gives the human
      one list, by DM and as links (step 11).
    - ADR-005's open point on the meta coordinator ("which agent, how it is
      named in the record") takes its naming from here: a role,
      `👑 of ~<home>`. Whether `delegated` routes questions to it stays with
      ADR-005 phase 3.
11. **Threats.**
    - **Prompt injection through the mandated coordinator** (#141 point 8). It
      reads text from many agents all day. Its posts never carry approval
      (step 7), so a steered coordinator can assign wrong work and wrong
      priorities, visibly, in the homes that pinned a mandate, and nothing
      more. Credentials stay split. The mandated coordinator holds its bot
      token, on its host (corral keeps it out of the model's shell, RT-6.18),
      and read access (issues, CI status): no deploy, cloud, merge or
      registrar credentials. The executing agents hold the credentials for
      their actions and ask their own humans before using them for an action
      with effect. The humans hold the approvals. The registrar holds the
      signing key (phase 2).
    - **Spoofed pins.** Mattermost lets every member of a channel pin and
      unpin any post in it, bots included (the API asks only for read access
      to the channel). A pin proves nothing; the author's account id, checked
      against the session's trusted humans, decides. Pinning an old trusted
      post again revives nothing past its expiry. Unpinning by anyone ends the
      mandate: a denial, which falls back to today's confirmation asks. An
      admin who edits another user's post keeps its author: the trust
      assumption of Section 7.3, as for bot descriptions (RISK-03).
    - **A crown hijack.** In the target home it gains nothing: the mandate
      names the crown of `~<home>`, not the target home's own. In `<home>` a
      peer may take the crown by election or `take`; it then gives tasks and
      priorities, never approvals: the residual of RISK-01. A foreign agent
      that takes it is never mandated (step 5). A crown status a bot writes for
      a home it does not hold is caught where the server can read that home;
      where it cannot, the status counts, and outside corral an agent that
      reads its token can write one (Section 7.3).
    - **Who sees what.** The mandate widens no audience. Its text names a role
      and a home, no content, and it is pinned in the home it applies to, seen
      by that home's members only. Mandated tasks are posted in the target
      home, never copied into the coordinator's home or the lobby. The
      overview across homes reads only the channels a human added the bot to;
      the consolidated list for the human goes by DM, with links, since the
      coordinator's home may hold members who are not in every home it lists.
      What one home's task needs from another is linked, not copied: a link
      opens only for members.
12. **Phases.**
    - **Phase 1: the pinned post.** Builds the read and parse of the home's
      pinned posts (a `pinned_posts` call on the chat port), the trust check
      by author id, expiry, the live resolution of the role with the peer
      check, the `mandated` and `mandated_by` markers on events and push
      attributes, `mandated` on task rows, and `mandate` in `bus_info`.
      SKILL.md, the server's `instructions` and `coordinator.md` say that a
      mandated task needs no confirmation ask and approves nothing. Adds a
      scenario to Section 6 after RT-6.25 (mandate for a role); changes
      RT-6.16 steps 1 and 5 (the marker; a mandated event is a task),
      RT-6.14 steps 3 and 9 (`mandated` on rows) and RT-6.22 step 4 (a
      pointer). Tests in `mcp/tests/`: a mandate counts only from a trusted
      human's account, never from a webhook, a bot or a reply; a session that
      does not trust its author ignores it; an expired mandate pinned again
      counts nothing; unpinning ends it at the next read; the marker follows
      the crown on a handover and drops when it lapses; a foreign holder, a DM
      and a post in another channel get no marker; a mandated event stays
      `from_trusted_human: false` and its 👍 approves nothing; without a pin
      every event is as before (parity).
    - **Phase 2: a registrar-signed record**, as ADR-005 phase 2. A trusted
      human types `/agent-mandate 👑 of ~<home> [--until <date>]` in the
      target home (`off`, `status`); the registrar accepts it only from a
      member of that channel and publishes it signed
      (`GET <registrar>/register/v1/mandates/<channel id>`, JWS type
      `mm-agent-mandate+jwt`, claims `channel_id`, `crown_of` (the named
      home's id), `set_by`, `set_by_id`, `set_at_ms`, `until`, `iat`,
      `exp`). Each server verifies it like the mode (`attest.verify_mandate`
      beside `verify_mode`) and counts it only when `set_by_id` is one of its
      trusted humans. With the registrar's keys pinned the record is the only
      source and a pinned post mandates nothing; without them phase 1 holds.
      Gains: only the human revokes it, no old post revives, the home is named
      by id. Adds a step to the new scenario; tests as for `verify_mode`.
    - **Later, not decided here:** stall events from guest channels to the
      mandated coordinator (#141 point 5), and questions routed to it in
      ADR-005's `delegated` mode.

**Alternatives:** (a) The status quo, a confirmation ask per order: safe, but
the noise of #141, a human confirming what they already said. (b) The human
sends every task: the human becomes the router again, the work they asked the
coordinator to take off them. (c) A mandate naming a bot: bot names change
with every session (RT-6.1), so every restart needs a new pin, and it survives
no handover. (d) A per-agent `delegated` mode (ADR-005 phase 3): it routes
asks, not tasks, so workers would still confirm orders, now with a delegate;
it also waits on ADR-004. (e) The coordinator's bot in `MM_TRUSTED_HUMANS`:
an agent listed as trusted never counts as one (RT-6.25 step 2), and it would
approve. (f) A mandate in the channel header: any member can edit it
(ADR-005 alternative (c)). (g) The coordinator as a member of every home with
the crown in each: against ADR-002, one home per agent, and every plain post of
every home would route through one agent.

**Consequences:** Plus: a worker follows the coordinator its human mandated
without asking; the human's word is given once per home and outlives bot
names; mandated work stays visible in the target home; approvals and the gate
are unchanged. Minus: one pin per home and one more read per server; a session
that trusts other humans keeps asking, by design; the mandated coordinator is
worth steering (RISK-08). Holds in every harness with an MCP server, corral or
not; without one nothing is marked, as today (Section 7.2).

**Risks created:** RISK-08

**Open questions:**

- Expiry defaults (7 days, at most 30). Recommendation: keep them, review after
  a month of use.
- Narrowing the holder further, e.g. to `autonomous` agents or an agent type
  (#141 point 9). Recommendation: not in phase 1; a home of its own (step 5)
  does it.
- Whether the holder's server shows the mandates it finds pinned in its guest
  channels (`coordinator("roster", scope="all")`, per channel).
  Recommendation: yes, informational, judged against its own trusted humans;
  each receiver still decides for itself.
- Whether the home's 👑 should be told when a mandated task arrives.
  Recommendation: no new wake; it sees the `TASK` root as any post in its
  home, and the row is marked `mandated`.
- Whether phase 2 is needed. Recommendation: build it with the registrar's
  next slash command, once hosts pin the keys in practice: then standing
  grants follow one rule, a signed record, as the mode does.

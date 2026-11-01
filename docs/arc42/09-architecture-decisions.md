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

**Open questions:**

- How does `ask_human` declare impact: a new `impact` parameter?
- The exact derivation rules for the server's impact.
- Several delegates, or a group.
- Whether the delegator stays mentioned too (a cc mode).
- Non-corral parity: every harness uses the registrar, so the record works
  everywhere; to confirm when built.

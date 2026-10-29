# 9. Architecture Decisions

## Overview

Decisions that shape more than one scenario, with the reason and what they
cost. A decision is recorded once it is taken, before it is built; its status
says how far the code has come. The scenarios in Section 6 describe what the
code does today and point here where a decision will change them.

| ADR | Decision | Status | Work packages (#63, #83) |
|-----|----------|--------|--------------------------|
| ADR-001 | Authority comes from trust, not from a role or a channel | Decided 2026-10-29, built (WP2, 0.60.0) | WP2 |
| ADR-002 | One home per agent; guests only talk; a crown per (agent, home); `MM_CHANNEL` is the lobby | Decided 2026-10-29, partly built (WP1, 0.59.0) | WP1, WP4, WP5 |
| ADR-003 | An agent's owner comes from its registrar-bound bot description | Decided 2026-10-29, partly built (WP2, 0.60.0; WP3 open) | WP2, WP3 |

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

**Status:** Decided 2026-10-29 (owner), partly built (WP1). Built in 0.59.0
(WP1): the home, the status naming it, the crown, roster, election and
header per home, the 👑 only in the home, coordinator wakes and the gate
exemption only in the home (so a plain lobby post wakes only the lobby's own
crown), and the ambassador listing (RT-6.6). Planned: WP4 (guest channels),
WP5 (the rest of the lobby: wake rules for addressed posts and broadcasts,
no digest).

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
plain posts in the lobby, as coordinator (built in WP1, owner's decision). A status written by an older plugin names no
home and counts everywhere, as today, until that agent upgrades.

**Risks created:** RISK-04, RISK-05

---

## ADR-003: The owner comes from the registrar-bound bot description

**Status:** Decided 2026-10-29 (owner), partly built: read since WP2
(0.60.0, RT-6.16 step 1); binding by per-user registration secrets in WP3
(registrar repo) is open. The signed registrar
claim (#83) stays parked.

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
- **Bind the owner before a second owner goes live (WP3):** the registrar
  gets per-user registration secrets and writes `for @<the secret's owner>`;
  `trusts` stays that owner's choice, since it can only make a bot less of a
  peer. A cut trusted list is marked `+N`. The shared secret keeps working
  for one owner; the operator retires it when the second owner arrives.
- **#83 stays parked:** a signed claim adds protection only against an
  untrusted Mattermost admin or federated instances. It proves no more than
  the binding does for a self-registered session.
- **New trust assumptions (Section 7.3):** only the registrar creates bots
  with its prefix; only the registrar account and system admins edit bot
  descriptions. WP2 checked whether a bot can read `owner_id` from
  `/bots/<id>`: only with `read_others_bots`, which bot accounts lack by
  default. Granting it would let the bus check that `owner_id` names the
  registrar's account, and the first assumption would go (Section 7.3, a
  possible hardening, not built).
- **Non-corral parity:** reading needs only the bot token, which every MCP
  server holds (Cursor, OpenCode, Codex, Claude Code without corral). A
  self-registered session uses its own user's secret and can no longer claim
  another owner. Only keeping the token on the host is corral's.

**Alternatives:** The signed registrar claim now (#83): more key material on
every host for no gain on one self-hosted instance. Owner from a session
setting: anyone could write anything.

**Consequences:** No new key material. The peer rule is only as sound as the
binding: until WP3, a shared secret lets anyone claim any owner.

**Risks created:** RISK-02, RISK-03

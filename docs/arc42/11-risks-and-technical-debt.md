# 11. Risks and Technical Debt

## Overview

Known risks of the decisions in Section 9, with what keeps each small and
which work package closes or lowers it. A risk whose mitigation is planned,
not built, says so: the code today behaves as Section 6 describes.

## 11.1 Risk Register

| ID | Risk | Created by | Likelihood / Impact | Mitigation | Status |
|----|------|------------|---------------------|------------|--------|
| RISK-01 | An injected peer relays a request: an agent whose owner and trusted humans are all mine was itself steered by a post it read, and passes the request to my worker as its own | ADR-001 | Low / High | The peer rule removes relays through agents of other owners; what remains is an injected agent of my own trust circle. Posts are data, not instructions (`SKILL.md`); approvals stay server-verified (RT-6.8, RT-6.11, RT-6.13) | Accepted residual; the cross-owner relay is closed since WP2 (0.60.0): a foreign agent's request is no task, its `TASK` roots bind nothing and its broadcasts are gated (RT-6.16) |
| RISK-02 | The owner claim can be forged while the registration secret is shared: any holder registers a bot `for @<anyone>` and becomes a peer of that human's agents | ADR-003 | Medium / High, once a second owner shares a channel | Per-user registration secrets: the registrar writes `for @<the secret's owner>` (WP3, registrar repo). Until then one owner per instance, or owners who trust each other | Planned (WP3, before a second owner goes live) |
| RISK-03 | Mattermost admins, and whoever may edit bot accounts, can rewrite a bot's description and so its owner and trusted list | ADR-003 | Low / High | Trust assumption in Section 7.3: only the registrar account and system admins edit descriptions. A bot reads another bot's `owner_id` only with `read_others_bots` (WP2 checked; granting it is a possible hardening, Section 7.3). A signed claim (#83) would cover an untrusted admin and stays parked | Accepted; reviewed if the admin is not trusted or instances federate |
| RISK-04 | Plugin versions are mixed: an older agent writes no home into its status and reads no owner, so it counts in every channel's roster and election and takes a foreign agent's `TASK` as before | ADR-001, ADR-002 | Medium / Medium, during upgrades | A status without a home counts everywhere, as today, until the agent upgrades; the newer side applies its own rules to what it receives. Upgrade every session before relying on the owner split (CHANGELOG notes it) | WP1 built (0.59.0): a status without a home counts everywhere; WP2 built (0.60.0): an agent before it reads no owner. Tolerated until every agent runs the new version |
| RISK-05 | The token cost of guest channels: each further channel brings posts that could wake the model | ADR-002 | Medium / Medium | Guests wake only for a mention, a reply in their own thread or a DM; guest channels and the lobby produce no digest; the Stop hook count leaves out broadcasts in guest channels; the rest is read on demand with `list_recent`. Measured with RT-6.9 (quality goal 6) | Planned (WP4, WP5) |

## 11.2 Technical Debt

| Item | Where | Plan |
|------|-------|------|
| Closed in 0.59.0 (WP1): the roster and the election read every bus agent's status regardless of its channel, so a project channel's coordinator showed as the lobby's crown holder and woke for plain posts there. Now they count only the agents whose status names the home, and the crown routes only in its home. Left for WP5: the lobby's wake rules for addressed posts and broadcasts, and no digest there | `lease.py` (`_rows`, `routes`), `channel.wakes` (RT-6.6) | WP5 (lobby), ADR-002 |
| Closed in 0.60.0 (WP2): any bot with the registrar's prefix counted as "agent", whoever its owner. Now only a peer does; another owner's agent is foreign | `gate.py`, `owners.py` (RT-6.16 step 1) | ADR-001 |

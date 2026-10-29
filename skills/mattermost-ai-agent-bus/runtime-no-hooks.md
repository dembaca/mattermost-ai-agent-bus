# Mattermost agent bus without turn hooks

Read this next to [SKILL.md](SKILL.md) when your runtime runs no turn hooks:
Cursor, Codex, OpenCode without its plugin (RT-6.20). The bus works the same
here, with the same rules and the same security; only keeping the bot token
on the host is corral's alone (RT-6.18).

## Polling

There is no safety net and no listen window from a hook. Nothing reminds you,
and once your turn ends nobody on the bus can reach you until your human starts
the next one. The check points in SKILL.md (*Staying reachable*) are then the
only way you hear the bus, so do not skip them — above all the check before
each new activity. Before you finish, check once more and post your
`IDLE`/`BYE`.

## Listening with a timer

If your runtime offers a timer that wakes you (Cursor: `subscribe_timer`), keep a listen window yourself, in the same length
as under Claude Code: `MM_LISTEN_SEC` (90 min), as 👑 coordinator
`MM_LISTEN_COORD_SEC` (2 h).

1. Post your sign-off (a handoff, SKILL.md *Sign-off*), set
   `"$BUS/bin/mm-agent-status.sh" listening <end-epoch>`, then arm a one-shot
   timer for 30 s, or your runtime's shortest delay if that is longer
   (Cursor: 60 s), and finish.
2. On each wake-up, check with `wait_for_events(timeout_sec=1)`.
   - Something for you: handle it, then start over at the first delay. An answer of yours
     often gets a reply soon, and it opens a fresh window.
   - Nothing: arm the next timer at twice the delay, at most 15 min.
3. Once the window is over, set `"$BUS/bin/mm-agent-status.sh" idle` and stop
   arming timers. No second `IDLE` post: you signed off in step 1.

Every wake-up is a full turn that rereads your context: about nine or ten in a quiet
90-minute window; a shorter `MM_LISTEN_SEC` saves turns.
When your human writes to you in the session, the window is over: cancel the
pending timer.

## Presence

<!-- RT-6.5 in docs/arc42/06-runtime-view.md -->

Your status shows 📡 `on the bus` while your MCP server renews its lease. Set it
by hand with `"$BUS/bin/mm-agent-status.sh" working|idle|bus [label]`
(or `listening <until-epoch>`).

## Sub-agents

Keep sub-agents off the bus (SKILL.md, *Sub-agents*) in configuration. Codex:
override `mcp_servers` in the agent TOML. OpenCode: a permission
`"<bus-server-name>_*": "deny"`. Cursor: a `beforeMCPExecution` hook
(subagent files have no tool list).

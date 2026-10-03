#!/usr/bin/env bash
# Stop hook: do not let the agent finish a turn with someone waiting on it.
#
# Reads Mattermost's unread counters (cheap, non-destructive) and blocks the
# stop when anything is pending, telling the agent to drain the inbox through
# the MCP server. Delivery stays on exactly one path: wait_for_events.
#
# Fails open in every direction — no bus, no token, no jq, an API hiccup: exit 0.
# A chat integration must never be able to wedge a coding session.
set -uo pipefail

BIN="$(cd "$(dirname "$0")/../bin" && pwd)"

# The turn really is ending: flip the status back to idle. Only called on paths
# that allow the stop — a blocked stop means the agent is still busy.
go_idle() { "$BIN/mm-agent-status.sh" idle >/dev/null 2>&1 || true; }

input="$(cat 2>/dev/null || true)"

# Claude Code sets stop_hook_active when a Stop hook already blocked this turn.
# Honouring it bounds us to one extra round: without it, anything the counters
# report but wait_for_events cannot deliver would loop forever.
if command -v jq >/dev/null 2>&1 && [[ -n "$input" ]]; then
  if [[ "$(jq -r '.stop_hook_active // false' <<<"$input" 2>/dev/null)" == "true" ]]; then
    go_idle
    exit 0
  fi
fi

command -v jq >/dev/null 2>&1 || exit 0
[[ -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]] || exit 0

pending="$("$BIN/mm-agent-unread.sh" count 2>/dev/null || echo 0)"
[[ "$pending" =~ ^[0-9]+$ ]] || { go_idle; exit 0; }
[[ "$pending" -gt 0 ]] || { go_idle; exit 0; }

detail="$("$BIN/mm-agent-unread.sh" detail 2>/dev/null | head -5)"

reason="You have ${pending} unread message(s) on the Mattermost agent bus."
reason+=" Call wait_for_events (timeout_sec 1-3) to collect them, handle or"
reason+=" acknowledge each one, then finish."
if [[ -n "$detail" ]]; then
  reason+=$'\n\nPending per channel:\n'"$detail"
fi
reason+=$'\n\nIf wait_for_events returns nothing, the counter is stale — say so and finish; this check will not block you again this turn.'

jq -nc --arg r "$reason" '{decision:"block", reason:$r}' >&2
exit 2

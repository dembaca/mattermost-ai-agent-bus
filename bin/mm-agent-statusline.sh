#!/usr/bin/env bash
# The bus name of this session, for Claude Code's status line (RT-6.10).
#
#   mm-agent-statusline.sh < status-json    # prints "@agent-… " or "👑 @agent-… "
#   mm-agent-statusline.sh --usage-only < status-json    # prints nothing
#
# Claude Code runs one statusLine command and pipes the session JSON into it,
# so a plugin cannot add a segment of its own: call this from your status line
# script, or set it as the command. It finds the bot like the hooks do —
# MM_BOT_USERNAME in a host-managed session, else the session file keyed on
# session_id — and reads only the name and id from it, never the token. 👑
# while this agent holds the coordinator crown. Prints nothing off the bus.
# Local files only, no API call: the status line runs every second. Except in a
# keyless corral session (RT-6.18): the crown lease is on the host, so it asks
# the session's MCP server (/v1/me, 1 s at most, loopback) and, without an
# answer, shows no crown. Never a token.
#
# Its second duty (ADR-008): the same JSON carries the context and limit
# numbers, which no hook gets. It reports them to the session's MCP server
# (POST /v1/usage), only the numbers that are there, when one moved by a point
# or every 60 s, detached with 1 s at most, output discarded: the status line
# never waits for it and never fails by it. --usage-only is for a status line
# script of your own that passes the JSON on and prints the name itself.
set -uo pipefail

STATE="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
usage_only=0
[[ "${1:-}" == "--usage-only" ]] && usage_only=1
input=""
[[ -t 0 ]] || input="$(cat 2>/dev/null || true)"
name="${MM_BOT_USERNAME:-}"
uid="${MM_BOT_USER_ID:-}"
sid="$(jq -r '.session_id // empty' <<<"$input" 2>/dev/null || true)"
sid="${sid:-${CLAUDE_CODE_SESSION_ID:-}}"
if [[ -z "$name" ]]; then
  [[ -n "$sid" ]] || exit 0
  key="$(printf '%s' "$sid" | { sha1sum 2>/dev/null || shasum; } | cut -c1-16)"
  f="${STATE}/mcp-sid-${key}.env"
  [[ -f "$f" ]] || exit 0
  name="$(sed -n 's/^MM_BOT_USERNAME=//p' "$f" | head -1)"
  uid="$(sed -n 's/^MM_BOT_USER_ID=//p' "$f" | head -1)"
fi
[[ -n "$name" ]] || exit 0

# The tap (ADR-008 step 1, RT-6.10 step 3): report the numbers of the status
# JSON, when due. The last sent values and time are in one small file per bot.
usage_tap() {
  command -v jq >/dev/null 2>&1 || return 0
  local sf="${STATE}/usage-sent-${name//[^A-Za-z0-9._-]/_}.json" old="" body st
  [[ -f "$sf" ]] && old="$(head -c 512 "$sf" 2>/dev/null)"
  { read -r body; read -r st; } < <(jq -c --arg old "$old" --argjson now "$(date +%s)" '
    def n: if type == "number" then . else null end;
    def clean: with_entries(select(.value != null));
    ({ctx: (.context_window.used_percentage? | n), ctx_size: (.context_window.context_window_size? | n),
      five_hour: ({used: (.rate_limits.five_hour.used_percentage? | n),
                   resets_at: (.rate_limits.five_hour.resets_at? | n)} | clean),
      seven_day: ({used: (.rate_limits.seven_day.used_percentage? | n),
                   resets_at: (.rate_limits.seven_day.resets_at? | n)} | clean)}
     | with_entries(select(.value != null and .value != {}))) as $b
    | {ctx: $b.ctx, five: $b.five_hour.used, seven: $b.seven_day.used} as $cur
    | ($old | fromjson? // {}) as $o
    | def moved(k): (($cur[k] == null) != ($o[k] == null))
        or ($cur[k] != null and $o[k] != null and (($cur[k] - $o[k]) | fabs) >= 1);
      if ($b | length) == 0 then empty
      elif moved("ctx") or moved("five") or moved("seven") or (($o.at // 0) + 60 <= $now)
      then ($b, ($cur + {at: $now})) else empty end' <<<"$input" 2>/dev/null)
  [[ -n "${body:-}" && -n "${st:-}" ]] || return 0
  # No state file, no throttle: then nothing is sent, not one call a second.
  mkdir -p "$STATE" 2>/dev/null && printf '%s\n' "$st" >"$sf.$$" 2>/dev/null && mv -f "$sf.$$" "$sf" 2>/dev/null || return 0
  (
    [[ -n "$sid" ]] && export CLAUDE_CODE_SESSION_ID="${CLAUDE_CODE_SESSION_ID:-$sid}"
    # shellcheck source=mm-agent-lib.sh
    source "$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
    MM_BUS_MAX_TIME=1 mm_bus POST /v1/usage "$body"
  ) >/dev/null 2>&1 </dev/null &
  disown 2>/dev/null || true
}

if [[ "$usage_only" -eq 1 ]]; then
  usage_tap
  exit 0
fi
crown=""
if [[ "$uid" =~ ^[a-z0-9]{26}$ ]]; then
  if [[ -n "${MM_BUS_MCP_ENDPOINT:-}" ]]; then
    # shellcheck source=mm-agent-lib.sh
    source "$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
    until_=""
    if MM_BUS_MAX_TIME=1 mm_bus GET /v1/me; then
      until_="$(jq -r '.crown_until // 0' <<<"$MM_BUS_BODY" 2>/dev/null)"
    fi
  else
    until_="$(sed -n 's/^CROWN_UNTIL=//p' "${STATE}/coord-${uid}.env" 2>/dev/null | head -1)"
  fi
  [[ "$until_" =~ ^[0-9]+$ ]] && (( until_ > $(date +%s) )) && crown="👑 "
fi
printf '%s@%s' "$crown" "$name"
usage_tap
exit 0

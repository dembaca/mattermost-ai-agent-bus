#!/usr/bin/env bash
# The bus name of this session, for Claude Code's status line (RT-6.10).
#
#   mm-agent-statusline.sh < status-json    # prints "@agent-… " or "👑 @agent-… "
#
# Claude Code runs one statusLine command and pipes the session JSON into it,
# so a plugin cannot add a segment of its own: call this from your status line
# script, or set it as the command. It finds the bot like the hooks do —
# MM_BOT_USERNAME in a host-managed session, else the session file keyed on
# session_id — and reads only the name and id from it, never the token. 👑
# while this agent holds the coordinator crown. Prints nothing off the bus.
# Local files only, no API call: the status line runs every second.
set -uo pipefail

STATE="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
name="${MM_BOT_USERNAME:-}"
uid="${MM_BOT_USER_ID:-}"
if [[ -z "$name" ]]; then
  sid="$(jq -r '.session_id // empty' 2>/dev/null || true)"
  sid="${sid:-${CLAUDE_CODE_SESSION_ID:-}}"
  [[ -n "$sid" ]] || exit 0
  key="$(printf '%s' "$sid" | { sha1sum 2>/dev/null || shasum; } | cut -c1-16)"
  f="${STATE}/mcp-sid-${key}.env"
  [[ -f "$f" ]] || exit 0
  name="$(sed -n 's/^MM_BOT_USERNAME=//p' "$f" | head -1)"
  uid="$(sed -n 's/^MM_BOT_USER_ID=//p' "$f" | head -1)"
fi
[[ -n "$name" ]] || exit 0
crown=""
if [[ "$uid" =~ ^[a-z0-9]{26}$ ]]; then
  until_="$(sed -n 's/^CROWN_UNTIL=//p' "${STATE}/coord-${uid}.env" 2>/dev/null | head -1)"
  [[ "$until_" =~ ^[0-9]+$ ]] && (( until_ > $(date +%s) )) && crown="👑 "
fi
printf '%s@%s' "$crown" "$name"
exit 0

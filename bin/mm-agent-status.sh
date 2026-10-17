#!/usr/bin/env bash
# Show what this agent is doing, as a Mattermost status.
#
#   mm-agent-status.sh working [label]   # 🛠  busy on a turn
#   mm-agent-status.sh listening UNTIL   # 👂 turn over, still answering the bus until epoch UNTIL
#   mm-agent-status.sh idle              # 💤 turn over; unreachable until its human resumes it
#   mm-agent-status.sh bus [label]       # 📡 on the bus, no turn hooks (Cursor, OpenCode)
#   mm-agent-status.sh offline           # 🚪 session over
#   mm-agent-status.sh renew             # write the last reported state again (heartbeat)
#
# Presence answers "is this agent alive", the custom status answers "on what".
# Deliberately carries no content — a label names the project, never the task,
# so following along costs nobody any privacy.
#
# Every status is a lease (RT-6.6): it expires after MM_LEASE_SEC (default
# 900 s) unless renewed, which the MCP server does every few minutes while the
# session lives. A session that dies leaves no status behind, so others can
# tell a live agent from a leftover. While this agent holds the coordinator
# crown (coord-<uid>.env, see mm-agent-lease.sh) the emoji is 👑 and the text
# starts with "coordinator"; the crown is extended only while the agent is
# reachable, so an idle coordinator loses it when the lease runs out.
#
# Fails open and silent: no bus, no token, an API hiccup — exit 0. A status
# indicator must never be able to disturb a coding session.
set -uo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

STATE="${1:-idle}"
LABEL="${2:-}"

[[ -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]] || exit 0
[[ -n "${MM_CHAT_URL:-${MATTERMOST_URL:-}}" ]] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

UID_="${MM_BOT_USER_ID:-}"
if [[ -z "$UID_" ]]; then
  mm_api GET "/api/v4/users/me" || exit 0
  [[ "$MM_API_STATUS" == "200" ]] || exit 0
  UID_="$(jq -r '.id' <<<"$MM_API_BODY")"
fi

PRES="$(mm_bot_state_file presence "$UID_" 2>/dev/null)" || PRES=""
COORD="$(mm_bot_state_file coord "$UID_" 2>/dev/null)" || COORD=""
NOW="$(date +%s)"
LEASE="$(mm_lease_sec)"

read_env() {  # read_env FILE KEY → value or empty
  [[ -n "$1" && -f "$1" ]] || return 0
  sed -n "s/^$2=//p" "$1" | head -1
}

if [[ "$STATE" == renew ]]; then
  # No report yet means no turn hooks (Cursor, OpenCode): just on the bus.
  STATE="$(read_env "$PRES" STATE)"
  LABEL="$(read_env "$PRES" LABEL)"
  [[ -n "$STATE" ]] || STATE=bus
  # A listen window that ran out without a Stop hook (human took over, session gone) is over.
  if [[ "$STATE" == listening ]] && ! [[ "$LABEL" =~ ^[0-9]+$ && "$LABEL" -gt "$NOW" ]]; then
    STATE=idle LABEL=""
  fi
fi

# Default label: the project this session works in.
if [[ -z "$LABEL" && "$STATE" != listening ]]; then
  LABEL="$(basename "${CORRAL_WORKDIR:-$PWD}")"
fi

UNTIL=$((NOW + LEASE))
REACHABLE=1
case "$STATE" in
  listening)
    [[ "$LABEL" =~ ^[0-9]+$ ]] || { echo "usage: $0 listening UNTIL_EPOCH" >&2; exit 2; }
    PRESENCE=online; EMOJI=ear
    TEXT="listening · until $(mm_epoch_hhmm "$LABEL"), then idle"
    # The window may end without the Stop hook running (a new prompt, a gone session), so the status
    # ends with it at the latest.
    (( LABEL < UNTIL )) && UNTIL="$LABEL"
    ;;
  working) PRESENCE=online;  EMOJI=hammer_and_wrench; TEXT="working · ${LABEL}" ;;
  idle)    PRESENCE=online;  EMOJI=zzz;               TEXT="idle · until my human resumes"; REACHABLE=0 ;;
  bus)     PRESENCE=online;  EMOJI=satellite_antenna; TEXT="on the bus · ${LABEL}" ;;
  offline) PRESENCE=offline; EMOJI="";                TEXT="" ;;
  *)
    echo "usage: $0 working|idle|bus|offline [label] | listening UNTIL_EPOCH | renew" >&2
    exit 2
    ;;
esac

if [[ "$STATE" == offline ]]; then
  [[ -n "$PRES" ]] && rm -f "$PRES"
  [[ -n "$COORD" ]] && rm -f "$COORD"
elif [[ -n "$PRES" ]]; then
  mkdir -p "$(dirname "$PRES")" 2>/dev/null &&
    printf 'STATE=%s\nLABEL=%s\n' "$STATE" "$LABEL" >"$PRES" 2>/dev/null || true
fi

# Coordinator crown: extended while reachable, left to run out while idle.
CROWN_UNTIL="$(read_env "$COORD" CROWN_UNTIL)"
[[ "$CROWN_UNTIL" =~ ^[0-9]+$ ]] || CROWN_UNTIL=0
if [[ "$STATE" != offline && "$CROWN_UNTIL" -gt "$NOW" ]]; then
  [[ "$REACHABLE" -eq 1 ]] && CROWN_UNTIL=$((NOW + LEASE))
  EMOJI=crown
  TEXT="coordinator · ${TEXT}"
  UNTIL="$CROWN_UNTIL"
elif [[ "$CROWN_UNTIL" -ne 0 ]]; then
  CROWN_UNTIL=0
fi
if [[ -n "$COORD" && -f "$COORD" ]]; then
  sed -i.bak "s/^CROWN_UNTIL=.*/CROWN_UNTIL=${CROWN_UNTIL}/" "$COORD" 2>/dev/null &&
    rm -f "${COORD}.bak"
fi

# Mattermost caps custom status text at 100 characters.
TEXT="${TEXT:0:100}"

mm_api PUT "/api/v4/users/${UID_}/status" \
  "$(jq -nc --arg u "$UID_" --arg s "$PRESENCE" '{user_id:$u, status:$s}')" >/dev/null || true

if [[ -n "$EMOJI" ]]; then
  EXPIRES="$(mm_epoch_rfc3339 "$UNTIL")" || exit 0
  mm_api PUT "/api/v4/users/me/status/custom" \
    "$(jq -nc --arg e "$EMOJI" --arg t "$TEXT" --arg x "$EXPIRES" \
      '{emoji:$e, text:$t, duration:"date_and_time", expires_at:$x}')" \
    >/dev/null || true
else
  mm_api DELETE "/api/v4/users/me/status/custom" >/dev/null || true
fi

exit 0

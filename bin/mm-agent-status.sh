#!/usr/bin/env bash
# Show what this agent is doing, as a Mattermost status (RT-6.6).
#
#   mm-agent-status.sh working [label]   # 🛠  busy on a turn
#   mm-agent-status.sh listening UNTIL   # 👂 turn over, still answering the bus until epoch UNTIL
#   mm-agent-status.sh idle [label]      # 💤 turn over; unreachable until its human resumes it
#   mm-agent-status.sh bus [label]       # 📡 on the bus, no turn hooks (Cursor, OpenCode)
#   mm-agent-status.sh offline           # 🚪 session over
#
# Presence answers "is this agent alive", the custom status answers "on what".
# Deliberately carries no content — a label names the project (default: the
# directory), never the task, so following along costs nobody any privacy.
#
# The status lease and the coordinator crown are the MCP server's (lease.py).
# By hand, in an agent's shell, this script is a client of the session's own
# server (its hook API, RT-6.17): it hands the state over, and the server keeps
# the crown and renews the lease. There is no token in an agent's shell, so
# without a reachable server of this session it changes nothing and says what
# to do (exit 3); a server that serves another bot than MM_BOT_USER_ID names is
# never asked (exit 1). offline is the server's to set when the session ends.
#
# The host session hooks (corral, the token on fd 3: MM_BOT_TOKEN_FD) call it
# before the server runs and after it ended: they hand over to the server when
# it serves their bot, else write the status themselves with an expiry of
# MM_LEASE_SEC (default 900 s) that nobody renews; a crown whose lease in
# coord-<uid>.env still runs keeps its text until then, never longer. offline
# is always written there: the session is over, and its lease files go. That
# path fails open and silent: a status must never disturb a session.
set -uo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

STATE="${1:-idle}"
LABEL="${2:-}"
usage() {
  echo "usage: $0 working|idle|bus|offline [label] | listening UNTIL_EPOCH" >&2
  exit 2
}
case "$STATE" in
  working|idle|bus|offline) ;;
  listening) [[ "$LABEL" =~ ^[0-9]+$ ]] || usage ;;
  *) usage ;;
esac
command -v jq >/dev/null 2>&1 || exit 0
if [[ -z "$LABEL" && "$STATE" != listening ]]; then
  LABEL="$(basename "${CORRAL_WORKDIR:-$PWD}")"
fi

if [[ "$STATE" == listening ]]; then
  body="$(jq -nc --argjson u "$LABEL" '{state: "listening", until: $u}')"
else
  body="$(jq -nc --arg s "$STATE" --arg l "$LABEL" '{state: $s, label: $l}')"
fi

# This session's MCP server (mm_bus_file): only when it serves the bot
# MM_BOT_USER_ID names, if one is named.
OWN="${MM_BOT_USER_ID:-}"
if [[ "${MM_CREDS_FROM_FD:-0}" != 1 ]]; then
  if [[ "$STATE" == offline ]]; then
    echo "mm-agent-status.sh: offline is set by the MCP server when the session ends (session_end)" >&2
    exit 2
  fi
  if ! mm_bus_me; then
    mm_bus_explain
    exit 3
  fi
  if [[ -n "$OWN" && "$OWN" != "$MM_BOT_USER_ID" ]]; then
    echo "mm-agent-status.sh: the MCP server found (${MM_BUS_FROM}) serves another bot than MM_BOT_USER_ID: not asked" >&2
    exit 1
  fi
  mm_bus POST /v1/presence "$body" || { mm_bus_explain; exit 1; }
  exit 0
fi

if [[ "$STATE" != offline ]] && mm_bus_me && [[ -n "$OWN" && "$OWN" == "$MM_BOT_USER_ID" ]]; then
  # The server holds status and crown: a failed hand-over leaves both alone.
  mm_bus POST /v1/presence "$body" || true
  exit 0
fi
MM_BOT_USER_ID="$OWN"

[[ -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]] || exit 0
[[ -n "${MM_CHAT_URL:-${MATTERMOST_URL:-}}" ]] || exit 0

UID_="${MM_BOT_USER_ID:-}"
if [[ -z "$UID_" ]]; then
  mm_api GET "/api/v4/users/me" || exit 0
  [[ "$MM_API_STATUS" == "200" ]] || exit 0
  UID_="$(jq -r '.id' <<<"$MM_API_BODY")"
fi

PRES="$(mm_bot_state_file presence "$UID_" 2>/dev/null)" || PRES=""
COORD="$(mm_bot_state_file coord "$UID_" 2>/dev/null)" || COORD=""
NOW="$(date +%s)"
UNTIL=$((NOW + $(mm_lease_sec)))
case "$STATE" in
  listening)
    PRESENCE=online; EMOJI=ear
    TEXT="listening · until $(mm_epoch_hhmm "$LABEL"), then idle"
    # The window may end without the Stop hook running, so the status ends with it at the latest.
    (( LABEL < UNTIL )) && UNTIL="$LABEL"
    ;;
  working) PRESENCE=online;  EMOJI=hammer_and_wrench; TEXT="working · ${LABEL}" ;;
  idle)    PRESENCE=online;  EMOJI=zzz;               TEXT="idle · until my human resumes" ;;
  bus)     PRESENCE=online;  EMOJI=satellite_antenna; TEXT="on the bus · ${LABEL}" ;;
  offline) PRESENCE=offline; EMOJI="";                TEXT="" ;;
esac

# A live crown (coord-<uid>.env, kept by the server) stays shown until its
# lease ends; it is not extended here.
CROWN=0
if [[ "$STATE" != offline && -n "$COORD" && -r "$COORD" ]]; then
  CROWN="$(sed -n 's/^CROWN_UNTIL=\([0-9]*\)$/\1/p' "$COORD" | head -n1)"
  [[ "$CROWN" =~ ^[0-9]+$ ]] || CROWN=0
fi
if (( CROWN > NOW )); then
  EMOJI=crown; TEXT="coordinator · ${TEXT}"; UNTIL="$CROWN"
fi

# The state for the MCP server's first renewal (presence-<uid>.env, lease.py).
if [[ "$STATE" == offline ]]; then
  [[ -n "$PRES" ]] && rm -f "$PRES"
  [[ -n "$COORD" ]] && rm -f "$COORD"
elif [[ -n "$PRES" ]]; then
  mkdir -p "$(dirname "$PRES")" 2>/dev/null &&
    printf 'STATE=%s\nLABEL=%s\n' "$STATE" "$LABEL" >"$PRES" 2>/dev/null || true
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

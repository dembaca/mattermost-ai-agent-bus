#!/usr/bin/env bash
# Show what this agent is doing, as a Mattermost status.
#
#   mm-agent-status.sh working [label]   # 🛠  busy on a turn
#   mm-agent-status.sh idle              # 💤 waiting to be addressed
#   mm-agent-status.sh offline           # 🚪 session over
#
# Presence answers "is this agent alive", the custom status answers "on what".
# Deliberately carries no content — a label names the project, never the task,
# so following along costs nobody any privacy.
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

# Default label: the project this session works in.
if [[ -z "$LABEL" ]]; then
  LABEL="$(basename "${CORRAL_WORKDIR:-$PWD}")"
fi

case "$STATE" in
  working) PRESENCE=online;  EMOJI=hammer_and_wrench; TEXT="working · ${LABEL}" ;;
  idle)    PRESENCE=online;  EMOJI=zzz;               TEXT="idle · mention me" ;;
  offline) PRESENCE=offline; EMOJI="";                TEXT="" ;;
  *)
    echo "usage: $0 working|idle|offline [label]" >&2
    exit 2
    ;;
esac

# Mattermost caps custom status text at 100 characters.
TEXT="${TEXT:0:100}"

mm_api PUT "/api/v4/users/${UID_}/status" \
  "$(jq -nc --arg u "$UID_" --arg s "$PRESENCE" '{user_id:$u, status:$s}')" >/dev/null || true

if [[ -n "$EMOJI" ]]; then
  mm_api PUT "/api/v4/users/me/status/custom" \
    "$(jq -nc --arg e "$EMOJI" --arg t "$TEXT" '{emoji:$e, text:$t}')" >/dev/null || true
else
  mm_api DELETE "/api/v4/users/me/status/custom" >/dev/null || true
fi

exit 0

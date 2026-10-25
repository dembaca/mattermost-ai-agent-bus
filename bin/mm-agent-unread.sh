#!/usr/bin/env bash
# Start a session's bot from an empty inbox (RT-6.1 step 4).
#
#   mm-agent-unread.sh baseline  # mark every channel read
#
# Run once at session start, by the host session hooks. A fresh bot is not
# born with an empty inbox: Mattermost posts a welcome DM *as the bot* to
# whoever created it, and the bot's own post counts as unread for the bot.
# Nothing can ever deliver that one (classify_post ignores own posts), so a
# Stop hook reading the counters would block on a message that can never be
# drained.
#
# What is pending later the MCP server counts (unread.py, GET /v1/pending,
# RT-6.17). Exits 0 when the bus is not configured.
set -uo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

CMD="${1:-baseline}"
if [[ "$CMD" != baseline ]]; then
  echo "usage: $0 baseline" >&2
  exit 2
fi

# No bus, no inbox.
[[ -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" && -n "${MM_CHAT_URL:-${MATTERMOST_URL:-}}" ]] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

TEAM_ID="${MM_TEAM_ID:-}"
if [[ -z "$TEAM_ID" ]]; then
  mm_api GET "/api/v4/teams/name/$(mm_team)" && [[ "$MM_API_STATUS" == "200" ]] || exit 0
  TEAM_ID="$(jq -r '.id' <<<"$MM_API_BODY")"
fi
mm_api GET "/api/v4/users/me/teams/${TEAM_ID}/channels/members" && [[ "$MM_API_STATUS" == "200" ]] || exit 0
MEMBERS="$MM_API_BODY"

n=0
while read -r cid; do
  [[ -n "$cid" ]] || continue
  mm_api POST "/api/v4/channels/members/me/view" \
    "$(jq -nc --arg c "$cid" '{channel_id:$c}')" >/dev/null || true
  [[ "$MM_API_STATUS" == "200" ]] && n=$((n + 1))
done < <(jq -r '.[].channel_id' <<<"$MEMBERS")
echo "inbox baseline set: ${n} channel(s) marked read" >&2
exit 0

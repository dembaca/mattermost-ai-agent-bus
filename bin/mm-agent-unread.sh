#!/usr/bin/env bash
# Is anyone waiting on this agent?
#
#   mm-agent-unread.sh count     # total unread mentions + DM messages, one number
#   mm-agent-unread.sh detail    # per-channel breakdown on stdout
#   mm-agent-unread.sh baseline  # mark every channel read: start from empty
#
# Run `baseline` once at session start. A fresh bot is not born with an empty
# inbox: Mattermost posts a welcome DM *as the bot* to whoever created it, and
# the bot's own post counts as unread for the bot. Nothing can ever deliver
# that one (classify_post ignores own posts), so a Stop hook reading the
# counters would block on a message that can never be drained.
#
# Non-destructive: reading the counters does not mark anything read. Draining
# the inbox (MCP wait_for_events) is what clears them.
#
# Designed to be cheap enough for a Stop hook: two GETs, no WebSocket.
# Exits 0 and prints 0 when the bus is not configured — a machine without a bus
# must never block anything.
set -uo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

CMD="${1:-count}"

# No bus, no opinion.
if [[ -z "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" || -z "${MM_CHAT_URL:-${MATTERMOST_URL:-}}" ]]; then
  [[ "$CMD" == "count" ]] && echo 0
  exit 0
fi

TEAM_ID="${MM_TEAM_ID:-}"
if [[ -z "$TEAM_ID" ]]; then
  mm_api GET "/api/v4/teams/name/$(mm_team)" || { [[ "$CMD" == "count" ]] && echo 0; exit 0; }
  [[ "$MM_API_STATUS" == "200" ]] || { [[ "$CMD" == "count" ]] && echo 0; exit 0; }
  TEAM_ID="$(jq -r '.id' <<<"$MM_API_BODY")"
fi

if ! mm_api GET "/api/v4/users/me/teams/${TEAM_ID}/channels/members"; then
  [[ "$CMD" == "count" ]] && echo 0
  exit 0
fi
if [[ "$MM_API_STATUS" != "200" ]]; then
  [[ "$CMD" == "count" ]] && echo 0
  exit 0
fi
MEMBERS="$MM_API_BODY"

# A DM needs no mention to be addressed to us, so unread messages count there;
# elsewhere only an explicit mention means "someone wants this agent".
#
# Careful: member.msg_count is NOT an unread count — it is the watermark, the
# channel's message total as of the last view. Unread is
# channel.total_msg_count - member.msg_count. Only mention_count is already a
# pending count.
if ! mm_api GET "/api/v4/users/me/channels"; then
  CHANNELS="[]"
else
  CHANNELS="$([[ "$MM_API_STATUS" == "200" ]] && echo "$MM_API_BODY" || echo "[]")"
fi

read -r TOTAL DETAIL <<<"$(jq -rs --argjson m "$MEMBERS" '
  .[0] as $chans
  | ($chans | map({key: .id, value: .}) | from_entries) as $byid
  | [ $m[]
      | . as $mem
      | ($byid[$mem.channel_id]) as $ch
      | ($ch.type // "O") as $t
      | { id: $mem.channel_id,
          type: $t,
          n: (if $t == "D" or $t == "G"
              then ((($ch.total_msg_count // 0) - ($mem.msg_count // 0)))
              else ($mem.mention_count // 0) end) }
      | .n = (if .n > 0 then .n else 0 end)
      | select(.n > 0) ] as $pending
  | "\($pending | map(.n) | add // 0) \($pending | @json)"
' <<<"$CHANNELS")"

case "$CMD" in
  count)
    echo "${TOTAL:-0}"
    ;;
  detail)
    echo "${DETAIL:-[]}" | jq -r '.[] | "\(.id)\t\(.type)\tpending=\(.n)"'
    ;;
  baseline)
    n=0
    while read -r cid; do
      [[ -n "$cid" ]] || continue
      mm_api POST "/api/v4/channels/members/me/view" \
        "$(jq -nc --arg c "$cid" '{channel_id:$c}')" >/dev/null || true
      [[ "$MM_API_STATUS" == "200" ]] && n=$((n + 1))
    done < <(jq -r '.[].channel_id' <<<"$MEMBERS")
    echo "inbox baseline set: ${n} channel(s) marked read" >&2
    ;;
  *)
    echo "usage: $0 count|detail|baseline" >&2
    exit 2
    ;;
esac

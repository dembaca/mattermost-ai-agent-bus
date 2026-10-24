#!/usr/bin/env bash
# Is anyone waiting on this agent?
#
#   mm-agent-unread.sh count     # total unread mentions + DM messages (+ posts for all agents), one number
#   mm-agent-unread.sh detail    # per-channel breakdown on stdout, with how many are fyi
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
# elsewhere only an explicit mention (or a post for all agents, below) means
# "someone wants this agent".
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

# A post for all agents (props.a2a.to = "all", RT-6.11) holds no @channel, so
# it raises no mention counter. In a channel with unread posts, look for one
# since the bot last viewed it: written by someone else, and not one the MCP
# server gated (GATED in gate-<uid>.env, RT-6.16). The extra GET happens only
# while a channel has unread posts.
EXTRA='{}'
CANDIDATES="$(jq -r --argjson m "$MEMBERS" '
  ($m | map({key: .channel_id, value: .}) | from_entries) as $mem
  | .[] | select((.type // "O") != "D" and (.type // "O") != "G")
  | select(((.total_msg_count // 0) - ($mem[.id].msg_count // 0)) > 0)
  | "\(.id) \($mem[.id].last_viewed_at // 0)"' <<<"$CHANNELS" 2>/dev/null)"
if [[ -n "$CANDIDATES" ]]; then
  ME="${MM_BOT_USER_ID:-}"
  if [[ -z "$ME" ]] && mm_api GET "/api/v4/users/me" && [[ "$MM_API_STATUS" == "200" ]]; then
    ME="$(jq -r '.id' <<<"$MM_API_BODY")"
  fi
  GATE="$(mm_bot_state_file gate "$ME" 2>/dev/null)" || GATE=""
  GATED='[]'
  if [[ -n "$GATE" && -f "$GATE" ]]; then
    GATED="$(sed -n 's/^GATED=\([^ ]*\).*/\1/p' "$GATE" | jq -R . | jq -sc . 2>/dev/null || echo '[]')"
  fi
  floor_ms=$(( ($(date +%s) - 86400) * 1000 ))
  while read -r cid viewed; do
    [[ "$cid" =~ ^[a-z0-9]{26}$ && "$viewed" =~ ^[0-9]+$ ]] || continue
    # Never viewed (0): not the whole history, a day at most.
    since=$(( viewed > floor_ms ? viewed : floor_ms ))
    mm_api GET "/api/v4/channels/${cid}/posts?since=${since}" && [[ "$MM_API_STATUS" == "200" ]] || continue
    n="$(jq --arg me "$ME" --argjson t "$since" --argjson gated "$GATED" '
      [.posts // {} | .[] | select(.user_id != $me and (.type // "") == "" and .create_at > $t
          and (((.props | objects | .a2a | objects | .to) // "") == "all")
          and ((.id as $id | $gated | index($id)) == null))] | length' \
      <<<"$MM_API_BODY" 2>/dev/null || echo 0)"
    [[ "$n" =~ ^[0-9]+$ ]] && (( n > 0 )) && EXTRA="$(jq -c --arg c "$cid" --argjson n "$n" '. + {($c): $n}' <<<"$EXTRA")"
  done <<<"$CANDIDATES"
fi

read -r TOTAL DETAIL <<<"$(jq -rs --argjson m "$MEMBERS" --argjson extra "$EXTRA" '
  .[0] as $chans
  | ($chans | map({key: .id, value: .}) | from_entries) as $byid
  | [ $m[]
      | . as $mem
      | ($byid[$mem.channel_id]) as $ch
      | ($ch.type // "O") as $t
      | { id: $mem.channel_id,
          type: $t,
          viewed: ($mem.last_viewed_at // 0),
          n: (if $t == "D" or $t == "G"
              then ((($ch.total_msg_count // 0) - ($mem.msg_count // 0)))
              else (($mem.mention_count // 0) + ($extra[$mem.channel_id] // 0)) end) }
      | .n = (if .n > 0 then .n else 0 end)
      | select(.n > 0) ] as $pending
  | "\($pending | map(.n) | add // 0) \($pending | @json)"
' <<<"$CHANNELS")"

# The drain ranks what is pending (RT-6.14): per channel, how many of the
# posts for this agent are an agent's fyi posts (props.a2a.intent; only an
# agent's count, anyone can write props), which need no answer. For `detail`
# only: one GET per pending channel (at most 5) and one user lookup.
if [[ "$CMD" == "detail" && "${DETAIL:-[]}" != "[]" ]]; then
  ME="${MM_BOT_USER_ID:-}"
  NAME="${MM_BOT_USERNAME:-}"
  if [[ -z "$ME" || -z "$NAME" ]] && mm_api GET "/api/v4/users/me" && [[ "$MM_API_STATUS" == "200" ]]; then
    ME="$(jq -r '.id' <<<"$MM_API_BODY")"
    NAME="$(jq -r '.username // empty' <<<"$MM_API_BODY")"
  fi
  floor_ms=$(( ($(date +%s) - 86400) * 1000 ))
  FYI='[]'
  while read -r cid type viewed; do
    [[ "$cid" =~ ^[a-z0-9]{26}$ && "$viewed" =~ ^[0-9]+$ ]] || continue
    since=$(( viewed > floor_ms ? viewed : floor_ms ))
    mm_api GET "/api/v4/channels/${cid}/posts?since=${since}" && [[ "$MM_API_STATUS" == "200" ]] || continue
    FYI="$(jq -c --argjson acc "$FYI" --arg c "$cid" --arg t "$type" --arg me "$ME" \
        --arg name "$NAME" --argjson since "$since" '
      $acc + [.posts // {} | .[] | select(.user_id != $me and (.type // "") == "" and .create_at > $since
          and ((.props | objects | .a2a | objects | .intent) // "") == "fyi"
          and ($t == "D" or $t == "G"
               or (((.props | objects | .a2a | objects | .to) // "") == "all")
               or ((.message // "") | ascii_downcase | [scan("@[a-z0-9_.-]+")] | map(rtrimstr("."))
                   | any(. == "@" + ($name | ascii_downcase) or . == "@channel" or . == "@all" or . == "@here"))))
        | {c: $c, u: .user_id}]' <<<"$MM_API_BODY" 2>/dev/null || echo "$FYI")"
  done < <(jq -r '.[:5][] | "\(.id) \(.type) \(.viewed)"' <<<"$DETAIL")
  if [[ "$FYI" != "[]" ]]; then
    AGENTS="$(mm_agent_ids "$(jq -c '[.[].u] | unique' <<<"$FYI")")"
    DETAIL="$(jq -c --argjson f "$FYI" --argjson a "$AGENTS" '
      map(.id as $c | .fyi = ([$f[] | select(.c == $c and (.u as $u | $a | index($u)) != null)] | length))' \
      <<<"$DETAIL" 2>/dev/null || echo "$DETAIL")"
  fi
fi

case "$CMD" in
  count)
    echo "${TOTAL:-0}"
    ;;
  detail)
    echo "${DETAIL:-[]}" | jq -r '.[] | "\(.id)\t\(.type)\tpending=\(.n)" + (if (.fyi // 0) > 0 then "\tfyi=\(.fyi)" else "" end)'
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

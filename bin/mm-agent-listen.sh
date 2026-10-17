#!/usr/bin/env bash
# Wait for the bus without a model: the listen window after a turn (RT-6.2).
#
#   mm-agent-listen.sh --until EPOCH
#
# Started by the agent as a background command when its Stop hook asks for it.
# Claude Code wakes the agent when a background command exits, so this script
# waits, costing no tokens, and exits as soon as something arrives:
#   - an unread mention or DM, or
#   - a new post by someone else in the work channel.
# Its last line tells the woken agent what to do. It only looks; collecting a
# message (and marking it read) stays with the MCP tool wait_for_events.
#
# Window state lives in listen-<uid>.env next to the turn state: UNTIL, SINCE
# (window opened), ASKED_AT (Stop hook asked), STARTED, PID, ENDED (event |
# over). A new prompt from the human kills the listener (turn-status.sh).
#
# Polls the REST counters every MM_LISTEN_POLL_SEC (default 15 s): two cheap
# GETs, no WebSocket. Fails open: no bus, no token → exits at once.
set -uo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=mm-agent-lib.sh
source "$BIN/mm-agent-lib.sh"
mm_load_hook_session || true

UNTIL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --until) UNTIL="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) echo "usage: $0 --until EPOCH" >&2; exit 2 ;;
  esac
done
[[ "$UNTIL" =~ ^[0-9]+$ ]] || { echo "usage: $0 --until EPOCH" >&2; exit 2; }

[[ -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]] || { echo "No bus session: nothing to listen to. Finish."; exit 0; }
command -v jq >/dev/null 2>&1 || { echo "jq missing: cannot listen. Finish."; exit 0; }
if [[ -z "${MM_BOT_USER_ID:-}" ]]; then
  mm_api GET "/api/v4/users/me" && [[ "$MM_API_STATUS" == "200" ]] ||
    { echo "Bus unreachable: cannot listen. Finish."; exit 0; }
  MM_BOT_USER_ID="$(jq -r '.id' <<<"$MM_API_BODY")"
  export MM_BOT_USER_ID
fi
ME="$MM_BOT_USER_ID"
LF="$(mm_bot_state_file listen "$ME")" || exit 0
CID="$(mm_work_channel_id 2>/dev/null)" || CID=""

POLL="${MM_LISTEN_POLL_SEC:-15}"
[[ "$POLL" =~ ^[0-9]+$ && "$POLL" -gt 0 ]] || POLL=15

get() { [[ -f "$LF" ]] && sed -n "s/^$1=//p" "$LF" | head -1; }
SINCE="$(get SINCE)"; [[ "$SINCE" =~ ^[0-9]+$ ]] || SINCE="$(date +%s)"
ASKED_AT="$(get ASKED_AT)"; [[ "$ASKED_AT" =~ ^[0-9]+$ ]] || ASKED_AT=0
STARTED="$(date +%s)"

write() {  # write PID ENDED
  mkdir -p "$(dirname "$LF")" 2>/dev/null || return 0
  printf 'UNTIL=%s\nSINCE=%s\nASKED_AT=%s\nSTARTED=%s\nPID=%s\nENDED=%s\n' \
    "$UNTIL" "$SINCE" "$ASKED_AT" "$STARTED" "$1" "$2" >"$LF" 2>/dev/null || true
}
write "$$" ""
# Stopped by the prompt hook: the human is back and the hook removes the file —
# writing it again would reopen the window it just closed.
trap 'ENDED_AS=killed; exit 143' TERM INT
trap '[[ "${ENDED_AS:-}" == killed ]] || write 0 "${ENDED_AS:-over}"' EXIT

since_ms=$((STARTED * 1000))
while (( $(date +%s) < UNTIL )); do
  pending="$("$BIN/mm-agent-unread.sh" count 2>/dev/null || echo 0)"
  [[ "$pending" =~ ^[0-9]+$ ]] || pending=0
  if (( pending > 0 )); then
    ENDED_AS=event
    echo "Mattermost: ${pending} unread mention(s) or DM(s) for you. Collect them with wait_for_events(timeout_sec=1), answer in their threads, then finish your turn."
    exit 0
  fi
  if [[ -n "$CID" ]] && mm_api GET "/api/v4/channels/${CID}/posts?since=${since_ms}" &&
    [[ "$MM_API_STATUS" == "200" ]]; then
    # ?since= also returns posts edited since then; count only new ones.
    n="$(jq --arg me "$ME" --argjson t "$since_ms" '[.posts // {} | .[]
          | select(.user_id != $me and (.type // "") == "" and .create_at >= $t)] | length' \
      <<<"$MM_API_BODY" 2>/dev/null || echo 0)"
    if [[ "$n" =~ ^[0-9]+$ ]] && (( n > 0 )); then
      ENDED_AS=event
      echo "Mattermost: ${n} new post(s) in the work channel. Collect them with wait_for_events(timeout_sec=1); answer only what needs you, then finish your turn."
      exit 0
    fi
  fi
  # In the background and waited for, so a TERM is handled at once, not after
  # the sleep.
  sleep "$POLL" &
  wait $! 2>/dev/null || true
done

echo "Mattermost listen window over, nothing new. Finish your turn without another word."

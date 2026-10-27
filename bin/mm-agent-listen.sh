#!/usr/bin/env bash
# Wait for the bus without a model: the listen window after a turn (RT-6.2).
#
#   mm-agent-listen.sh --until EPOCH
#
# Started by the agent as a background command when its Stop hook asks for it.
# Claude Code wakes the agent when a background command exits, so this script
# waits, costing no tokens, and exits as soon as something is for this agent:
#   - an unread mention or DM, or @channel / @all / @here, or a post for all
#     agents (props.a2a.to = "all", what agents write instead, RT-6.11),
#   - a reply in a thread the agent posted in,
#   - any new post, while the agent holds the 👑 (the coordinator sees all,
#     and routes a human's plain post to the agent it means, RT-6.6),
#     for both not an agent's fyi post (props.a2a.intent, RT-6.14: ACK, RESULT,
#     DONE, IDLE…) unless the agent started the thread: that waits for the digest,
#   - a reaction to one of its open asks (asks-<uid>.env, RT-6.11) that the
#     agent has not been handed yet and the server did not gate (RT-6.16).
# A post the MCP server gated (GATED in gate-<uid>.env: by someone the agent
# does not trust, already answered, RT-6.16) is not for it; the server also
# marked its channel read, so it raises no counter here.
# Other posts wake it only for a digest, once the oldest of them is
# MM_LISTEN_DIGEST_SEC (default 1800 s) old: every wake-up is a full model turn,
# and a busy channel would otherwise wake every member for every post.
# Its last line tells the woken agent what to do. It only looks; collecting a
# message (and marking it read) stays with the MCP tool wait_for_events.
#
# Window state lives in listen-<uid>.env next to the turn state: UNTIL, SINCE
# (window opened), ASKED_AT (Stop hook asked), STARTED, PID, ENDED (event |
# over). A new prompt from the human kills the listener (turn-status.sh).
#
# Asks the MCP server's hook API (RT-6.17) every MM_LISTEN_POLL_SEC (default
# 15 s), never Mattermost: it holds no token. Fails open: no server, no bus
# session → exits at once; a server that goes away mid-window (or comes back
# with a new port) is asked again on the next round.
set -uo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=mm-agent-lib.sh
source "$BIN/mm-agent-lib.sh"

UNTIL=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --until) UNTIL="${2:-}"; shift 2 ;;
    -h|--help) sed -n '2,28p' "$0"; exit 0 ;;
    *) echo "usage: $0 --until EPOCH" >&2; exit 2 ;;
  esac
done
[[ "$UNTIL" =~ ^[0-9]+$ ]] || { echo "usage: $0 --until EPOCH" >&2; exit 2; }

command -v jq >/dev/null 2>&1 || { echo "jq missing: cannot listen. Finish."; exit 0; }
if ! mm_bus_me; then
  if [[ "$MM_BUS_STATUS" == 502 ]]; then
    echo "Bus unreachable: cannot listen. Finish."
  elif [[ "$MM_BUS_STATUS" == 503 ]]; then
    echo "No bus session (no bot registered): nothing to listen to. Finish."
  else
    mm_bus_explain 2>&1 | sed 's/$/ Finish./'
  fi
  exit 0
fi
ME="$MM_BOT_USER_ID"
LF="$(mm_bot_state_file listen "$ME")" || exit 0

POLL="${MM_LISTEN_POLL_SEC:-15}"
[[ "$POLL" =~ ^[0-9]+$ && "$POLL" -gt 0 ]] || POLL=15
DIGEST="${MM_LISTEN_DIGEST_SEC:-1800}"
[[ "$DIGEST" =~ ^[0-9]+$ ]] || DIGEST=1800

# Who am I to a post: the username (for @mentions in the text), the threads
# this agent posted in and those it started, read once — the agent is idle
# while this runs.
MY_NAME="${MM_BOT_USERNAME:-}"
MY_THREADS="[]"
MY_ROOTS="[]"
if mm_bus GET '/v1/posts?limit=200'; then
  MY_THREADS="$(jq -c --arg me "$ME" '[.posts[] | select(.user_id == $me)
                  | if .root_id == "" then .id else .root_id end] | unique' \
    <<<"$MM_BUS_BODY" 2>/dev/null || echo "[]")"
  MY_ROOTS="$(jq -c --arg me "$ME" '[.posts[] | select(.user_id == $me and .root_id == "") | .id]' \
    <<<"$MM_BUS_BODY" 2>/dev/null || echo "[]")"
fi

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
# writing it again would reopen the window it just closed. Exit 0: being
# stopped is the normal end, and the runtime would list a 143 as "failed".
trap 'ENDED_AS=killed; echo "Your human is back: nothing to do here."; exit 0' TERM INT
trap '[[ "${ENDED_AS:-}" == killed ]] || write 0 "${ENDED_AS:-over}"' EXIT

since_ms=$((STARTED * 1000))
while (( $(date +%s) < UNTIL )); do
  pending=0
  if mm_bus GET /v1/pending; then
    pending="$(jq -r '.count // 0' <<<"$MM_BUS_BODY" 2>/dev/null || echo 0)"
  fi
  [[ "$pending" =~ ^[0-9]+$ ]] || pending=0
  if (( pending > 0 )); then
    ENDED_AS=event
    mm_stat wake reason=dm
    echo "Mattermost: ${pending} unread mention(s) or DM(s) for you. Collect them with wait_for_events(timeout_sec=1), answer in their threads, then finish your turn."
    exit 0
  fi
  # A reaction to an open ask (RT-6.11) raises no unread counter: the server
  # looks at the asks themselves and counts reactions not yet handed over.
  fresh=0
  if mm_bus GET /v1/ask-reactions; then
    fresh="$(jq -r '.fresh // 0' <<<"$MM_BUS_BODY" 2>/dev/null || echo 0)"
  fi
  [[ "$fresh" =~ ^[0-9]+$ ]] || fresh=0
  if (( fresh > 0 )); then
    ENDED_AS=event
    mm_stat wake reason=reaction
    echo "Mattermost: ${fresh} reaction(s) to your ask(s). Collect them with wait_for_events(timeout_sec=1), act on the answer, call resolve_ask once it is settled, then finish your turn."
    exit 0
  fi
  coordinator=false
  if mm_bus_me && (( MM_CROWN_UNTIL > $(date +%s) )); then
    coordinator=true
  fi
  if mm_bus GET "/v1/posts?since=${since_ms}"; then
    # ?since= also returns posts edited since then; look only at new ones.
    # Named: a mention, @channel/@all/@here or to all; seen: the coordinator, or
    # a thread it posted in. An agent's fyi post (the server tells, RT-6.14)
    # wakes only whom it names and the author of its thread's root; a post the
    # server gated (RT-6.16) wakes nobody here.
    read -r addressed others oldest fyi < <(jq -r --arg me "$ME" --arg name "$MY_NAME" \
        --argjson t "$since_ms" --argjson threads "$MY_THREADS" --argjson roots "$MY_ROOTS" \
        --argjson coord "$coordinator" '
      [.posts[] | select(.user_id != $me and .type == "" and .create_at >= $t)]
      | map(.root_id as $r
        | . + {named: ((.message | ascii_downcase | [scan("@[a-z0-9_.-]+")] | map(rtrimstr("."))
                        | any(. == "@" + ($name | ascii_downcase) or . == "@channel" or . == "@all" or . == "@here"))
                       or .to == "all"),
               seen: ($coord or ($r != "" and ($threads | index($r)) != null)),
               mine: ($r != "" and ($roots | index($r)) != null)})
      | map(. + {addressed: ((.gated | not) and (.named or (.seen and (.mine or (.fyi | not)))))})
      | map(select(.addressed)) as $a | map(select(.addressed | not)) as $o
      | "\($a | length) \($o | length) \($o | map(.create_at) | min // 0) \($o | map(select(.fyi)) | length)"' \
      <<<"$MM_BUS_BODY" 2>/dev/null || echo "0 0 0 0")
    if [[ "${addressed:-0}" =~ ^[0-9]+$ ]] && (( addressed > 0 )); then
      ENDED_AS=event
      mm_stat wake reason=addressed
      echo "Mattermost: ${addressed} new post(s) for you in the work channel. Collect them with wait_for_events(timeout_sec=1), answer in their threads, then finish your turn."
      exit 0
    fi
    if [[ "${others:-0}" =~ ^[0-9]+$ ]] && (( others > 0 )) &&
      (( $(date +%s) - ${oldest:-0} / 1000 >= DIGEST )); then
      ENDED_AS=event
      mm_stat wake reason=digest
      note=""
      [[ "${fyi:-0}" =~ ^[0-9]+$ ]] && (( fyi > 0 )) && note=", ${fyi} of them fyi"
      echo "Mattermost digest: ${others} post(s) in the work channel since $(mm_epoch_hhmm $(( oldest / 1000 )))${note}, none addressed to you. Skim them with wait_for_events(timeout_sec=1); act only on what needs you, then finish your turn."
      exit 0
    fi
  fi
  # In the background and waited for, so a TERM is handled at once, not after
  # the sleep.
  sleep "$POLL" &
  wait $! 2>/dev/null || true
done

mm_stat wake reason=over
echo "Mattermost listen window over, nothing new. Finish your turn without another word."

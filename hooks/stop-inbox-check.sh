#!/usr/bin/env bash
# Stop hook: do not let the agent finish a turn with someone waiting on it.
#
# Runs three checks in order, each blocking the stop with an instruction:
#   1. drain   — unread mentions/DMs are waiting: collect them via wait_for_events.
#   2. sign-off — the latest own post in the work channel is not IDLE/BYE: post one.
#   3. listen  — in a project channel, keep answering the bus for MM_LISTEN_SEC
#                (default 900) by blocking in wait_for_events, before going idle.
# Only when all three let go does the status flip to 💤 idle.
#
# Reads Mattermost's unread counters (cheap, non-destructive); delivery stays on
# exactly one path: wait_for_events. The human can end the listen window at any
# time with ESC; the 👂 status expires on its own then.
#
# Fails open in every direction — no bus, no token, no jq, an API hiccup: exit 0.
# A chat integration must never be able to wedge a coding session.
set -uo pipefail

# shellcheck source=../bin/mm-agent-lib.sh
source "$(cd "$(dirname "$0")/../bin" && pwd)/mm-agent-lib.sh"
mm_load_hook_session || true

BIN="$(cd "$(dirname "$0")/../bin" && pwd)"

# Drain rounds per turn before a counter that wait_for_events cannot clear is
# treated as stale. Bounds the hook against looping forever.
MAX_DRAIN=3
LISTEN_SEC="${MM_LISTEN_SEC:-900}"
[[ "$LISTEN_SEC" =~ ^[0-9]+$ ]] || LISTEN_SEC=900

input="$(cat 2>/dev/null || true)"

command -v jq >/dev/null 2>&1 || exit 0
[[ -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]] || exit 0

if [[ -z "${MM_BOT_USER_ID:-}" ]]; then
  mm_api GET "/api/v4/users/me" && [[ "$MM_API_STATUS" == "200" ]] &&
    MM_BOT_USER_ID="$(jq -r '.id' <<<"$MM_API_BODY")"
fi

# Turn state: which checks already ran this turn, and the listen window.
# Without a state file (bot id unknown) fall back to Claude Code's
# stop_hook_active: one extra round per turn, no listening.
DRAINS=0 SIGNOFF_ASKED=0 LISTEN_SINCE=0 LISTEN_UNTIL=0
STATE="$(mm_turn_state_file 2>/dev/null)" || STATE=""
if [[ -n "$STATE" && -f "$STATE" ]]; then
  while IFS='=' read -r k v; do
    [[ "$v" =~ ^[0-9]+$ ]] || continue
    case "$k" in
      DRAINS) DRAINS="$v" ;; SIGNOFF_ASKED) SIGNOFF_ASKED="$v" ;;
      LISTEN_SINCE) LISTEN_SINCE="$v" ;; LISTEN_UNTIL) LISTEN_UNTIL="$v" ;;
    esac
  done <"$STATE"
elif [[ -z "$STATE" && "$(jq -r '.stop_hook_active // false' <<<"$input" 2>/dev/null)" == "true" ]]; then
  "$BIN/mm-agent-status.sh" idle >/dev/null 2>&1 || true
  exit 0
fi

save_state() {
  [[ -n "$STATE" ]] || return 0
  mkdir -p "$(dirname "$STATE")" 2>/dev/null || return 0
  printf 'DRAINS=%s\nSIGNOFF_ASKED=%s\nLISTEN_SINCE=%s\nLISTEN_UNTIL=%s\n' \
    "$DRAINS" "$SIGNOFF_ASKED" "$LISTEN_SINCE" "$LISTEN_UNTIL" >"$STATE" 2>/dev/null || true
}

block() {
  save_state
  jq -nc --arg r "$1" '{decision:"block", reason:$r}' >&2
  exit 2
}

# The turn really is ending: flip the status back to idle and forget the turn.
go_idle() {
  "$BIN/mm-agent-status.sh" idle >/dev/null 2>&1 || true
  [[ -n "$STATE" ]] && rm -f "$STATE"
  exit 0
}

# Latest own post in the work channel: sets LAST_MSG and LAST_AT (epoch ms).
LAST_MSG="" LAST_AT=0
my_last_post() {
  local cid
  [[ -n "${MM_BOT_USER_ID:-}" ]] || return 1
  cid="$(mm_work_channel_id 2>/dev/null)" || return 1
  mm_api GET "/api/v4/channels/${cid}/posts?per_page=100" &&
    [[ "$MM_API_STATUS" == "200" ]] || return 1
  read -r LAST_AT LAST_MSG < <(jq -r --arg me "$MM_BOT_USER_ID" '
    first(.order[] as $i | .posts[$i] | select(.user_id == $me)
          | "\(.create_at) \(.message | gsub("\n"; " "))") // "0 "' \
    <<<"$MM_API_BODY" 2>/dev/null) || return 1
  [[ "$LAST_AT" =~ ^[0-9]+$ ]] || LAST_AT=0
}

# --- 1. drain ---------------------------------------------------------------
pending="$("$BIN/mm-agent-unread.sh" count 2>/dev/null || echo 0)"
[[ "$pending" =~ ^[0-9]+$ ]] || pending=0
if [[ "$pending" -gt 0 && "$DRAINS" -lt "$MAX_DRAIN" ]]; then
  DRAINS=$((DRAINS + 1))
  detail="$("$BIN/mm-agent-unread.sh" detail 2>/dev/null | head -5)"
  reason="You have ${pending} unread message(s) on the Mattermost agent bus."
  reason+=" Call wait_for_events (timeout_sec 1-3) to collect them, handle or"
  reason+=" acknowledge each one, then finish."
  if [[ -n "$detail" ]]; then
    reason+=$'\n\nPending per channel:\n'"$detail"
  fi
  reason+=$'\n\nIf wait_for_events returns nothing, the counter is stale — say so and finish.'
  block "$reason"
fi

have_post=0
my_last_post && have_post=1

# --- 2. sign-off ------------------------------------------------------------
# Once the turn ends, nobody on the bus can reach this agent until its human
# resumes it. Other agents should learn that from the channel, so when the
# agent's latest post in the work channel says it is busy (anything but an
# IDLE or BYE sign-off), ask for a sign-off — once per turn. An agent that has
# not posted in the work channel at all is not part of the coordination and is
# left alone. Nor is one answering from inside its listen window: it signed off
# before the window opened, and a second IDLE after each answer is just noise.
if [[ "$SIGNOFF_ASKED" -eq 0 && "$LISTEN_UNTIL" -eq 0 && "$have_post" -eq 1 && -n "$LAST_MSG" ]] &&
  ! [[ "$LAST_MSG" =~ ^(IDLE|BYE)([^A-Za-z]|$) ]]; then
  SIGNOFF_ASKED=1
  reason="Before this turn ends: once you are idle, other agents on the bus cannot reach you until your human resumes you."
  reason+=" Reply in your HELLO thread with one line: IDLE (waiting for my human) or BYE (task done),"
  reason+=" plus what is done, what is open, and the branch/PR. If you coordinate and will be gone for long, hand over first."
  reason+=" If nothing changed since your last post, say so in one line and finish; this check will not block you again this turn."
  block "$reason"
fi

# --- 3. listen --------------------------------------------------------------
# A request or a reply to the agent's own post typically arrives within a turn
# or two of the others, so in a project channel stay reachable for a while
# before going fully idle. Needs the turn state (to know when the window ends),
# a project channel and a reachable bus — listening on a dead one would hold the
# agent for nothing; MM_LISTEN_SEC=0 turns it off. A BYE ends the session's part
# in the coordination, so it skips the window.
if [[ -n "$STATE" && "$LISTEN_SEC" -gt 0 && "$have_post" -eq 1 ]] &&
  [[ -n "${MM_PROJECT_CHANNEL_ID:-}${MM_PROJECT_CHANNEL:-}" ]] &&
  ! [[ "$LAST_MSG" =~ ^BYE([^A-Za-z]|$) ]]; then
  now="$(date +%s)"
  # A new window when none is open yet, or when the agent answered on the bus
  # since the window opened: that answer may well get a reply of its own.
  if [[ "$LISTEN_UNTIL" -eq 0 ]] || (( LAST_AT / 1000 > LISTEN_SINCE )); then
    LISTEN_SINCE="$now"
    LISTEN_UNTIL=$((now + LISTEN_SEC))
  fi
  remaining=$((LISTEN_UNTIL - now))
  if [[ "$remaining" -gt 5 ]]; then
    "$BIN/mm-agent-status.sh" listening "$LISTEN_UNTIL" >/dev/null 2>&1 || true
    reason="Stay reachable on the bus before going idle: call wait_for_events with timeout_sec=${remaining}."
    reason+=" If it returns [], finish your turn without another word. If it returns events, handle them like any"
    reason+=" request (reply in its thread; ignore posts that need nothing from you), then finish — this check"
    reason+=" resumes listening for the rest of the window. Your human can end the window at any time with ESC."
    block "$reason"
  fi
fi

go_idle

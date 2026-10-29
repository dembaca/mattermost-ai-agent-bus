#!/usr/bin/env bash
# Stop hook: do not let the agent finish a turn with someone waiting on it.
#
# Runs three checks in order, each blocking the stop with an instruction for the
# agent and a one-line banner for the human:
#   1. drain   — unread mentions/DMs are waiting: collect them via wait_for_events.
#   2. sign-off — the latest own post in the work channel is not IDLE/BYE: post one.
#   3. listen  — in a project channel, stay reachable for MM_LISTEN_SEC
#                (default 5400; the 👑 coordinator MM_LISTEN_COORD_SEC, default
#                7200): the agent starts bin/mm-agent-listen.sh in the
#                background and ends its turn; the runtime wakes it when a
#                message arrives. Waiting costs no model calls.
# Only when all three let go does the status flip to 💤 idle.
# Spec: RT-6.2, RT-6.3, RT-6.4 in docs/arc42/06-runtime-view.md.
#
# Asks the MCP server (RT-6.17), which counts what came after its read
# watermark (cheap, non-destructive); delivery stays on exactly one path:
# wait_for_events. The human ends the listen window with their next prompt,
# which stops the listener; the 👂 status expires on its own.
#
# Fails open in every direction — no MCP server, no bus session, no jq, an API
# hiccup: exit 0. A chat integration must never be able to wedge a coding session.
set -uo pipefail

# shellcheck source=../bin/mm-agent-lib.sh
source "$(cd "$(dirname "$0")/../bin" && pwd)/mm-agent-lib.sh"
mm_read_hook_input

BIN="$(cd "$(dirname "$0")/../bin" && pwd)"

# Whatever way the hook ends, a stop it did not block ends the turn: note it, so
# the summary can say how long turns run (RT-6.9). A blocked stop is a forced
# round inside the same turn.
TURN_BLOCKED=0
turn_end() { [[ "$TURN_BLOCKED" -eq 1 ]] || mm_stat turn_end; }
trap turn_end EXIT

# Drain rounds per turn before a count that wait_for_events cannot clear is
# treated as stale. Bounds the hook against looping forever.
MAX_DRAIN=3
# Waiting costs no model calls (bin/mm-agent-listen.sh), so the window is long.
# Claude Code stops a background command after 2 h at most, which caps it.
MAX_BG_SEC=7200
LISTEN_SEC="${MM_LISTEN_SEC:-5400}"
[[ "$LISTEN_SEC" =~ ^[0-9]+$ ]] || LISTEN_SEC=5400
LISTEN_COORD_SEC="${MM_LISTEN_COORD_SEC:-7200}"
[[ "$LISTEN_COORD_SEC" =~ ^[0-9]+$ ]] || LISTEN_COORD_SEC=7200
(( LISTEN_SEC > MAX_BG_SEC )) && LISTEN_SEC=$MAX_BG_SEC
(( LISTEN_COORD_SEC > MAX_BG_SEC )) && LISTEN_COORD_SEC=$MAX_BG_SEC

# The hook has 10 s (hooks.json): all its calls to the server (me, pending,
# last post, presence) share 8 of them, each at most MM_BUS_MAX_TIME. A call
# past the deadline fails like an unreachable server: the check counts as
# nothing to do (RT-6.17).
MM_BUS_DEADLINE=$(( $(date +%s) + 8 ))
export MM_BUS_DEADLINE

# Who the bot is, its project channel and crown: from the MCP server. Without
# it there is no bus session to answer for.
mm_bus_me || exit 0

# The status for this turn, set by the MCP server (lease.py, RT-6.6).
presence() {  # presence working|idle | presence listening UNTIL
  local body
  if [[ "$1" == listening ]]; then
    body="$(jq -nc --argjson u "$2" '{state: "listening", until: $u}')"
  else
    body="$(jq -nc --arg s "$1" --arg l "$(basename "${CORRAL_WORKDIR:-$PWD}")" '{state: $s, label: $l}')"
  fi
  mm_bus POST /v1/presence "$body" || true
}

# Turn state: which checks already ran this turn, keyed on the bot.
DRAINS=0 SIGNOFF_ASKED=0 BY_BUS=0
STATE="$(mm_turn_state_file 2>/dev/null)" || STATE=""
if [[ -n "$STATE" && -f "$STATE" ]]; then
  while IFS='=' read -r k v; do
    [[ "$v" =~ ^[0-9]+$ ]] || continue
    case "$k" in
      DRAINS) DRAINS="$v" ;; SIGNOFF_ASKED) SIGNOFF_ASKED="$v" ;; BY_BUS) BY_BUS="$v" ;;
    esac
  done <"$STATE"
fi

# Listen window (bin/mm-agent-listen.sh): kept outside the turn state, because a
# turn the runtime starts to deliver a message clears the turn state like any
# new prompt, and the window has to survive that.
LISTEN_SINCE=0 LISTEN_UNTIL=0 LISTEN_ASKED=0 LISTEN_STARTED=0 LISTEN_PID=0
LF="$(mm_bot_state_file listen 2>/dev/null)" || LF=""
if [[ -n "$LF" && -f "$LF" ]]; then
  while IFS='=' read -r k v; do
    [[ "$v" =~ ^[0-9]+$ ]] || continue
    case "$k" in
      SINCE) LISTEN_SINCE="$v" ;; UNTIL) LISTEN_UNTIL="$v" ;; ASKED_AT) LISTEN_ASKED="$v" ;;
      STARTED) LISTEN_STARTED="$v" ;; PID) LISTEN_PID="$v" ;;
    esac
  done <"$LF"
fi

save_state() {
  [[ -n "$STATE" ]] || return 0
  mkdir -p "$(dirname "$STATE")" 2>/dev/null || return 0
  printf 'DRAINS=%s\nSIGNOFF_ASKED=%s\nBY_BUS=%s\n' "$DRAINS" "$SIGNOFF_ASKED" "$BY_BUS" >"$STATE" 2>/dev/null || true
}

save_listen() {
  [[ -n "$LF" ]] || return 0
  mkdir -p "$(dirname "$LF")" 2>/dev/null || return 0
  printf 'UNTIL=%s\nSINCE=%s\nASKED_AT=%s\nSTARTED=%s\nPID=0\nENDED=\n' \
    "$LISTEN_UNTIL" "$LISTEN_SINCE" "$LISTEN_ASKED" "$LISTEN_STARTED" >"$LF" 2>/dev/null || true
}

# block <reason for the agent> [banner for the human] [stat kind]. The reason goes to the
# model; the banner (systemMessage) is shown to the human in the terminal, so
# they can tell at a glance why the turn has not ended yet.
block() {
  mm_stat block kind="${3:-other}"
  TURN_BLOCKED=1
  save_state
  jq -nc --arg r "$1" --arg m "${2:-}" \
    '{decision:"block", reason:$r} + (if $m == "" then {} else {systemMessage:$m} end)'
  exit 0
}

# A newer plugin is installed than this session runs (RT-6.2): say so once per
# version, in the banner the human sees when the turn ends. Prints the note.
update_note() {
  local newer uf
  newer="$(mm_newer_plugin 2>/dev/null)" || return 1
  uf="$(mm_bot_state_file update 2>/dev/null)" || return 1
  [[ "$(sed -n 's/^NOTIFIED=//p' "$uf" 2>/dev/null)" != "$newer" ]] || return 1
  printf 'NOTIFIED=%s\n' "$newer" >"$uf" 2>/dev/null || return 1
  printf '⬆ plugin %s is installed, this session runs %s — to switch: quit, then claude --continue' \
    "$newer" "$(mm_tree_version "$BIN/..")"
}

# The turn really is ending: flip the status back to idle and forget the turn.
go_idle() {
  presence idle
  [[ -n "$STATE" ]] && rm -f "$STATE"
  [[ -n "$LF" ]] && rm -f "$LF"
  local note
  if note="$(update_note)"; then
    jq -nc --arg m "$note" '{systemMessage:$m}'
  fi
  exit 0
}

# Latest own post in the work channel: sets LAST_MSG and LAST_AT (epoch ms).
# Without the 👑 the MCP server puts before a coordinator's posts (RT-6.6).
# The server keeps it in last-<uid>.env (RT-6.2, ownpost.py, mm-agent-post.sh)
# and reads the channel's last 100 posts only without that file. Fails when the
# server cannot tell; LAST_AT 0 when the agent never posted there.
LAST_MSG="" LAST_AT=0
my_last_post() {
  mm_bus GET /v1/last-post || return 1
  LAST_AT="$(jq -r '.at // 0' <<<"$MM_BUS_BODY" 2>/dev/null)" || return 1
  LAST_MSG="$(jq -r '.message // ""' <<<"$MM_BUS_BODY" 2>/dev/null)" || return 1
  [[ "$LAST_AT" =~ ^[0-9]+$ ]] || LAST_AT=0
}

# --- 1. drain ---------------------------------------------------------------
# Unread mentions, DMs and posts for all agents, counted by the MCP server
# (unread.py there); per channel, with the fyi posts, for the reason.
pending=0
if [[ "$DRAINS" -lt "$MAX_DRAIN" ]] && mm_bus GET '/v1/pending?detail=1'; then
  pending="$(jq -r '.count // 0' <<<"$MM_BUS_BODY" 2>/dev/null || echo 0)"
fi
[[ "$pending" =~ ^[0-9]+$ ]] || pending=0
if [[ "$pending" -gt 0 && "$DRAINS" -lt "$MAX_DRAIN" ]]; then
  DRAINS=$((DRAINS + 1))
  detail="$(jq -r '(.channels // [])[:5][] | "\(.id)\t\(.type)\tpending=\(.pending)"
                   + (if (.fyi // 0) > 0 then "\tfyi=\(.fyi)" else "" end)' <<<"$MM_BUS_BODY" 2>/dev/null)"
  # Ranked by intent (RT-6.14): an agent's fyi post needs no answer.
  fyi="$(awk -F'fyi=' 'NF > 1 { n += $2 } END { print n + 0 }' <<<"$detail")"
  reason="You have ${pending} unread message(s) on the Mattermost agent bus"
  if (( fyi > 0 )); then
    reason+=", $(( pending > fyi ? pending - fyi : 0 )) needing action, the rest fyi"
  fi
  reason+="."
  reason+=" Call wait_for_events (timeout_sec 1-3) to collect them, handle or"
  reason+=" acknowledge each one, then finish."
  if [[ -n "$detail" ]]; then
    reason+=$'\n\nPending per channel:\n'"$detail"
  fi
  reason+=$'\n\nIf wait_for_events returns nothing, the count is stale — say so and finish.'
  block "$reason" "📨 Collecting ${pending} unread Mattermost message(s)" drain
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
# The same holds for a turn a channel push started (RT-6.12): it answers the
# bus, and the agent stays reachable through the channel anyway.
CHANNEL=0; mm_channel_push && CHANNEL=1
if [[ "$SIGNOFF_ASKED" -eq 0 && "$LISTEN_UNTIL" -eq 0 && "$have_post" -eq 1 && -n "$LAST_MSG" ]] &&
  ! [[ "$BY_BUS" -eq 1 && "$CHANNEL" -eq 1 ]] &&
  ! [[ "$LAST_MSG" =~ ^(IDLE|BYE)([^A-Za-z]|$) ]]; then
  SIGNOFF_ASKED=1
  if [[ "$CHANNEL" -eq 1 ]]; then
    reason="Before this turn ends: messages still reach you through the channel, but the others need to know what you finished and what is next."
  else
    reason="Before this turn ends: once you are idle, other agents on the bus cannot reach you until your human resumes you."
  fi
  reason+=" Reply in your HELLO thread, no prose: a headline \`IDLE · <gist>\`, then a line each for \`**done:**\`, \`**open:**\`, \`**next:**\` (a \`- \` list for several), PRs, issues, tickets and commits as links, post ids in full"
  reason+=" (BYE when the task is done). Give the next step so that you could pick up from it alone: your"
  reason+=" conversation may be compacted while you are idle, and this post is handed back to you then."
  reason+=" If you coordinate and will be gone for long, hand over first."
  reason+=" If nothing changed since your last post, say so in one line and finish; this check will not block you again this turn."
  block "$reason" "✋ Signing off on Mattermost before going idle" signoff
fi

# --- 3. listen --------------------------------------------------------------
# The coordinator listens longer (RT-6.6): while it listens it stays reachable
# and keeps the crown, so the role does not move the moment its human steps
# away.
if (( MM_CROWN_UNTIL > $(date +%s) )); then
  LISTEN_SEC="$LISTEN_COORD_SEC"
fi
# A request or a reply to the agent's own post typically arrives within a turn
# or two of the others, so in a project channel stay reachable for a while
# before going fully idle. Needs a project channel and a reachable bus —
# listening on a dead one would hold nothing; MM_LISTEN_SEC=0 turns it off. A
# BYE ends the session's part in the coordination, so it skips the window.
# Started as a channel (RT-6.12) and confirmed by the agent,
# the MCP server pushes messages into the session itself: nothing to listen for.
if [[ -n "$LF" && "$LISTEN_SEC" -gt 0 && "$have_post" -eq 1 ]] && ! mm_channel_push &&
  [[ "$MM_BUS_PROJECT" == 1 ]] &&
  ! [[ "$LAST_MSG" =~ ^BYE([^A-Za-z]|$) ]]; then
  now="$(date +%s)"
  # A listener is already waiting in the background: let the turn end and keep
  # the 👂 status. Without this the hook would ask again on every stop.
  if [[ "$LISTEN_PID" -gt 0 && "$LISTEN_UNTIL" -gt "$now" ]] && kill -0 "$LISTEN_PID" 2>/dev/null; then
    exit 0
  fi
  # A new window when none is open yet, or when the agent answered on the bus
  # since the window opened: that answer may well get a reply of its own.
  if [[ "$LISTEN_UNTIL" -eq 0 ]] || (( LAST_AT / 1000 > LISTEN_SINCE )); then
    LISTEN_SINCE="$now"
    LISTEN_UNTIL=$((now + LISTEN_SEC))
  fi
  remaining=$((LISTEN_UNTIL - now))
  # Asked before, and no listener started since: the agent cannot run one
  # (an older runtime, a denied command). Go idle rather than ask forever.
  if [[ "$remaining" -gt 5 ]] && ! [[ "$LISTEN_ASKED" -gt 0 && "$LISTEN_STARTED" -lt "$LISTEN_ASKED" ]]; then
    LISTEN_ASKED="$now"
    save_listen
    presence listening "$LISTEN_UNTIL"
    # The runtime's default limit for a background command (30 min) would
    # cut the window short, so the hook names the timeout to pass.
    bg_ms=$(( (remaining + 60 < MAX_BG_SEC ? remaining + 60 : MAX_BG_SEC) * 1000 ))
    reason="Stay reachable on the bus before going idle: run \`\"$BIN/mm-agent-listen.sh\" --until ${LISTEN_UNTIL}\`"
    reason+=" as a background command (Bash with run_in_background and timeout ${bg_ms}), then finish your turn without another word."
    reason+=" It waits without using the model and wakes you when a message arrives; its output says what to do."
    reason+=" If you cannot run background commands, just finish; this check will not ask again."
    banner="👂 Listening on Mattermost until $(mm_epoch_hhmm "$LISTEN_UNTIL") in the background ($(( (remaining + 59) / 60 )) min) · your next prompt ends it"
    if note="$(update_note)"; then banner+=$'\n'"$note"; fi
    block "$reason" "$banner" listen
  fi
fi

go_idle

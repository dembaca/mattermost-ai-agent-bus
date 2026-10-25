#!/usr/bin/env bash
# UserPromptSubmit hook: this agent just started working on something.
#
# Flips the Mattermost status to 🛠 working. The matching flip back to 💤 idle
# happens in the Stop hook, but only when the turn is actually allowed to end —
# a blocked stop means we are still busy.
#
# Carries no content: presence and a project label, never the prompt.
# Asks the MCP server (RT-6.17), never Mattermost: no server, no bus session —
# exit 0 without a word.
# Spec: RT-6.2 (step 6), RT-6.3 in docs/arc42/06-runtime-view.md.
set -uo pipefail

# shellcheck source=../bin/mm-agent-lib.sh
source "$(cd "$(dirname "$0")/../bin" && pwd)/mm-agent-lib.sh"
mm_read_hook_input            # drains the payload: its session id, and whether the bus woke the turn
mm_bus_me || exit 0           # the bot's id keys the turn and listen state

# Who started this turn: the bus (a channel push, or the listener that ended on
# a message) or the human. The Stop hook that ends the turn notes the end, so
# the summary can say how long it ran (RT-6.9).
by=human

# A new turn: forget what the Stop hook checked in the last one.
state="$(mm_turn_state_file 2>/dev/null)" && rm -f "$state"
# A turn the channel push started (RT-6.12) answers the bus; like an answer
# from inside a listen window it needs no sign-off of its own.
if [[ -n "${state:-}" ]] && mm_channel_push &&
  [[ "$(jq -r '.prompt // ""' <<<"$MM_HOOK_INPUT" 2>/dev/null)" == '<channel source="plugin:mattermost-ai-agent-bus:'* ]]; then
  mkdir -p "$(dirname "$state")" 2>/dev/null && printf 'BY_BUS=1\n' >"$state" 2>/dev/null
  by=bus
fi

# Listen window (RT-6.2, RT-6.3). A listener still running means the human is
# back before anything arrived: stop it and close the window. A listener that
# ended on a message is what started this turn: keep the window, so the Stop
# hook resumes it afterwards instead of asking for a second sign-off. So does
# a window that just ran out: its end started this turn, not the human, and
# removing it would make the Stop hook open the next one — a turn every
# window, for good.
if lf="$(mm_bot_state_file listen 2>/dev/null)" && [[ -f "$lf" ]]; then
  pid="$(sed -n 's/^PID=//p' "$lf" | head -1)"
  ended="$(sed -n 's/^ENDED=//p' "$lf" | head -1)"
  if [[ "$pid" =~ ^[0-9]+$ && "$pid" -gt 0 ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    rm -f "$lf"
  elif [[ "$ended" == over && -n "$(find "$lf" -mmin -2 2>/dev/null)" ]]; then
    by=bus
  elif [[ "$ended" != event ]]; then
    rm -f "$lf"
  else
    by=bus
  fi
fi

mm_stat turn by="$by"
# Where Claude Code books each request's tokens: the MCP server measures the
# bus's share from it at session end (RT-6.9).
transcript="$(jq -r '.transcript_path // ""' <<<"${MM_HOOK_INPUT:-}" 2>/dev/null)" || transcript=""
[[ -z "$transcript" ]] || mm_stat transcript "path=$transcript"

mm_bus POST /v1/presence "$(jq -nc --arg l "$(basename "${CORRAL_WORKDIR:-$PWD}")" \
  '{state: "working", label: $l}')" || true
exit 0

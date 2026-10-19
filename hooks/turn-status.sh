#!/usr/bin/env bash
# UserPromptSubmit hook: this agent just started working on something.
#
# Flips the Mattermost status to 🛠 working. The matching flip back to 💤 idle
# happens in the Stop hook, but only when the turn is actually allowed to end —
# a blocked stop means we are still busy.
#
# Carries no content: presence and a project label, never the prompt.
# Spec: RT-6.2 (step 6), RT-6.3 in docs/arc42/06-runtime-view.md.
set -uo pipefail

# shellcheck source=../bin/mm-agent-lib.sh
source "$(cd "$(dirname "$0")/../bin" && pwd)/mm-agent-lib.sh"
mm_read_hook_input            # drains the payload; only its session id is used
mm_load_hook_session || true

BIN="$(cd "$(dirname "$0")/../bin" && pwd)"

mm_stat turn

# A new turn: forget what the Stop hook checked in the last one.
state="$(mm_turn_state_file 2>/dev/null)" && rm -f "$state"

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
    :
  elif [[ "$ended" != event ]]; then
    rm -f "$lf"
  fi
fi

exec "$BIN/mm-agent-status.sh" working >/dev/null 2>&1

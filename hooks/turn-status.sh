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

cat >/dev/null 2>&1 || true   # drain the hook payload; we do not read it

# shellcheck source=../bin/mm-agent-lib.sh
source "$(cd "$(dirname "$0")/../bin" && pwd)/mm-agent-lib.sh"
mm_load_hook_session || true

BIN="$(cd "$(dirname "$0")/../bin" && pwd)"

# A new turn: forget what the Stop hook checked or listened for in the last one.
state="$(mm_turn_state_file 2>/dev/null)" && rm -f "$state"

exec "$BIN/mm-agent-status.sh" working >/dev/null 2>&1

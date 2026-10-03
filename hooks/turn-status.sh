#!/usr/bin/env bash
# UserPromptSubmit hook: this agent just started working on something.
#
# Flips the Mattermost status to 🛠 working. The matching flip back to 💤 idle
# happens in the Stop hook, but only when the turn is actually allowed to end —
# a blocked stop means we are still busy.
#
# Carries no content: presence and a project label, never the prompt.
set -uo pipefail

cat >/dev/null 2>&1 || true   # drain the hook payload; we do not read it

BIN="$(cd "$(dirname "$0")/../bin" && pwd)"
exec "$BIN/mm-agent-status.sh" working >/dev/null 2>&1

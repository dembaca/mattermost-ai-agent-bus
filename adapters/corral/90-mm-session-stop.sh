#!/usr/bin/env bash
# corral postEnd hook: unregister the ephemeral bot this session used.
#
# Runs on the HOST after the agent exits — including when the launch was aborted
# (CORRAL_AGENT_EXIT=aborted). Idempotent and never fatal: a postEnd failure only
# warns, and the agent's own exit code always wins.
#
# stdout/stderr are passthrough for postEnd; nothing here is parsed.
set -uo pipefail

BIN="$(cd "$(dirname "$0")/../../bin" && pwd)"

log() { echo "mm-bus: $*" >&2; }

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
SESSION_FILE="${STATE_DIR}/${CORRAL_SESSION_ID:-$$}.env"

if [[ ! -f "$SESSION_FILE" ]]; then
  # No bot was registered for this session (no secret, no bus, or preStart bailed).
  exit 0
fi

# shellcheck source=/dev/null
MM_BOT_NAME="$(sed -n 's/^MM_BOT_NAME=//p' "$SESSION_FILE" | head -n1)"

if [[ -z "$MM_BOT_NAME" ]]; then
  log "session file has no MM_BOT_NAME; removing it"
  rm -f "$SESSION_FILE"
  exit 0
fi

export MM_AGENT_SESSION_FILE="$SESSION_FILE"

if "$BIN/mm-agent-unregister.sh" "$MM_BOT_NAME" >/dev/null 2>&1; then
  log "unregistered ${MM_BOT_NAME}"
else
  # Worth saying out loud: a leftover bot account needs manual cleanup.
  log "WARNING: could not unregister ${MM_BOT_NAME} — remove it manually"
fi

rm -f "$SESSION_FILE"

#!/usr/bin/env bash
# corral postEnd hook: unregister the ephemeral bot this session used.
#
# Runs on the HOST after the agent exits — including when the launch was aborted
# (CORRAL_AGENT_EXIT=aborted). Idempotent and never fatal: a postEnd failure only
# warns, and the agent's exit code always wins.
#
# stdout/stderr are passthrough for postEnd; nothing here is parsed.
set -uo pipefail

BIN="$(cd "$(dirname "$0")/../../bin" && pwd)"
# shellcheck source=../../bin/mm-agent-lib.sh
source "$BIN/mm-agent-lib.sh"

log() { echo "mm-bus: $*" >&2; }

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
SESSION_FILE="${STATE_DIR}/${CORRAL_SESSION_ID:-$$}.env"

if [[ ! -f "$SESSION_FILE" ]]; then
  # No bot was registered for this session (no secret, no bus, or preStart bailed).
  exit 0
fi

# Load the session into the environment. Setting MM_AGENT_SESSION_FILE alone is
# not enough: mm-agent-unregister.sh authenticates from MM_REG_SECRET or
# MM_BOT_TOKEN in the environment and never reads the file itself. Without this
# the teardown exits "token required" and every bot leaks.
export MM_AGENT_SESSION_FILE="$SESSION_FILE"
mm_load_session_file || true

if [[ -z "${MM_BOT_NAME:-}" ]]; then
  log "session file has no MM_BOT_NAME; removing it"
  rm -f "$SESSION_FILE"
  exit 0
fi

if [[ -z "${MM_REG_SECRET:-${MM_BOT_TOKEN:-}}" ]]; then
  log "WARNING: no credential for ${MM_BOT_NAME}; keeping ${SESSION_FILE} so it can be retried"
  exit 0
fi

# Mark it gone before unregistering, so a bot that survives a failed teardown
# does not sit there looking available.
"$BIN/mm-agent-status.sh" offline >/dev/null 2>&1 || true

if "$BIN/mm-agent-unregister.sh" "$MM_BOT_NAME" >/dev/null 2>&1; then
  log "unregistered ${MM_BOT_NAME}"
  rm -f "$SESSION_FILE"
else
  # Keep the session file: it holds the only credential that can still remove
  # this bot. Deleting it here is what turns a failed teardown into a permanent
  # orphan.
  log "WARNING: could not unregister ${MM_BOT_NAME}"
  log "         retry: $BIN/mm-agent-sweep.sh"
fi

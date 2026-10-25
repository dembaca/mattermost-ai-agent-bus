#!/usr/bin/env bash
# corral postEnd hook: stop the session's MCP server on the host, then
# unregister the ephemeral bot this session used.
#
# Runs on the HOST after the agent exits — including when the launch was aborted
# (CORRAL_AGENT_EXIT=aborted). Idempotent and never fatal: a postEnd failure only
# warns, and the agent's exit code always wins.
#
# stdout/stderr are passthrough for postEnd; nothing here is parsed.
#
# Spec: RT-6.1 and RT-6.18 in docs/arc42/06-runtime-view.md.
set -uo pipefail

# No credential in any process environment (arc42 §7.3): one that arrived in
# ours stays visible in this process, but no child gets it.
unset MM_BOT_TOKEN MATTERMOST_TOKEN CDPATH
export -n MM_REG_SECRET 2>/dev/null || true

log() { echo "mm-bus: $*" >&2; }

# --- where this hook runs from (arc42 §7.1) ------------------------------------
# Same check as preStart's, inline, before anything of the tree is sourced or
# run: a tree a sandbox can change runs none of its code here. preStart refused
# to register from such a tree, so there is no bot of it to remove; a session
# file left by an earlier install the sweep handles.
seen=()
self="$0"
while :; do
  seen+=("$(cd "$(dirname "$self")" 2>/dev/null && pwd)/")
  [[ -L "$self" && "${#seen[@]}" -lt 40 ]] || break
  link="$(readlink "$self")"
  [[ "$link" == /* ]] || link="$(dirname "$self")/$link"
  self="$link"
done
ROOT="$(cd -P "$(dirname "$self")/../.." && pwd -P)"
seen+=("$ROOT/")
for r in "$HOME/.cache/corral" "$HOME/.claude" "${CORRAL_WORKDIR:-}"; do
  [[ -n "$r" ]] || continue
  real="$(cd -P "$r" 2>/dev/null && pwd -P)" || real="$r"
  for p in "${seen[@]}"; do
    if [[ "$p" == "$r/"* || "$p" == "$real/"* ]]; then
      log "the host hooks run from ${p%/}, which a sandbox can change — nothing run from it; install them from a reviewed clone with bin/mm-agent-host-install.sh and point corral's exec at ~/.local/share/mm-agent-bus/current/adapters/corral/"
      exit 0
    fi
  done
done

BIN="$ROOT/bin"
# shellcheck source=../../bin/mm-agent-lib.sh
source "$BIN/mm-agent-lib.sh"

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
SESSION_KEY="${CORRAL_SESSION_ID:-$$}"
[[ "$SESSION_KEY" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]] || SESSION_KEY="$$"
SESSION_FILE="${STATE_DIR}/${SESSION_KEY}.env"

if [[ ! -f "$SESSION_FILE" ]]; then
  # No bot was registered for this session (no secret, no bus, or preStart bailed).
  exit 0
fi

# --- the MCP server on the host (RT-6.18) ------------------------------------
# SIGTERM starts its exit work (BYE, stats, offline, unregister); give it a
# bounded while, then SIGKILL. Only the process preStart recorded: same pid and
# the same start time. Without a recorded start time it is not touched: a pid
# alone may name another process by now, and the server's watchdog on corral's
# pid stops it anyway.
proc_start() { ps -o lstart= -p "$1" 2>/dev/null | tr -s ' ' | sed -e 's/^ //' -e 's/ $//'; }
hpid="$(sed -n 's/^MM_BUS_HOST_PID=//p' "$SESSION_FILE" | head -n1)"
hstart="$(sed -n 's/^MM_BUS_HOST_START=//p' "$SESSION_FILE" | head -n1)"
if [[ "$hpid" =~ ^[0-9]+$ && -z "$hstart" ]]; then
  log "no start time recorded for the MCP server (pid ${hpid}); not stopping it here, its watchdog does"
elif [[ "$hpid" =~ ^[0-9]+$ ]] && kill -0 "$hpid" 2>/dev/null &&
  [[ "$(proc_start "$hpid")" == "$hstart" ]]; then
  kill -TERM "$hpid" 2>/dev/null || true
  for _ in $(seq 1 100); do
    kill -0 "$hpid" 2>/dev/null || break
    sleep 0.1
  done
  if kill -0 "$hpid" 2>/dev/null; then
    kill -KILL "$hpid" 2>/dev/null || true
    log "WARNING: the MCP server (pid ${hpid}) did not stop within 10 s; killed"
  fi
fi
rm -f "${STATE_DIR}/${SESSION_KEY}.host.json"
hosted="${STATE_DIR}/hosted/${SESSION_KEY}"
[[ -d "$hosted" && ! -L "$hosted" ]] && rm -rf "$hosted"

# Load the session. The token goes into a variable that is not exported, and
# from there to the helpers on fd 3 only (arc42 §7.3).
export MM_AGENT_SESSION_FILE="$SESSION_FILE"
mm_load_session_file || true
export -n MM_BOT_TOKEN MATTERMOST_TOKEN 2>/dev/null || true

if [[ -z "${MM_REG_BOT_NAME:-}" ]]; then
  log "session file has no MM_REG_BOT_NAME; removing it"
  rm -f "$SESSION_FILE"
  exit 0
fi

if [[ -z "${MM_REG_SECRET:-${MM_BOT_TOKEN:-}}" ]]; then
  log "WARNING: no credential for ${MM_REG_BOT_NAME}; keeping ${SESSION_FILE} so it can be retried"
  exit 0
fi

# Mark it gone before unregistering, so a bot that survives a failed teardown
# does not sit there looking available.
mm_with_token "$BIN/mm-agent-status.sh" offline >/dev/null 2>&1 || true

# With the bot's own token first, which the registrar always accepts for it.
if { [[ -n "${MM_BOT_TOKEN:-}" ]] && mm_with_token "$BIN/mm-agent-unregister.sh" "$MM_REG_BOT_NAME" >/dev/null 2>&1; } ||
  { [[ -n "${MM_REG_SECRET:-}" ]] && mm_with_secret "$BIN/mm-agent-unregister.sh" "$MM_REG_BOT_NAME" >/dev/null 2>&1; }; then
  log "unregistered ${MM_REG_BOT_NAME}"
  rm -f "$SESSION_FILE"
else
  # Keep the session file: it holds the only credential that can still remove
  # this bot. Deleting it here is what turns a failed teardown into a permanent
  # orphan.
  log "WARNING: could not unregister ${MM_REG_BOT_NAME}"
  log "         retry: $BIN/mm-agent-sweep.sh"
fi

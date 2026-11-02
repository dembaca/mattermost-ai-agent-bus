#!/usr/bin/env bash
# corral postEnd hook: stop the session's MCP server on the host, then
# unregister the ephemeral bot this session used — or keep it for
# `claude --continue` when the agent was Claude Code and ended by itself.
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

# --- what a sandbox can write (arc42 §7.1, §7.3) -------------------------------
# Same as preStart's, inline: the corral homes, ~/.claude, the workdir and
# corral's temporary directories, without regard to case; a relative path or
# one with a . or .. component counts as one. PATH keeps the absolute entries
# outside them, and the hook works from /.
shopt -s nocasematch
sandbox_dirs=(/tmp/corral- /private/tmp/corral-)
for r in "$HOME/.cache/corral" "$HOME/.claude" "${CORRAL_WORKDIR:-}" "${TMPDIR:-}"; do
  [[ -n "$r" && ( "$r" != "${TMPDIR:-}" || "$r" == *corral* ) ]] || continue
  sandbox_dirs+=("${r%/}/")
  if real="$(cd -P "$r" 2>/dev/null && pwd -P)"; then sandbox_dirs+=("$real/"); fi
done
sandbox_can_write() {
  local d
  [[ "$1" == /* && "$1/" != */../* && "$1/" != */./* ]] || return 0
  for d in "${sandbox_dirs[@]}"; do [[ "$1/" == "$d"* ]] && return 0; done
  return 1
}
path_rest="${PATH:-}:" safe_path=""
while [[ -n "$path_rest" ]]; do
  e="${path_rest%%:*}" path_rest="${path_rest#*:}"
  sandbox_can_write "${e%/}" || safe_path="${safe_path:+$safe_path:}$e"
done
export PATH="${safe_path:-/usr/bin:/bin:/usr/sbin:/sbin}"
self="$0"
[[ "$self" == /* ]] || self="$PWD/$self"
cd /

log() { echo "mm-bus: $*" >&2; }

# --- where this hook runs from (arc42 §7.1) ------------------------------------
# Same check as preStart's, inline, before anything of the tree is sourced or
# run: a tree a sandbox can change runs none of its code here. preStart refused
# to register from such a tree, so there is no bot of it to remove; a session
# file left by an earlier install the sweep handles.
seen=()
while :; do
  d="$(cd -P "$(dirname "$self")" 2>/dev/null && pwd -P)" || d=""
  seen+=("$self" "$d")
  [[ -L "$self" && "${#seen[@]}" -lt 80 ]] || break
  link="$(readlink "$self")"
  [[ "$link" == /* ]] || link="$(dirname "$self")/$link"
  self="$link"
done
ROOT="$(cd -P "$(dirname "$self")/../.." && pwd -P)"
seen+=("$ROOT")
for p in "${seen[@]}"; do
  if sandbox_can_write "$p"; then
    log "the host hooks run from ${p:-$0}, which a sandbox can change — nothing run from it; install them from a reviewed clone with bin/mm-agent-host-install.sh and point corral's exec at ~/.local/share/mm-agent-bus/current/adapters/corral/"
    exit 0
  fi
done
# The session file holds the bot's token, a kept one for up to a day longer:
# never in a directory a sandbox can change (RT-6.18 step 9).
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
if sandbox_can_write "$STATE_DIR"; then
  log "the state directory ${STATE_DIR} is one a sandbox can change — nothing run from it; set XDG_STATE_HOME to a directory outside the corral homes and workdirs"
  exit 0
fi
shopt -u nocasematch

BIN="$ROOT/bin"
# shellcheck source=../../bin/mm-agent-lib.sh
source "$BIN/mm-agent-lib.sh"

SESSION_KEY="${CORRAL_SESSION_ID:-$$}"
[[ "$SESSION_KEY" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]] || SESSION_KEY="$$"
SESSION_FILE="${STATE_DIR}/${SESSION_KEY}.env"

if [[ ! -f "$SESSION_FILE" ]]; then
  # No bot was registered for this session (no secret, no bus, or preStart bailed).
  exit 0
fi

# Load the session. The token goes into a variable that is not exported, and
# from there to the helpers on fd 3 only (arc42 §7.3).
export MM_AGENT_SESSION_FILE="$SESSION_FILE"
mm_load_session_file || true
export -n MM_BOT_TOKEN MATTERMOST_TOKEN 2>/dev/null || true

# --- keep the bot for `claude --continue`? (RT-6.18 step 9) --------------------
# corral cannot tell postEnd whether the session will be resumed. A Claude Code
# session that ended by itself keeps its bot, offline, for a start with
# --continue or --resume in the same workdir (step 10) or until the grace
# period ends. Only when its host process saw the Claude session: it wrote the
# session's key at the first relay handshake. Never for an aborted launch,
# another agent, or a session file without the bot's own token.
hosted="${STATE_DIR}/hosted/${SESSION_KEY}"
KEEP=0 KEPT_SID="" KEPT_WORKDIR=""
if [[ "${CORRAL_AGENT:-}" == claude && "${CORRAL_AGENT_EXIT:-}" != aborted &&
      -n "${MM_REG_BOT_NAME:-}" && -n "${MM_BOT_TOKEN:-}" &&
      -d "$hosted" && ! -L "$hosted" && ! -L "$hosted/claude-sid.env" ]]; then
  KEPT_SID="$(mm_read_file "$hosted/claude-sid.env" 256 | sed -n 's/^MM_CLAUDE_SID=//p' | head -n1)" || KEPT_SID=""
  KEPT_WORKDIR="$(mm_workdir_key "${CORRAL_WORKDIR:-}")" || KEPT_WORKDIR=""
  if [[ "$KEPT_SID" =~ ^[0-9a-f]{16}$ && "$KEPT_WORKDIR" =~ ^[0-9a-f]{16}$ ]]; then KEEP=1; fi
fi

# --- the MCP server on the host (RT-6.18) ------------------------------------
# SIGTERM starts its exit work (BYE, stats, offline, unregister); SIGUSR1, when
# the bot is kept, only sets it offline and leaves its state directory. Give it
# a bounded while, then SIGKILL. Only the process preStart recorded: same pid
# and the same start time. Without a recorded start time it is not touched: a
# pid alone may name another process by now, and the server's watchdog on
# corral's pid stops it anyway — with its exit work, so then nothing is kept.
proc_start() { ps -o lstart= -p "$1" 2>/dev/null | tr -s ' ' | sed -e 's/^ //' -e 's/ $//'; }
hpid="$(sed -n 's/^MM_BUS_HOST_PID=//p' "$SESSION_FILE" | head -n1)"
hstart="$(sed -n 's/^MM_BUS_HOST_START=//p' "$SESSION_FILE" | head -n1)"
sig=TERM
[[ "$KEEP" -eq 0 ]] || sig=USR1
if [[ "$hpid" =~ ^[0-9]+$ && -z "$hstart" ]]; then
  if kill -0 "$hpid" 2>/dev/null; then KEEP=0; fi
  log "no start time recorded for the MCP server (pid ${hpid}); not stopping it here, its watchdog does"
elif [[ "$hpid" =~ ^[0-9]+$ ]] && kill -0 "$hpid" 2>/dev/null &&
  [[ "$(proc_start "$hpid")" == "$hstart" ]]; then
  kill "-${sig}" "$hpid" 2>/dev/null || true
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

# Kept: the session file stays in the host state dir (0600, the token never
# leaves it), without the host process, marked with the end and the keys of
# the workdir and the Claude session; the state directory stays with it.
if [[ "$KEEP" -eq 1 ]]; then
  tmp=""
  if tmp="$(umask 077 && mktemp "${STATE_DIR}/.${SESSION_KEY}.env.XXXXXX")" &&
     { grep -Ev '^(MM_BUS_HOST_[A-Z_]*|MM_SESSION_PID|MM_SERVER_ENDED|MM_KEPT_[A-Z_]*)=' "$SESSION_FILE"
       printf 'MM_SERVER_ENDED=%s\nMM_KEPT_WORKDIR=%s\nMM_KEPT_CLAUDE_SID=%s\n' \
         "$(date +%s)" "$KEPT_WORKDIR" "$KEPT_SID"; } >"$tmp" &&
     mv -f "$tmp" "$SESSION_FILE"; then
    # Offline as a safety net: the host process did it already.
    mm_with_token "$BIN/mm-agent-status.sh" offline >/dev/null 2>&1 || true
    log "kept ${MM_REG_BOT_NAME} for claude --continue or --resume in this workdir, offline, for $(( $(mm_keep_sec) / 3600 )) h"
    exit 0
  fi
  [[ -z "$tmp" ]] || rm -f "$tmp"
  log "WARNING: could not mark ${MM_REG_BOT_NAME} as kept; unregistering it"
fi
[[ -d "$hosted" && ! -L "$hosted" ]] && rm -rf "$hosted"

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

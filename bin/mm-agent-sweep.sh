#!/usr/bin/env bash
# Clean up agents a failed teardown left behind.
#
#   mm-agent-sweep.sh            # dry run: list what would be removed
#   mm-agent-sweep.sh --apply    # actually unregister
#   mm-agent-sweep.sh --apply --force   # include sessions that still look live
#
# A bot an agent registered itself (mcp-*.env) outlives its process for
# `claude --continue`; it counts as abandoned only once its file is untouched
# for MM_SESSION_KEEP_SEC (default 24 h). --force includes it sooner.
#
# A session file exists for the whole run of a session, so it is NOT evidence
# that a bot is abandoned. Each file records MM_SESSION_PID, the supervising
# corral process; while that process is alive the session is in use and is
# skipped. Without that check a sweep unregisters the bot of the session it is
# running alongside, killing a live agent's credentials mid-task.
#
# Two sources, in this order:
#   1. Session files whose process is gone. Each holds the bot's own token,
#      which the registrar accepts for deleting that bot — so this needs no
#      MM_REG_SECRET, the normal case on a gopass setup.
#   2. With MM_REG_SECRET set, agent-* accounts (the registrar's prefix) with
#      no session file at all. Required rather than convenient: a bot token
#      targeting another agent is answered 401.
#
# Per-bot state files next to them (asks-, coord-, gate-, last-, listen-, presence-, turn-, update-<uid>.env,
# stats-<uid>.jsonl) hold no token and are not sessions. Last, the sweep removes
# those whose bot has no session file left and that were untouched for a day: a
# live session keeps writing them, and one whose token was exported by hand has
# no session file but would lose its crown lease if they went sooner.
#
# A keyless corral session (RT-6.18) has its MCP server running on the host:
# its session file records MM_BUS_HOST_PID, and the process keeps its files in
# <sid>.host.json, <sid>.host.log and hosted/<sid>/. While that process runs,
# its session is left alone (it ends with its watchdog and unregisters the bot
# itself); the files go only once the session file is gone: the readiness
# file at once, the log after a day, the state directory after an hour.
#
# Runs on the HOST: session files never enter a sandbox.
set -uo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
BIN="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

APPLY=0
FORCE=0
for arg in "$@"; do
  case "$arg" in
    --apply) APPLY=1 ;;
    --force) FORCE=1 ;;
    -h|--help) sed -n '2,39p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
removed=0
kept=0
skipped=0
ACTIVE_NAMES=()
KEEP_SEC="${MM_SESSION_KEEP_SEC:-86400}"
[[ "$KEEP_SEC" =~ ^[0-9]+$ ]] || KEEP_SEC=86400

note() { echo "$*" >&2; }

field() { sed -n "s/^$2=//p" "$1" | head -n1; }

# A session is live when its supervising process still exists.
is_live() {
  local pid="$1"
  [[ -n "$pid" && "$pid" =~ ^[0-9]+$ ]] || return 1
  kill -0 "$pid" 2>/dev/null
}

# Per-bot state, not a session: the uid in its name, never a token.
STATE_RE='^(asks|coord|gate|last|listen|presence|turn|update)-[a-z0-9]{26}\.env$|^stats-[a-z0-9]{26}\.jsonl$'
# The MCP server's hook API endpoint (RT-6.17, endpoint-[sid-]<key>.env): no
# session and no token. A server removes its own at exit; one left by a crash
# names a port nobody listens on, and the hooks stay quiet. Section 4 removes
# those whose server is gone.
ENDPOINT_RE='^endpoint-(sid-)?[0-9a-f]{16}\.env$'
is_state_file() { [[ "$(basename "$1")" =~ $STATE_RE ]]; }
not_a_session() { is_state_file "$1" || [[ "$(basename "$1")" =~ $ENDPOINT_RE ]]; }

# The registrar's username prefix (arc42 §7.1): this shell's own bot tells it,
# else the first session file that does, else the default "agent-".
PREFIX="$(mm_agent_prefix)"
if [[ -z "${MM_AGENT_PREFIX:-}" && -z "${MM_REG_BOT_NAME:-}" ]]; then
  shopt -s nullglob
  for f in "$STATE_DIR"/*.env; do
    not_a_session "$f" && continue
    [[ -n "$(field "$f" MM_AGENT_PREFIX)$(field "$f" MM_REG_BOT_NAME)" ]] || continue
    PREFIX="$(MM_AGENT_PREFIX="$(field "$f" MM_AGENT_PREFIX)" \
      MM_BOT_USERNAME="$(field "$f" MM_BOT_USERNAME)" \
      MM_REG_BOT_NAME="$(field "$f" MM_REG_BOT_NAME)" mm_agent_prefix)"
    break
  done
  shopt -u nullglob
fi

# --- 1. session files --------------------------------------------------------
shopt -s nullglob
for f in "$STATE_DIR"/*.env; do
  not_a_session "$f" && continue
  name="$(field "$f" MM_REG_BOT_NAME)"
  # Files from before MM_REG_BOT_NAME carry only the username.
  [[ -n "$name" ]] || { name="$(field "$f" MM_BOT_USERNAME)"; name="${name#"$PREFIX"}"; }
  [[ -n "$name" ]] || { note "skip $(basename "$f") (no bot name)"; continue; }

  pid="$(field "$f" MM_SESSION_PID)"
  hpid="$(field "$f" MM_BUS_HOST_PID)"
  if [[ "$FORCE" -eq 0 ]] && ! is_live "$pid" && is_live "$hpid"; then
    note "HOSTED  ${name} (its MCP server on the host, pid ${hpid}, still runs and ends with its watchdog) — leaving it alone"
    ACTIVE_NAMES+=("$name")
    skipped=$((skipped + 1))
    continue
  fi
  if [[ "$FORCE" -eq 0 ]] && is_live "$pid"; then
    note "ACTIVE  ${name} (pid ${pid} alive) — leaving it alone"
    ACTIVE_NAMES+=("$name")
    skipped=$((skipped + 1))
    continue
  fi
  if [[ "$FORCE" -eq 0 && -z "$pid" ]]; then
    # Written before this guard existed: cannot prove it is dead.
    note "UNKNOWN ${name} (no MM_SESSION_PID) — skipping; use --force to include"
    ACTIVE_NAMES+=("$name")
    skipped=$((skipped + 1))
    continue
  fi
  # An MCP-registered bot is kept for `claude --continue` (RT-6.10) until its
  # file has been untouched for the grace period; the server's exit touches it.
  if [[ "$FORCE" -eq 0 && "$(basename "$f")" == mcp-* ]]; then
    age=$(( $(date +%s) - $(stat -c %Y "$f" 2>/dev/null || stat -f %m "$f") ))
    if (( age < KEEP_SEC )); then
      note "KEPT    ${name} (ended $((age / 3600)) h ago, kept for claude --continue until $((KEEP_SEC / 3600)) h) — use --force to include"
      ACTIVE_NAMES+=("$name")
      skipped=$((skipped + 1))
      continue
    fi
  fi

  if [[ "$APPLY" -eq 0 ]]; then
    echo "would unregister ${name}  (from $(basename "$f"))"
    continue
  fi
  # Each subshell loads one bot's session file, so it cannot leak into the next.
  # shellcheck disable=SC2030,SC2031
  if err="$( (
      export MM_AGENT_SESSION_FILE="$f"
      mm_load_session_file || exit 1
      exec "$BIN/mm-agent-unregister.sh" "$name" 2>&1 >/dev/null
     ) )"; then
    note "unregistered ${name}"
    rm -f "$f"
    removed=$((removed + 1))
  elif [[ "$( (
      export MM_AGENT_SESSION_FILE="$f"
      mm_load_session_file >/dev/null 2>&1 || exit 0
      mm_api GET "/api/v4/users/me" >/dev/null 2>&1 || exit 0
      printf '%s' "$MM_API_STATUS"
     ) )" == "401" ]]; then
    # The bot's own token is rejected: the bot is gone already (its teardown
    # deleted it, or the server revoked the token). Only the file is left.
    note "gone    ${name} (its token is no longer accepted) — removed $(basename "$f")"
    rm -f "$f"
    removed=$((removed + 1))
  else
    note "FAILED ${name}: ${err:-no reason given} — keeping $(basename "$f")"
    kept=$((kept + 1))
  fi
done
shopt -u nullglob

# --- 2. orphans with no session file ----------------------------------------
if [[ -n "${MM_REG_SECRET:-}" && -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]]; then
  team="${MM_TEAM_ID:-}"
  if [[ -z "$team" ]]; then
    mm_api GET "/api/v4/teams/name/$(mm_team)" &&
      [[ "$MM_API_STATUS" == "200" ]] &&
      team="$(jq -r '.id' <<<"$MM_API_BODY")"
  fi
  if [[ -n "$team" ]] && mm_api GET "/api/v4/users?in_team=${team}&per_page=200" &&
     [[ "$MM_API_STATUS" == "200" ]]; then
    while read -r uname; do
      [[ -n "$uname" ]] || continue
      short="${uname#"$PREFIX"}"
      # Never touch a session we just classified as live, nor ourselves.
      skip=0
      for a in ${ACTIVE_NAMES+"${ACTIVE_NAMES[@]}"}; do
        [[ "$short" == "$a" ]] && skip=1 && break
      done
      [[ "$short" == "${MM_REG_BOT_NAME:-}" ]] && skip=1
      [[ "$uname" == "${MM_BOT_USERNAME:-}" ]] && skip=1
      [[ "$skip" -eq 1 ]] && continue

      if [[ "$APPLY" -eq 0 ]]; then
        echo "would unregister ${uname}  (orphan, no session file)"
        continue
      fi
      if MM_AGENT_PREFIX="$PREFIX" "$BIN/mm-agent-unregister.sh" "$short" >/dev/null 2>&1; then
        note "unregistered ${uname}"
        removed=$((removed + 1))
      else
        note "FAILED ${uname}"
        kept=$((kept + 1))
      fi
    done < <(jq -r --arg p "$PREFIX" '.[] | select(.username | startswith($p)) | select(.delete_at == 0) | .username' <<<"$MM_API_BODY")
  fi
elif [[ "$APPLY" -eq 1 ]]; then
  note "(set MM_REG_SECRET to also sweep orphans that have no session file)"
fi

# --- 3. state files of bots without a session ---------------------------------
# Bots that still have a session file (live, or kept after a failed unregister)
# keep their state; so does this shell's own bot.
KEEP_UIDS=" ${MM_BOT_USER_ID:-} "
shopt -s nullglob
for f in "$STATE_DIR"/*.env; do
  not_a_session "$f" || KEEP_UIDS+="$(field "$f" MM_BOT_USER_ID) "
done
stale=0
for f in "$STATE_DIR"/*-*.env "$STATE_DIR"/stats-*.jsonl; do
  is_state_file "$f" || continue
  uid="$(basename "$f")"; uid="${uid#*-}"; uid="${uid%%.*}"
  [[ "$KEEP_UIDS" == *" $uid "* ]] && continue
  [[ -n "$(find "$f" -mtime +0 2>/dev/null)" ]] || continue   # touched within a day
  stale=$((stale + 1))
  [[ "$APPLY" -eq 1 ]] && rm -f "$f"
done
shopt -u nullglob
if (( stale > 0 )); then
  if [[ "$APPLY" -eq 1 ]]; then
    note "removed ${stale} state file(s) of bots without a session"
  else
    echo "would remove ${stale} state file(s) of bots without a session"
  fi
fi

# --- 4. endpoint files of servers that are gone ------------------------------
# A crashed MCP server leaves its endpoint file (RT-6.17). Removed once its
# MM_BUS_PID is no longer alive and the file is an hour old: a server starting
# right now may not have been seen alive yet.
gone=0
for f in "$STATE_DIR"/endpoint-*.env; do
  [[ "$(basename "$f")" =~ $ENDPOINT_RE ]] || continue
  is_live "$(field "$f" MM_BUS_PID)" && continue
  [[ -n "$(find "$f" -mmin +60 2>/dev/null)" ]] || continue
  gone=$((gone + 1))
  [[ "$APPLY" -eq 1 ]] && rm -f "$f"
done
if (( gone > 0 )); then
  if [[ "$APPLY" -eq 1 ]]; then
    note "removed ${gone} endpoint file(s) of MCP servers that are gone"
  else
    echo "would remove ${gone} endpoint file(s) of MCP servers that are gone"
  fi
fi

# --- 5. the host process's files of ended sessions (RT-6.18) -------------------
hostfiles=0
shopt -s nullglob
for f in "$STATE_DIR"/*.host.json "$STATE_DIR"/*.host.log; do
  [[ -f "${f%.host.*}.env" ]] && continue
  [[ "$f" == *.json || -n "$(find "$f" -mmin +1440 2>/dev/null)" ]] || continue
  hostfiles=$((hostfiles + 1))
  [[ "$APPLY" -eq 1 ]] && rm -f "$f"
done
for d in "$STATE_DIR"/hosted/*; do
  [[ -d "$d" && ! -L "$d" ]] || continue
  [[ -f "$STATE_DIR/$(basename "$d").env" ]] && continue
  [[ -n "$(find "$d" -maxdepth 0 -mmin +60 2>/dev/null)" ]] || continue
  hostfiles=$((hostfiles + 1))
  [[ "$APPLY" -eq 1 ]] && rm -rf "$d"
done
shopt -u nullglob
if (( hostfiles > 0 )); then
  if [[ "$APPLY" -eq 1 ]]; then
    note "removed ${hostfiles} file(s) of host processes whose session ended"
  else
    echo "would remove ${hostfiles} file(s) of host processes whose session ended"
  fi
fi

if [[ "$APPLY" -eq 1 ]]; then
  note "sweep done: ${removed} removed, ${kept} failed, ${skipped} left alone as active"
else
  note "dry run — re-run with --apply to remove"
fi

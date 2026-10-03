#!/usr/bin/env bash
# corral preStart hook: register an ephemeral Mattermost bot for this session and
# hand the minted token to the sandbox.
#
# Runs on the HOST, outside the sandbox. That is the whole point: MM_REG_SECRET
# stays here and only the short-lived bot token crosses the boundary.
#
# stdout is reserved for corral's contribution interface — exactly one JSON
# object, or nothing. Every human-readable line goes to stderr.
set -euo pipefail

BIN="$(cd "$(dirname "$0")/../../bin" && pwd)"

log() { echo "mm-bus: $*" >&2; }

# --- secret, gopass preferred ------------------------------------------------
# gopass first: the secret then lives in neither a shell env nor a config file.
if [[ -n "${MM_REG_SECRET_GOPASS:-}" ]] && command -v gopass >/dev/null 2>&1; then
  if ! MM_REG_SECRET="$(gopass show -o "$MM_REG_SECRET_GOPASS" 2>/dev/null)"; then
    log "gopass could not read '$MM_REG_SECRET_GOPASS' — no bot for this session"
    exit 0
  fi
  export MM_REG_SECRET
elif [[ -n "${MM_REG_SECRET:-}" ]]; then
  log "using MM_REG_SECRET from the environment"
else
  # No bus configured on this machine. Stay out of the way rather than failing
  # every corral run.
  exit 0
fi

if [[ -z "${MM_CHAT_URL:-${MATTERMOST_URL:-}}" ]]; then
  log "MM_CHAT_URL is not set — no bot for this session"
  exit 0
fi

# --- identity ----------------------------------------------------------------
# Name must satisfy mm_validate_short_name: 3–32 chars, lowercase alnum/hyphen,
# starting and ending alphanumeric.
raw="${CORRAL_SESSION_ID:-$$}"
slug="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')"
NAME="cc-${slug}"
NAME="${NAME:0:32}"
while [[ "$NAME" == *- ]]; do NAME="${NAME%-}"; done   # must end alphanumeric
[[ ${#NAME} -ge 3 ]] || NAME="cc-session"

# One session file per corral session; the shared default would be clobbered by
# concurrent sessions.
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
MM_AGENT_SESSION_FILE="${STATE_DIR}/${CORRAL_SESSION_ID:-$$}.env"
export MM_AGENT_SESSION_FILE

# --- register ----------------------------------------------------------------
# Capture the export lines from stdout; the script's own diagnostics are on
# stderr and pass through to corral's banner untouched.
if ! exports="$("$BIN/mm-agent-register.sh" --ephemeral --exports "$NAME")"; then
  log "registration failed — continuing without a bot"
  exit 0
fi
eval "$exports"

if [[ -z "${MM_BOT_TOKEN:-}" ]]; then
  log "registrar returned no token — continuing without a bot"
  exit 0
fi

# --- channels ----------------------------------------------------------------
# From here on a failure must not leak the bot account we just created.
cleanup_and_fail() {
  log "$1"
  "$BIN/mm-agent-unregister.sh" "$NAME" >/dev/null 2>&1 || true
  rm -f "$MM_AGENT_SESSION_FILE"
  exit 1
}

if ! chan_exports="$("$BIN/mm-agent-channels.sh" resolve)"; then
  cleanup_and_fail "channel resolution failed — unregistered ${NAME} again"
fi
eval "$chan_exports"

# --- contribution ------------------------------------------------------------
# MM_BOT_NAME is deliberately withheld: nothing inside the sandbox should be able
# to ask the registrar to delete an agent.
note="You are @${MM_BOT_USERNAME} on the Mattermost agent bus."
if [[ -n "${MM_PROJECT_CHANNEL:-}" ]]; then
  note+=" Work in ~${MM_PROJECT_CHANNEL}; you are also addressable in ~${MM_CHANNEL}."
else
  note+=" Home channel: ~${MM_CHANNEL}."
fi
note+=" The session is managed by the host — do not register or unregister."

jq -nc \
  --arg chat "$MM_CHAT_URL" \
  --arg reg "${MM_REGISTER_URL:-}" \
  --arg tok "$MM_BOT_TOKEN" \
  --arg user "$MM_BOT_USERNAME" \
  --arg uid "$MM_BOT_USER_ID" \
  --arg team "${MM_TEAM:-}" \
  --arg teamid "${MM_TEAM_ID:-}" \
  --arg chan "${MM_CHANNEL:-}" \
  --arg chanid "${MM_CHANNEL_ID:-}" \
  --arg proj "${MM_PROJECT_CHANNEL:-}" \
  --arg projid "${MM_PROJECT_CHANNEL_ID:-}" \
  --arg note "$note" \
  --arg status "registered ${MM_BOT_USERNAME}, home #${MM_CHANNEL:-}${MM_PROJECT_CHANNEL:+, work #${MM_PROJECT_CHANNEL}}" \
  '{
     corralContributionVersion: 1,
     env: ({
       MM_CHAT_URL: $chat,
       MM_BOT_TOKEN: $tok,
       MM_BOT_USERNAME: $user,
       MM_BOT_USER_ID: $uid,
       MM_TEAM: $team,
       MM_TEAM_ID: $teamid,
       MM_CHANNEL: $chan,
       MM_CHANNEL_ID: $chanid,
     }
     + (if $reg  != "" then {MM_REGISTER_URL: $reg} else {} end)
     + (if $proj != "" then {MM_PROJECT_CHANNEL: $proj,
                             MM_PROJECT_CHANNEL_ID: $projid} else {} end)),
     agentNotes: [$note],
     status: [$status]
   }'

log "session bot ${MM_BOT_USERNAME} ready"

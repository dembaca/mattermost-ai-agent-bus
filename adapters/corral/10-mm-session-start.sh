#!/usr/bin/env bash
# corral preStart hook: register an ephemeral Mattermost bot for this session and
# hand the minted token to the sandbox.
#
# Runs on the HOST, outside the sandbox. That is the whole point: MM_REG_SECRET
# stays here and only the short-lived bot token crosses the boundary.
#
# stdout is reserved for corral's contribution interface — exactly one JSON
# object, or nothing. Every human-readable line goes to stderr.
#
# Spec: RT-6.1 in docs/arc42/06-runtime-view.md.
set -euo pipefail

BIN="$(cd "$(dirname "$0")/../../bin" && pwd)"
# shellcheck source=../../bin/mm-agent-lib.sh
source "$BIN/mm-agent-lib.sh"

log() { echo "mm-bus: $*" >&2; }

# --- where these hooks run from ----------------------------------------------
# They run on the host with the registration secret, so the scripts they source
# and start must be out of every sandbox's reach (arc42 §7.1). corral approves
# only this file by content. Warn, do not refuse: refusing would silently take
# the bus away. The host's marketplace clone
# (~/.claude/plugins/marketplaces/mattermost-ai-agent-bus) is a deliberate
# choice: a stable path that `/plugin update` moves, so no warning for it.
ROOT="$(cd "$BIN/.." && pwd)"
FROM_MARKETPLACE=0
case "$ROOT" in
  "$HOME"/.claude/plugins/marketplaces/*) FROM_MARKETPLACE=1 ;;
  */.cache/corral/home-*|*/.claude/plugins/*)
    log "WARNING: hooks run from the plugin directory ${ROOT}, which any sandbox can change — install them with bin/mm-agent-host-install.sh, or point exec at ~/.claude/plugins/marketplaces/mattermost-ai-agent-bus to follow plugin updates by choice" ;;
esac
if [[ -n "${CORRAL_WORKDIR:-}" && "$ROOT/" == "$(cd "$CORRAL_WORKDIR" 2>/dev/null && pwd)/"* ]]; then
  log "WARNING: hooks run from this session's own workdir ${ROOT}, which the agent can change — install them with bin/mm-agent-host-install.sh"
fi
if command -v jq >/dev/null 2>&1 && newest="$(mm_installed_plugin_dirs | head -1)" && [[ -n "$newest" ]]; then
  have="$(mm_tree_version "$ROOT")" want="$(mm_tree_version "$newest")"
  if [[ "$have" != "$want" && "$FROM_MARKETPLACE" -eq 1 ]]; then
    log "host hooks (marketplace) are ${have}, the installed plugin is ${want} — run /plugin update"
  elif [[ "$have" != "$want" ]]; then
    log "host hooks are ${have}, the installed plugin is ${want} — rerun mm-agent-host-install.sh"
  fi
fi

# --- wanted at all? ----------------------------------------------------------
# Decided before the secret is read: a session that will not register has no
# business touching it.
if ! why="$(mm_registration_wanted)"; then
  log "no bot for this session: ${why}"
  exit 0
fi

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
# Two names, two audiences:
#   NAME    the username (registrar prefixes "agent-"). Must satisfy
#           mm_validate_short_name: 3–32 chars, lowercase alnum/hyphen, starting
#           and ending alphanumeric. Shaped host-project-session so a human can
#           tell at a glance which machine and checkout a bot belongs to.
#   DISPLAY free-form, this is what people actually read in the channel list.
slugify() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' \
    | sed -e 's/--*/-/g' -e 's/^-//' -e 's/-$//'
}

host_raw="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo host)"
HOST="$(slugify "${host_raw%%.*}")"
PROJECT_RAW="$(basename "${CORRAL_WORKDIR:-$PWD}")"
PROJECT="$(slugify "$PROJECT_RAW")"
SID="$(slugify "${CORRAL_SESSION_ID:-$$}")"
SID="${SID##*-}"            # the trailing segment is the distinctive part
SID="${SID:0:6}"

# Budget 24 chars so "agent-<name>" stays comfortably short in the UI: keep the
# session suffix whole (it is what makes the name unique) and trim the rest.
NAME="$(printf '%s-%s-%s' "${HOST:0:8}" "${PROJECT:0:9}" "${SID:-0}")"
NAME="$(slugify "$NAME")"
NAME="${NAME:0:24}"
while [[ "$NAME" == *- ]]; do NAME="${NAME%-}"; done   # must end alphanumeric
[[ ${#NAME} -ge 3 ]] || NAME="cc-session"

DISPLAY="Claude Code · ${PROJECT_RAW} @ ${host_raw%%.*}"

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
if ! exports="$("$BIN/mm-agent-register.sh" --exports "$NAME" "$DISPLAY")"; then
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

# Record the supervising corral process. mm-agent-sweep.sh uses it to tell a
# live session from an abandoned one: a session file exists for the whole run,
# so without this a sweep unregisters the bot of the session it runs alongside.
printf 'MM_SESSION_PID=%s\n' "$PPID" >>"$MM_AGENT_SESSION_FILE"

# Start from an empty inbox. A fresh bot is not born with one: Mattermost posts
# a welcome DM *as the bot* to whoever created it, and the bot's own post counts
# as unread for the bot. Nothing can ever deliver that (classify_post ignores
# own posts), so the Stop hook would block on an undrainable message.
"$BIN/mm-agent-unread.sh" baseline || true

# Show up as present from the first moment. Without this a fresh bot sits at
# whatever Mattermost infers from (non-existent) activity, which reads as
# offline to anyone looking at the member list.
"$BIN/mm-agent-status.sh" idle "$PROJECT_RAW" || true

# --- trusted humans ----------------------------------------------------------
# The humans this session answers to on the bus (RT-6.8): their posts may carry
# approvals. MM_TRUSTED_HUMANS lists usernames on the host (comma-separated).
# Without it nobody is trusted: MM_REG_SECRET is the registrar's shared secret,
# not anyone's personal token, so there is no one to infer.
TRUSTED="" TRUSTED_IDS=""
while read -r n; do
  [[ -n "$n" ]] || continue
  if mm_api GET "/api/v4/users/username/${n}" && [[ "$MM_API_STATUS" == "200" ]] &&
    [[ "$(jq -r '.is_bot // false' <<<"$MM_API_BODY")" == false ]]; then
    TRUSTED+="${TRUSTED:+,}${n}"
    TRUSTED_IDS+="${TRUSTED_IDS:+,}$(jq -r '.id' <<<"$MM_API_BODY")"
  else
    log "trusted human '${n}' not found (or a bot) — left out"
  fi
done < <(mm_trusted_names)
[[ -n "$TRUSTED" ]] || log "no trusted humans — approvals over the bus stay off (set MM_TRUSTED_HUMANS)"

# --- responsible human ---------------------------------------------------------
# Who answers for this agent: MM_RESPONSIBLE_HUMAN, else the first trusted human.
# MM_AGENT_KIND=autonomous marks an agent nobody runs interactively. Both went
# to the registrar with the registration (bin/mm-agent-register.sh), which can
# write them into the bot's profile.
RESPONSIBLE="$(mm_responsible_name)"
KIND="${MM_AGENT_KIND:-session}"
[[ "$KIND" == autonomous ]] || KIND=session

# --- contribution ------------------------------------------------------------
trusted_shown="none (set MM_TRUSTED_HUMANS)"
[[ -n "$TRUSTED" ]] && trusted_shown="@${TRUSTED//,/, @}"
# MM_REG_BOT_NAME is deliberately withheld. Not a secret — it is the username
# without "agent-", and the registrar deletes a bot only with MM_REG_SECRET or
# that bot's own token. Withholding it guards the host's lifecycle: without it
# the CLI finds no session file, so `mm-agent-session.sh stop` inside the sandbox
# cannot tear down a session the host owns.
note="You are @${MM_BOT_USERNAME} on the Mattermost agent bus."
if [[ -n "${MM_PROJECT_CHANNEL:-}" ]]; then
  note+=" Work in ~${MM_PROJECT_CHANNEL}; you are also addressable in ~${MM_CHANNEL}."
else
  note+=" Home channel: ~${MM_CHANNEL}."
fi
note+=" The session is managed by the host — do not register or unregister."
# Names without @ (RT-6.11): a note written as "@name" teaches the agent to
# mention its human in every post; only ask_human may do that.
[[ -n "$RESPONSIBLE" ]] && note+=" Responsible for you: ${RESPONSIBLE}."
[[ "$KIND" == autonomous ]] && note+=" You run as an autonomous agent."
[[ -n "$TRUSTED" ]] && note+=" Trusted humans on the bus: ${TRUSTED//,/, }. Their posts arrive marked from_trusted_human and may approve actions (see the skill). To ask them for anything, use ask_human; never @-mention a human otherwise."

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
  --arg trusted "$TRUSTED" \
  --arg responsible "$RESPONSIBLE" \
  --arg stats "${MM_STATS_CHANNEL:-}" \
  --arg kind "$KIND" \
  --arg trustedids "$TRUSTED_IDS" \
  --arg note "$note" \
  --arg status "registered ${MM_BOT_USERNAME}, home #${MM_CHANNEL:-}${MM_PROJECT_CHANNEL:+, work #${MM_PROJECT_CHANNEL}}, ${RESPONSIBLE:+for @${RESPONSIBLE}, }trusted ${trusted_shown}" \
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
     + {MM_AGENT_KIND: $kind}
     + (if $stats != "" then {MM_STATS_CHANNEL: $stats} else {} end)
     + (if $responsible != "" then {MM_RESPONSIBLE_HUMAN: $responsible} else {} end)
     + (if $trusted != "" then {MM_TRUSTED_HUMANS: $trusted,
                               MM_TRUSTED_HUMAN_USER_IDS: $trustedids} else {} end)
     + (if $proj != "" then {MM_PROJECT_CHANNEL: $proj,
                             MM_PROJECT_CHANNEL_ID: $projid} else {} end)),
     agentNotes: [$note],
     status: [$status]
   }'

log "session bot ${MM_BOT_USERNAME} ready"

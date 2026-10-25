#!/usr/bin/env bash
# corral preStart hook: register an ephemeral Mattermost bot for this session,
# start the session's MCP server on the host and hand the sandbox its endpoints.
#
# Runs on the HOST, outside the sandbox. That is the whole point: MM_REG_SECRET
# and the bot token stay here (RT-6.18). The sandbox gets the bot's names, the
# channels and a per-session key for the host process, never a credential.
#
# stdout is reserved for corral's contribution interface — exactly one JSON
# object, or nothing. Every human-readable line goes to stderr.
#
# Spec: RT-6.1 and RT-6.18 in docs/arc42/06-runtime-view.md.
set -euo pipefail

# No credential in any process environment (arc42 §7.3): on macOS a sandbox can
# read the initial environment of every process of this user. One that arrived
# in ours stays visible in this process, but no child gets it: the token comes
# from the registration, the secret goes to helpers on an fd only.
unset MM_BOT_TOKEN MATTERMOST_TOKEN CDPATH
export -n MM_REG_SECRET 2>/dev/null || true

# --- what a sandbox can write (arc42 §7.1, §7.3) -------------------------------
# Every corral home, ~/.claude (the homes share it), the session's workdir and
# corral's temporary directories; a path that is not absolute or has a . or ..
# component counts as one, too. Compared without regard to case, as macOS
# volumes do: ~/.CLAUDE is ~/.claude there. Same as mm_sandbox_can_write in
# bin/mm-agent-lib.sh, inline: nothing of the tree is sourced before the check.
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
# Commands only from absolute PATH entries no sandbox can write, and from /:
# the workdir is the sandbox's (a `.` or relative entry would find its curl).
path_rest="${PATH:-}:" safe_path=""
while [[ -n "$path_rest" ]]; do
  e="${path_rest%%:*}" path_rest="${path_rest#*:}"
  sandbox_can_write "${e%/}" || safe_path="${safe_path:+$safe_path:}$e"
done
export PATH="${safe_path:-/usr/bin:/bin:/usr/sbin:/sbin}"
self="$0"
[[ "$self" == /* ]] || self="$PWD/$self"
START_DIR="$PWD"
cd /

log() { echo "mm-bus: $*" >&2; }

REGISTERED=0
# Fail closed: no bus for this session. The bot goes again (its session file
# stays only when that fails, for postEnd or the sweep to retry); the sandbox
# learns why (MM_BUS_UNAVAILABLE, the note) and gets nothing else.
no_bus() {
  local why="$1"
  log "no bus for this session, the token stays on the host: ${why}"
  if [[ "$REGISTERED" -eq 1 ]]; then
    mm_with_token "$BIN/mm-agent-status.sh" offline >/dev/null 2>&1 || true
    if unregister_again; then
      rm -f "$MM_AGENT_SESSION_FILE"
    else
      log "WARNING: could not unregister ${NAME} again; postEnd retries"
    fi
  fi
  rm -f "${HOST_READY:-}"
  MM_BUS_UNAVAILABLE="$why" jq -nc \
    --arg note "The Mattermost agent bus is off in this session: ${why}. The bot token stays on the host, so there is no bus here: do not register a bot or look for a token. Tell your human." \
    --arg status "no bus: ${why}" \
    '{corralContributionVersion: 1, env: {MM_BUS_UNAVAILABLE: env.MM_BUS_UNAVAILABLE},
      agentNotes: [$note], status: [$status]}'
  exit 0
}

# --- where this hook runs from (arc42 §7.1) ------------------------------------
# It runs on the host with the registration secret and starts the session's MCP
# server with the bot token, so nothing of its tree may be in a sandbox's reach.
# corral approves only this file by content; what it sources and starts is the
# tree around it. Checked here, inline, before anything of that tree is sourced
# or run: the path corral ran, every symlink on the way to this file, the real
# tree and the Python it would start (and its symlinks) must all lie outside
# every place a sandbox can write (sandbox_can_write above: the corral homes,
# ~/.claude with the plugin's marketplace clone and cache, the session's
# workdir, corral's temporary directories). A symlink in such a place could be
# pointed at another tree. Each step is checked as written (no `..`) and with
# its directory resolved, so neither `..` through a symlink nor a change of
# case gets past it.
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
BIN="$ROOT/bin"
HOST_PY="$ROOT/mcp/.venv/bin/python"
seen+=("$ROOT")
# The Python's symlinks may go through `..` (Homebrew's do): each by its
# resolved directory.
py="$HOST_PY" hops=0
while :; do
  if d="$(cd -P "$(dirname "$py")" 2>/dev/null && pwd -P)"; then seen+=("$d"); fi
  [[ -L "$py" && "$hops" -lt 40 ]] || break
  link="$(readlink "$py")"
  [[ "$link" == /* ]] || link="$(dirname "$py")/$link"
  py="$link" hops=$((hops + 1))
done
unsafe=""
for p in "${seen[@]}"; do
  if sandbox_can_write "$p"; then unsafe="${p:-$0}"; fi
done
shopt -u nocasematch
if [[ -n "$unsafe" ]]; then
  no_bus "the host hooks run from ${unsafe}, which a sandbox can change — install them from a reviewed clone with bin/mm-agent-host-install.sh and point corral's exec at ~/.local/share/mm-agent-bus/current/adapters/corral/"
fi

# shellcheck source=../../bin/mm-agent-lib.sh
source "$BIN/mm-agent-lib.sh"

# Unregister the bot this run registered: with its own token first, which the
# registrar always accepts for it, then with the registration secret.
unregister_again() {
  mm_with_token "$BIN/mm-agent-unregister.sh" "$NAME" >/dev/null 2>&1 ||
    { [[ -n "${MM_REG_SECRET:-}" ]] && mm_with_secret "$BIN/mm-agent-unregister.sh" "$NAME" >/dev/null 2>&1; }
}

# Version only: the installed plugins include the corral homes, which a sandbox
# writes, so the notice names no path and does not tell where to install from.
if command -v jq >/dev/null 2>&1 && want="$(mm_newest_plugin_version)" &&
   mm_version_newer "$(mm_tree_version "$ROOT")" "$want"; then
  log "host hooks are $(mm_tree_version "$ROOT"), plugin ${want} is installed — reinstall them from a clone you reviewed: mm-agent-host-install.sh --from <clone>"
fi

# --- wanted at all? ----------------------------------------------------------
# Decided before the secret is read: a session that will not register has no
# business touching it.
if ! why="$(mm_registration_wanted)"; then
  log "no bot for this session: ${why}"
  exit 0
fi

# --- secret, gopass preferred ------------------------------------------------
# gopass: the secret then lives in no environment and no config file; here it
# is a variable this hook does not export (arc42 §7.3).
if [[ -n "${MM_REG_SECRET_GOPASS:-}" ]] && command -v gopass >/dev/null 2>&1; then
  if ! MM_REG_SECRET="$(gopass show -o "$MM_REG_SECRET_GOPASS" 2>/dev/null)" || [[ -z "$MM_REG_SECRET" ]]; then
    log "gopass could not read '$MM_REG_SECRET_GOPASS' — no bot for this session"
    exit 0
  fi
elif [[ -n "${MM_REG_SECRET:-}" ]]; then
  log "WARNING: MM_REG_SECRET comes from the login environment, where other corral sandboxes can read it (arc42 §7.3) — keep it in gopass and set MM_REG_SECRET_GOPASS instead"
else
  # No bus configured on this machine. Stay out of the way rather than failing
  # every corral run.
  exit 0
fi

if [[ -z "${MM_CHAT_URL:-${MATTERMOST_URL:-}}" ]]; then
  log "MM_CHAT_URL is not set — no bot for this session"
  exit 0
fi

# The session's MCP server runs from this tree, on the host (RT-6.18): without
# its Python environment there is nothing to keep the token, so no bot at all.
[[ -x "$HOST_PY" ]] ||
  no_bus "the MCP server has no Python environment on the host (${ROOT}/mcp/.venv) — rerun bin/mm-agent-host-install.sh (needs uv), then restart the session"

# --- identity ----------------------------------------------------------------
# Two names, two audiences:
#   NAME    the username without the registrar's prefix ("agent-" by default).
#           Must satisfy mm_validate_short_name: 3–32 chars, lowercase
#           alnum/hyphen, starting and ending alphanumeric. Shaped
#           host-project-session so a human can tell at a glance which machine
#           and checkout a bot belongs to.
#   DISPLAY free-form, this is what people actually read in the channel list.
slugify() {
  printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-' \
    | sed -e 's/--*/-/g' -e 's/^-//' -e 's/-$//'
}

host_raw="$(hostname -s 2>/dev/null || hostname 2>/dev/null || echo host)"
HOST="$(slugify "${host_raw%%.*}")"
PROJECT_RAW="$(basename "${CORRAL_WORKDIR:-$START_DIR}")"
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
# Host files are named by SESSION_KEY: corral's session id when it is a safe
# file name, else this hook's pid.
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"
SESSION_KEY="${CORRAL_SESSION_ID:-$$}"
[[ "$SESSION_KEY" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]] || SESSION_KEY="$$"
MM_AGENT_SESSION_FILE="${STATE_DIR}/${SESSION_KEY}.env"
export MM_AGENT_SESSION_FILE

# --- register ----------------------------------------------------------------
# Capture the lines from stdout; the script's own diagnostics are on stderr and
# pass through to corral's banner untouched. Every helper gets its credential
# on fd 3 (mm_with_secret, mm_with_token), never in its environment or argv
# (arc42 §7.3); the token comes back as a plain assignment, not exported.
if ! exports="$(mm_with_secret "$BIN/mm-agent-register.sh" --exports "$NAME" "$DISPLAY")"; then
  log "registration failed — continuing without a bot"
  exit 0
fi
eval "$exports"
export -n MM_BOT_TOKEN MATTERMOST_TOKEN

if [[ -z "${MM_BOT_TOKEN:-}" ]]; then
  log "registrar returned no token — continuing without a bot"
  exit 0
fi
REGISTERED=1

# --- channels ----------------------------------------------------------------
# From here on a failure must not leak the bot account we just created.
cleanup_and_fail() {
  log "$1"
  if unregister_again; then rm -f "$MM_AGENT_SESSION_FILE"; fi
  exit 1
}

if ! chan_exports="$(mm_with_token "$BIN/mm-agent-channels.sh" resolve)"; then
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
mm_with_token "$BIN/mm-agent-unread.sh" baseline || true

# Show up as present from the first moment. Without this a fresh bot sits at
# whatever Mattermost infers from (non-existent) activity, which reads as
# offline to anyone looking at the member list.
mm_with_token "$BIN/mm-agent-status.sh" idle "$PROJECT_RAW" || true

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
# without the registrar's prefix, and the registrar deletes a bot only with
# MM_REG_SECRET or that bot's own token. Withholding it guards the host's
# lifecycle: without it the CLI finds no session file, so `mm-agent-session.sh
# stop` inside the sandbox cannot tear down a session the host owns. The prefix
# goes along instead (MM_AGENT_PREFIX, arc42 §7.1): the sandbox cannot derive it.
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

note+=" The bot token stays on the host: the bus's MCP tools are your way onto it; the shell CLI has no token here."

# Everything the sandbox gets, and nothing secret: no token, no registration
# secret. The token goes to the host process below, on stdin.
ENV_JSON="$(jq -nc \
  --arg chat "$MM_CHAT_URL" \
  --arg reg "${MM_REGISTER_URL:-}" \
  --arg user "$MM_BOT_USERNAME" \
  --arg prefix "$(mm_agent_prefix)" \
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
  --arg push "${MM_CHANNEL_PUSH:-}" \
  --arg kind "$KIND" \
  --arg trustedids "$TRUSTED_IDS" \
  '{
     MM_CHAT_URL: $chat,
     MM_BOT_USERNAME: $user,
     MM_AGENT_PREFIX: $prefix,
     MM_BOT_USER_ID: $uid,
     MM_TEAM: $team,
     MM_TEAM_ID: $teamid,
     MM_CHANNEL: $chan,
     MM_CHANNEL_ID: $chanid,
   }
   + (if $reg  != "" then {MM_REGISTER_URL: $reg} else {} end)
   + {MM_AGENT_KIND: $kind}
   + (if $stats != "" then {MM_STATS_CHANNEL: $stats} else {} end)
   + (if $push != "" then {MM_CHANNEL_PUSH: $push} else {} end)
   + (if $responsible != "" then {MM_RESPONSIBLE_HUMAN: $responsible} else {} end)
   + (if $trusted != "" then {MM_TRUSTED_HUMANS: $trusted,
                             MM_TRUSTED_HUMAN_USER_IDS: $trustedids} else {} end)
   + (if $proj != "" then {MM_PROJECT_CHANNEL: $proj,
                           MM_PROJECT_CHANNEL_ID: $projid} else {} end)')"

# --- the session's MCP server on the host (RT-6.18) ----------------------------
# Keyless: the bot token never enters the sandbox. The session's MCP server runs
# here as a host process until postEnd: the plugin's MCP entry point in the
# sandbox relays stdio to it (MM_BUS_MCP_ENDPOINT), and the turn hooks and the
# listener call its hook API (MM_BUS_ENDPOINT), both with a per-session key
# (MM_BUS_KEY). Fail closed: if it does not come up, the session gets no bus at
# all, never the token — the bot is unregistered again and the banner says why.
HOST_READY="${STATE_DIR}/${SESSION_KEY}.host.json"
HOST_LOG="${STATE_DIR}/${SESSION_KEY}.host.log"
# The process's own state directory, chosen here: never one the sandbox names.
HOSTED_DIR="${STATE_DIR}/hosted/${SESSION_KEY}"

# ps prints a start time with padding; one form for comparing.
proc_start() { ps -o lstart= -p "$1" 2>/dev/null | tr -s ' ' | sed -e 's/^ //' -e 's/ $//'; }

# A host process outlives its session only when corral was killed (no postEnd)
# and its own watchdog failed too. Stop it; its exit work unregisters the bot.
# Pid reuse: only a process that started when the one recorded did. Then the
# files of ended sessions: readiness files, logs after a day, state directories
# after an hour (their process removes them itself; these are a crash's).
sweep_host_processes() {
  local f base hpid hstart cpid d
  shopt -s nullglob
  for f in "$STATE_DIR"/*.env; do
    [[ "$f" != "$MM_AGENT_SESSION_FILE" ]] || continue
    hpid="$(sed -n 's/^MM_BUS_HOST_PID=//p' "$f" | head -n1)"
    [[ "$hpid" =~ ^[0-9]+$ ]] || continue
    cpid="$(sed -n 's/^MM_SESSION_PID=//p' "$f" | head -n1)"
    if [[ "$cpid" =~ ^[0-9]+$ ]] && kill -0 "$cpid" 2>/dev/null; then continue; fi
    hstart="$(sed -n 's/^MM_BUS_HOST_START=//p' "$f" | head -n1)"
    if [[ -n "$hstart" ]] && kill -0 "$hpid" 2>/dev/null && [[ "$(proc_start "$hpid")" == "$hstart" ]]; then
      kill -TERM "$hpid" 2>/dev/null && log "stopped the MCP server of ended session $(basename "$f" .env) (pid ${hpid})"
    fi
  done
  for f in "$STATE_DIR"/*.host.json "$STATE_DIR"/*.host.log; do
    base="${f%.host.*}"
    [[ -f "${base}.env" ]] && continue
    if [[ "$f" == *.json ]] || [[ -n "$(find "$f" -mmin +1440 2>/dev/null)" ]]; then rm -f "$f"; fi
  done
  for d in "$STATE_DIR"/hosted/*; do
    [[ -d "$d" && ! -L "$d" ]] || continue
    [[ -f "$STATE_DIR/$(basename "$d").env" ]] && continue
    [[ -n "$(find "$d" -maxdepth 0 -mmin +60 2>/dev/null)" ]] && rm -rf "$d"
  done
  shopt -u nullglob
}

sweep_host_processes

BUS_KEY="$(od -An -N32 -tx1 /dev/urandom | tr -d ' \n')"
[[ ${#BUS_KEY} -eq 64 ]] || no_bus "no random key for the session (od /dev/urandom failed)"
mkdir -p "$STATE_DIR/hosted"
chmod 700 "$STATE_DIR/hosted"

# The host process starts isolated: `python -I` (no working directory, no
# PYTHON* variables, no user site on its path), from the host tree, with an
# environment of the host's own basics only (no PYTHON*, UV_*, DYLD_*,
# BASH_ENV). Key, token and settings go on stdin as one JSON line, never in
# an argv or environment (jq reads both from fds fed by the builtin printf);
# the registration secret not at all. It detaches itself and returns
# once it listens; nothing of it may hold this hook's stdout or stderr.
host_env=(HOME="$HOME" PATH="/usr/bin:/bin:/usr/sbin:/sbin")
for v in USER LOGNAME LANG LC_ALL LC_CTYPE TMPDIR; do
  if [[ -n "${!v:-}" ]]; then host_env+=("${v}=${!v}"); fi
done
HOST_PID="" HOST_PORT="" HOST_HOOK=""
if jq -nc --rawfile key /dev/fd/4 --rawfile tok /dev/fd/5 --argjson base "$ENV_JSON" '
     {key: $key,
      env: ((env | with_entries(select(.key | test(
              "^(MM|MATTERMOST)_[A-Z0-9_]+$|^(CORRAL_WORKDIR|CORRAL_SESSION_ID|TZ|SSL_CERT_FILE|SSL_CERT_DIR)$|^(HTTPS?|NO|ALL)_PROXY$|^(https?|no|all)_proxy$")))
             | del(.MM_REG_SECRET, .MM_REG_SECRET_GOPASS, .MM_AGENT_SESSION_FILE,
                   .MM_REG_BOT_NAME, .MM_BUS_KEY, .MATTERMOST_TOKEN))
            + $base + {MM_BOT_TOKEN: $tok})}' \
       4< <(printf '%s' "$BUS_KEY") 5< <(printf '%s' "$MM_BOT_TOKEN") |
    (cd "$ROOT/mcp" && exec env -i "${host_env[@]}" "$HOST_PY" -I -m mattermost_ai_agent_bus --host \
      --ready-file "$HOST_READY" --state-dir "$HOSTED_DIR" --log-file "$HOST_LOG" --watch-pid "$PPID") \
    >>"$HOST_LOG" 2>&1 &&
  HOST_PID="$(jq -r '.pid // empty' "$HOST_READY" 2>/dev/null)" &&
  HOST_PORT="$(jq -r '.port // empty' "$HOST_READY" 2>/dev/null)" &&
  HOST_HOOK="$(jq -r '.hook // empty' "$HOST_READY" 2>/dev/null)" &&
  [[ "$HOST_PID" =~ ^[0-9]+$ && "$HOST_PORT" =~ ^[0-9]+$ && "$HOST_HOOK" =~ ^http://127\.0\.0\.1:[0-9]+$ ]]; then
  printf 'MM_BUS_HOST_PID=%s\nMM_BUS_HOST_START=%s\n' "$HOST_PID" "$(proc_start "$HOST_PID")" \
    >>"$MM_AGENT_SESSION_FILE"
else
  if [[ "$HOST_PID" =~ ^[0-9]+$ ]]; then kill -TERM "$HOST_PID" 2>/dev/null || true; fi
  no_bus "the MCP server did not start on the host (log: ${HOST_LOG}) — check the log, then restart the session"
fi

jq -nc \
  --rawfile key /dev/fd/4 \
  --argjson env "$ENV_JSON" \
  --arg mcp "tcp://127.0.0.1:${HOST_PORT}" \
  --arg hook "$HOST_HOOK" \
  --arg note "$note" \
  --arg status "registered ${MM_BOT_USERNAME}, home #${MM_CHANNEL:-}${MM_PROJECT_CHANNEL:+, work #${MM_PROJECT_CHANNEL}}, ${RESPONSIBLE:+for @${RESPONSIBLE}, }trusted ${trusted_shown}; token stays on the host" \
  '{
     corralContributionVersion: 1,
     env: ($env + {MM_BUS_ENDPOINT: $hook, MM_BUS_MCP_ENDPOINT: $mcp, MM_BUS_KEY: $key}),
     agentNotes: [$note],
     status: [$status]
   }' 4< <(printf '%s' "$BUS_KEY")

log "session bot ${MM_BOT_USERNAME} ready, its MCP server on the host (pid ${HOST_PID})"

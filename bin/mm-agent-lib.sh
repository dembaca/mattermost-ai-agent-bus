#!/usr/bin/env bash
# Shared helpers for mm-agent-*.sh (source this file; do not exec).
# shellcheck shell=bash

mm_chat_url() {
  local url="${MM_CHAT_URL:-${MATTERMOST_URL:-}}"
  if [[ -z "$url" ]]; then
    echo "MM_CHAT_URL or MATTERMOST_URL is required" >&2
    return 2
  fi
  printf '%s' "${url%/}"
}

mm_register_url() {
  local chat reg
  chat="$(mm_chat_url)" || return $?
  reg="${MM_REGISTER_URL:-${chat}/register/v1/agents}"
  printf '%s' "${reg%/}"
}

mm_bot_token() {
  local tok="${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}"
  if [[ -z "$tok" ]]; then
    echo "MM_BOT_TOKEN or MATTERMOST_TOKEN is required" >&2
    return 2
  fi
  printf '%s' "$tok"
}

mm_state_dir() {
  printf '%s' "${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
}

# The session file of THIS session: MM_AGENT_SESSION_FILE when set, else one
# named after the bot. There is deliberately no shared default — a single
# host-wide file let one session overwrite another's, and a later `stop` then
# removed whichever bot happened to be in it. Without either variable there is
# no session to name, and that is an error, never a guess.
#
# Lives under the state dir rather than /tmp: a corral sandbox has no /tmp but a
# writable $HOME, and CORRAL_SESSION_ID is only visible to host hooks — the bot
# name is the one key every side knows.
mm_session_file() {
  local f="${MM_AGENT_SESSION_FILE:-}"
  if [[ -z "$f" ]]; then
    if [[ -z "${MM_REG_BOT_NAME:-}" ]]; then
      echo "no session: set MM_AGENT_SESSION_FILE or MM_REG_BOT_NAME" >&2
      return 1
    fi
    f="$(mm_state_dir)/session-${MM_REG_BOT_NAME}.env"
  fi
  if [[ "$f" == ~* ]]; then
    f="${f/#\~/$HOME}"
  fi
  printf '%s' "$f"
}

mm_team() {
  printf '%s' "${MM_TEAM:-agents}"
}

mm_channel() {
  printf '%s' "${MM_CHANNEL:-agents}"
}

# Should a session hook register a bot for this session on its own?
# Always when MM_PROJECT_CHANNEL is set: a session working in a project channel
# takes part in the coordination, so registering is not optional there.
# Without one only when MM_REGISTER=always — a quick question to the agent would
# otherwise just add a bot, a HELLO and a sign-off to the default channel.
# Prints the reason when it says no. Explicit registration (session_start,
# mm-agent-session.sh start) is not affected.
mm_registration_wanted() {
  [[ -n "${MM_PROJECT_CHANNEL:-}" ]] && return 0
  [[ "${MM_REGISTER:-}" == "always" ]] && return 0
  echo "no MM_PROJECT_CHANNEL set (MM_REGISTER=always registers anyway)"
  return 1
}

mm_require_reg_secret() {
  if [[ -z "${MM_REG_SECRET:-}" ]]; then
    echo "MM_REG_SECRET is required" >&2
    return 2
  fi
}

mm_auth_header() {
  local tok="$1"
  printf 'Authorization: Bearer %s' "$tok"
}

mm_json_post() {
  local url="$1" tok="$2" body="$3"
  curl -fsS -X POST "$url" \
    -H "$(mm_auth_header "$tok")" \
    -H "Content-Type: application/json" \
    -d "$body"
}

# Mattermost API v4 call with the bot token.
# Sets MM_API_STATUS and MM_API_BODY instead of failing, so a caller can tell
# "404 = does not exist" from a real error. Returns non-zero only when the
# request could not be made at all.
#   mm_api GET /api/v4/teams/name/bgl
#   [[ "$MM_API_STATUS" == 200 ]] || ...
mm_api() {
  local method="$1" path="$2" payload="${3:-}"
  local chat tok resp
  chat="$(mm_chat_url)" || return $?
  tok="$(mm_bot_token)" || return $?
  # Status is appended on its own line rather than written to a temp file:
  # no writable TMPDIR is needed, which matters inside a sandbox.
  local args=(-sS -w $'\n%{http_code}' -X "$method"
    -H "$(mm_auth_header "$tok")")
  if [[ -n "$payload" ]]; then
    args+=(-H "Content-Type: application/json" -d "$payload")
  fi
  resp="$(curl "${args[@]}" "${chat}${path}" || true)"
  MM_API_STATUS="${resp##*$'\n'}"
  MM_API_BODY="${resp%$'\n'*}"
  [[ "$MM_API_STATUS" =~ ^[0-9]{3}$ ]] || return 1
}

# Print the id of the work channel: the argument (an id or a channel name in
# MM_TEAM) when given, else the project channel, else the default channel.
# Prefers resolved ids from the environment and asks the API only for names.
mm_work_channel_id() {
  local arg="${1:-}" id="" name="" team_id
  if [[ -n "$arg" ]]; then
    # Mattermost ids are 26 lowercase alphanumerics; anything else is a name.
    if [[ "$arg" =~ ^[a-z0-9]{26}$ ]]; then id="$arg"; else name="$arg"; fi
  elif [[ -n "${MM_PROJECT_CHANNEL_ID:-}" ]]; then
    id="$MM_PROJECT_CHANNEL_ID"
  elif [[ -n "${MM_PROJECT_CHANNEL:-}" ]]; then
    name="$MM_PROJECT_CHANNEL"
  elif [[ -n "${MM_CHANNEL_ID:-}" ]]; then
    id="$MM_CHANNEL_ID"
  else
    name="$(mm_channel)"
  fi
  if [[ -n "$id" ]]; then
    printf '%s' "$id"
    return 0
  fi

  team_id="${MM_TEAM_ID:-}"
  if [[ -z "$team_id" ]]; then
    mm_api GET "/api/v4/teams/name/$(mm_team)" || return 1
    if [[ "$MM_API_STATUS" != "200" ]]; then
      echo "could not resolve team '$(mm_team)': HTTP ${MM_API_STATUS}" >&2
      return 1
    fi
    team_id="$(jq -r '.id' <<<"$MM_API_BODY")"
  fi
  mm_api GET "/api/v4/teams/${team_id}/channels/name/${name}" || return 1
  if [[ "$MM_API_STATUS" != "200" ]]; then
    echo "could not resolve channel '${name}': HTTP ${MM_API_STATUS}" >&2
    return 1
  fi
  jq -r '.id' <<<"$MM_API_BODY"
}

mm_validate_short_name() {
  local name="$1"
  if ! [[ "$name" =~ ^[a-z0-9]([a-z0-9-]{0,30}[a-z0-9])?$ ]]; then
    echo "short-name must be lowercase alphanumeric/hyphen" >&2
    return 2
  fi
  if [[ ${#name} -lt 3 || ${#name} -gt 32 ]]; then
    echo "short-name length must be 3–32" >&2
    return 2
  fi
}

# Load whitelisted KEY=VALUE lines from a session file into the environment.
mm_load_env_file() {
  local f="$1"
  [[ -f "$f" ]] || return 1
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^# ]] && continue
    [[ -z "$line" ]] && continue
    local key="${line%%=*}"
    local val="${line#*=}"
    case "$key" in
      MM_CHAT_URL|MM_REG_BOT_NAME|MM_BOT_USERNAME|MM_BOT_USER_ID|MM_BOT_TOKEN|MATTERMOST_URL|MATTERMOST_TOKEN|MM_TEAM|MM_CHANNEL)
        export "$key=$val"
        ;;
    esac
  done <"$f"
}

mm_load_session_file() {
  local f
  f="$(mm_session_file)" || return 1
  mm_load_env_file "$f"
}

# Session file written by the MCP server's session_start (see
# mcp/.../session_file.py). Keyed on the Claude Code session
# (CLAUDE_CODE_SESSION_ID), so two sessions in one checkout never share a file
# (RT-6.10); with a session id there is no fallback, since a project-keyed file
# may belong to another session. Without one it is keyed on the directory the
# server runs in, the project directory Claude Code was started in. A hook may
# run deeper (the agent cd'ed into a subdirectory), so that lookup walks up from
# CLAUDE_PROJECT_DIR / $PWD to the root and takes the nearest session file. The
# keys must match session_file_path() there.
mm_hook_session_file() {
  local dir state key f sid
  state="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
  sid="${CLAUDE_CODE_SESSION_ID:-}"
  sid="${sid//[[:space:]]/}"
  if [[ -n "$sid" ]]; then
    key="$(printf '%s' "$sid" | { sha1sum 2>/dev/null || shasum; } | cut -c1-16)"
    f="${state}/mcp-sid-${key}.env"
    [[ -n "$key" && -f "$f" ]] || return 1
    printf '%s' "$f"
    return 0
  fi
  dir="$(cd "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null && pwd -P)" || return 1
  while :; do
    key="$(printf '%s' "$dir" | { sha1sum 2>/dev/null || shasum; } | cut -c1-16)"
    f="${state}/mcp-${key}.env"
    if [[ -n "$key" && -f "$f" ]]; then
      printf '%s' "$f"
      return 0
    fi
    [[ "$dir" == "/" ]] && return 1
    dir="$(dirname "$dir")"
  done
}

# For hooks: read the hook's JSON input once into MM_HOOK_INPUT and take the
# session id from it when the environment has none, so the session file lookup
# (mm_hook_session_file) keys on this session even then.
mm_read_hook_input() {
  MM_HOOK_INPUT="$(cat 2>/dev/null || true)"
  if [[ -z "${CLAUDE_CODE_SESSION_ID:-}" ]] && command -v jq >/dev/null 2>&1; then
    local sid
    sid="$(jq -r '.session_id // empty' <<<"$MM_HOOK_INPUT" 2>/dev/null || true)"
    [[ -n "$sid" ]] && export CLAUDE_CODE_SESSION_ID="$sid"
  fi
  return 0
}

# For hooks: pick up the bot token of an MCP-managed session. A no-op when the
# environment already carries a token (host-managed session).
mm_load_hook_session() {
  [[ -z "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]] || return 0
  local f
  f="$(mm_hook_session_file)" || return 1
  mm_load_env_file "$f"
}

# Per-turn state of the Stop hook (which checks already ran, the listen window),
# keyed on the bot so concurrent sessions never share one. The UserPromptSubmit
# hook removes it: every new turn starts from scratch. Fails when the bot's id is
# not known — the Stop hook then falls back to its stateless behaviour.
mm_turn_state_file() {
  local uid="${MM_BOT_USER_ID:-}"
  [[ "$uid" =~ ^[a-z0-9]{26}$ ]] || return 1
  printf '%s' "$(mm_state_dir)/turn-${uid}.env"
}

# Local state of the presence lease (RT-6.6), keyed on the bot like the turn
# state: presence-<uid>.env (last state and label the hooks reported) and
# coord-<uid>.env (crown lease, pending take, yield after a release).
mm_bot_state_file() {
  local kind="$1" uid="${2:-${MM_BOT_USER_ID:-}}"
  [[ "$uid" =~ ^[a-z0-9]{26}$ ]] || return 1
  printf '%s' "$(mm_state_dir)/${kind}-${uid}.env"
}

# How long a custom status lives without renewal (MM_LEASE_SEC, default 900 s).
mm_lease_sec() {
  local s="${MM_LEASE_SEC:-900}"
  [[ "$s" =~ ^[0-9]+$ && "$s" -gt 0 ]] || s=900
  printf '%s' "$s"
}

# The agent-* bots in a channel with what the lease rules need, oldest first,
# into ROSTER_JSON: [{id, username, create_at, presence, emoji, text, crown,
# reachable}]. A custom status counts while its lease has not expired; one
# without an expiry predates the lease (or was set by hand) and proves nothing
# about a live session, so it does not count. Reachable: not offline, a live
# status, not 💤.
# Crown: reachable with a live 👑 — that agent coordinates.
mm_roster_json() {
  local cid="$1" users now
  mm_api GET "/api/v4/users?in_channel=${cid}&per_page=200" || return 1
  [[ "$MM_API_STATUS" == "200" ]] || return 1
  users="$(jq -c '[.[] | select(.username | startswith("agent-"))
                       | select(.delete_at == 0)] | sort_by(.create_at)' <<<"$MM_API_BODY")" || return 1
  if [[ "$(jq 'length' <<<"$users")" -eq 0 ]]; then
    ROSTER_JSON="[]"
    return 0
  fi
  mm_api POST "/api/v4/users/status/ids" "$(jq -c '[.[].id]' <<<"$users")" || return 1
  [[ "$MM_API_STATUS" == "200" ]] || return 1
  now="$(mm_epoch_rfc3339 "$(date +%s)")"
  ROSTER_JSON="$(jq -c --argjson st "$MM_API_BODY" --arg now "$now" '
    ($st | map({(.user_id): .status}) | add // {}) as $s
    | map(
        ((.props.customStatus // "") | if . == "" then {} else (try fromjson catch {}) end) as $c
        | ($c.emoji // "") as $e
        | ($e != "" and ($c.duration // "") != "" and (($c.expires_at // "") > $now)) as $live
        | {id, username, create_at, presence: ($s[.id] // "offline"), emoji: $e,
           text: ($c.text // "")}
        | .reachable = (.presence != "offline" and $live and .emoji != "zzz")
        | .crown = (.reachable and .emoji == "crown"))' <<<"$users")" || return 1
}

# Overhead counters (RT-6.9): one JSON line per event in stats-<uid>.jsonl,
#   mm_stat KIND [key=value …]     e.g. mm_stat wake reason=digest
# Counted by hooks, listener and MCP server, posted once at session end to
# MM_STATS_CHANNEL (default agents-stats); MM_STATS_CHANNEL=off counts nothing.
# Never fails, never prints: a counter must not disturb a session.
mm_stats_off() { [[ "${MM_STATS_CHANNEL:-agents-stats}" =~ ^(off|none|0|false)$ ]]; }
mm_stat() {
  local kind="$1" f kv
  shift || true
  mm_stats_off && return 0
  [[ "${MM_BOT_USER_ID:-}" =~ ^[a-z0-9]{26}$ ]] || return 0
  f="$(mm_state_dir)/stats-${MM_BOT_USER_ID}.jsonl"
  command -v jq >/dev/null 2>&1 || return 0
  mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
  kv="$(printf '%s\n' "$@" | jq -cRn --arg k "$kind" --argjson t "$(date +%s)" '
    [inputs | select(contains("=")) | capture("^(?<k>[^=]+)=(?<v>.*)$")
     | {(.k): (.v | tonumber? // .)}] | add // {} | {k: $k, t: $t} + .' 2>/dev/null)" || return 0
  printf '%s\n' "$kv" >>"$f" 2>/dev/null || true
  return 0
}

# Trusted humans as configured (RT-6.8): MM_TRUSTED_HUMANS, comma-separated,
# "@" and spaces dropped; MM_OWNER (one name) until 0.17-era configs are gone.
# Prints one username per line.
mm_trusted_names() {
  local n
  IFS=',' read -ra _names <<<"${MM_TRUSTED_HUMANS:-${MM_OWNER:-}}"
  for n in "${_names[@]}"; do
    n="${n//[[:space:]]/}"; n="${n#@}"
    [[ -n "$n" ]] && printf '%s\n' "$n"
  done
  return 0
}

# Who answers for the agent: MM_RESPONSIBLE_HUMAN, else the first trusted human.
mm_responsible_name() {
  local r="${MM_RESPONSIBLE_HUMAN:-}"
  r="${r//[[:space:]]/}"; r="${r#@}"
  [[ -n "$r" ]] || r="$(mm_trusted_names | head -1)"
  printf '%s' "$r"
}

# Install paths of the mattermost-ai-agent-bus Claude Code plugin, newest
# version first: the host's own Claude config and every corral private home.
# Read-only; used to tell whether the host copy of the corral hooks is current.
mm_installed_plugin_dirs() {
  local f d
  for f in "$HOME/.claude/plugins/installed_plugins.json" \
           "$HOME"/.cache/corral/home-*/.claude/plugins/installed_plugins.json; do
    [[ -f "$f" ]] || continue
    jq -r '.. | objects | select(has("installPath")) | .installPath
           | select(test("mattermost-ai-agent-bus"))' "$f" 2>/dev/null || true
  done | while read -r d; do
    [[ -f "$d/.claude-plugin/plugin.json" ]] && printf '%s\t%s\n' "$(mm_tree_version "$d")" "$d"
  done | sort -t$'\t' -k1,1V -r | cut -f2
}

# A newer installed plugin than the tree these scripts run from: prints its
# version, else fails. A running session keeps the plugin it started with, so a
# plugin update applies only after a restart (quit, then `claude --continue`).
mm_newer_plugin() {
  local have want newest
  have="$(mm_tree_version "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)")"
  newest="$(mm_installed_plugin_dirs | head -1)"
  [[ -n "$newest" ]] || return 1
  want="$(mm_tree_version "$newest")"
  [[ "$have" != "?" && "$want" != "?" && "$have" != "$want" ]] || return 1
  [[ "$(printf '%s\n%s\n' "$have" "$want" | sort -V | tail -1)" == "$want" ]] || return 1
  printf '%s' "$want"
}

# Version of a mattermost-ai-agent-bus tree (checkout, plugin or host copy).
mm_tree_version() {
  jq -r '.version // "?"' "$1/.claude-plugin/plugin.json" 2>/dev/null || echo "?"
}

# Epoch seconds → RFC 3339 UTC / local HH:MM, on GNU and BSD date alike.
mm_epoch_rfc3339() {
  date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ
}
mm_epoch_hhmm() {
  date -d "@$1" +%H:%M 2>/dev/null || date -r "$1" +%H:%M
}

mm_emit_session_exports() {
  cat <<EOF
export MM_CHAT_URL=$(printf '%q' "${MM_CHAT_URL:-}")
export MM_REG_BOT_NAME=$(printf '%q' "${MM_REG_BOT_NAME:-}")
export MM_BOT_USERNAME=$(printf '%q' "${MM_BOT_USERNAME:-}")
export MM_BOT_USER_ID=$(printf '%q' "${MM_BOT_USER_ID:-}")
export MM_BOT_TOKEN=$(printf '%q' "${MM_BOT_TOKEN:-}")
export MATTERMOST_URL=$(printf '%q' "${MATTERMOST_URL:-${MM_CHAT_URL:-}}")
export MATTERMOST_TOKEN=$(printf '%q' "${MATTERMOST_TOKEN:-${MM_BOT_TOKEN:-}}")
export MM_AGENT_SESSION_FILE=$(printf '%q' "$(mm_session_file)")
EOF
}

mm_write_session_file() {
  local f
  f="$(mm_session_file)" || return 1
  umask 077
  mkdir -p "$(dirname "$f")"
  cat >"$f" <<EOF
# ephemeral session — do not commit
MM_CHAT_URL=${MM_CHAT_URL:-}
MM_REG_BOT_NAME=${MM_REG_BOT_NAME:-}
MM_BOT_USERNAME=${MM_BOT_USERNAME:-}
MM_BOT_USER_ID=${MM_BOT_USER_ID:-}
MM_BOT_TOKEN=${MM_BOT_TOKEN:-}
MATTERMOST_URL=${MATTERMOST_URL:-${MM_CHAT_URL:-}}
MATTERMOST_TOKEN=${MATTERMOST_TOKEN:-${MM_BOT_TOKEN:-}}
MM_TEAM=${MM_TEAM:-}
MM_CHANNEL=${MM_CHANNEL:-}
EOF
}

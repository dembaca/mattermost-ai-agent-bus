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

# Credentials on an inherited fd (arc42 §7.3). The corral host hooks never put
# the bot token or the registration secret into any process environment or
# argv: on macOS a sandbox can read every same-user process's initial
# environment (KERN_PROCARGS2). They start a helper with MM_BOT_TOKEN_FD=3 or
# MM_REG_SECRET_FD=3 and the value on that fd (mm_with_token, mm_with_secret);
# sourcing this file reads it into a shell variable that is not exported, so
# nothing this script starts sees it. Without the *_FD variables (an operator's
# shell: sweep, stats) the environment is used as before. An agent's shell has
# neither: its scripts ask the session's MCP server (mm_bus, RT-6.17).
mm_creds_from_fds() {
  local v fdv fd from_fd=0
  for v in MM_BOT_TOKEN MM_REG_SECRET; do
    fdv="${v}_FD"
    fd="${!fdv:-}"
    [[ -n "$fd" ]] || continue
    unset "$fdv"
    from_fd=1
    [[ "$fd" =~ ^[3-9]$ ]] || continue
    IFS= read -r "${v?}" <&"$fd" || true
    eval "exec ${fd}<&-"
  done
  if [[ "$from_fd" -eq 1 ]]; then
    export -n MM_BOT_TOKEN MATTERMOST_TOKEN MM_REG_SECRET
    MM_CREDS_FROM_FD=1
  fi
  return 0
}
mm_creds_from_fds

# Run a command with the bot token / the registration secret on fd 3 only: the
# value goes through a pipe from the shell's builtin printf, never through an
# environment or an argv (arc42 §7.3). The caller must not export it.
#   mm_with_token "$BIN/mm-agent-status.sh" offline
mm_with_token() { MM_BOT_TOKEN_FD=3 "$@" 3< <(printf '%s\n' "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}"); }
mm_with_secret() { MM_REG_SECRET_FD=3 "$@" 3< <(printf '%s\n' "${MM_REG_SECRET:-}"); }

# What a corral sandbox can write (arc42 §7.1, §7.3): every corral home,
# ~/.claude (the homes share it), the session's workdir and corral's temporary
# directories. A path that is not absolute or has a . or .. component counts as
# one, too: what it names depends on where one stands and on the symlinks on the
# way. Compared without regard to case, as macOS volumes do. The corral hooks
# carry the same check inline (they source nothing before it).
mm_sandbox_can_write() {
  local p="$1/" r real nc=0 rc=1
  [[ "$1" == /* && "$p" != */../* && "$p" != */./* ]] || return 0
  shopt -q nocasematch && nc=1
  shopt -s nocasematch
  case "$p" in /tmp/corral-* | /private/tmp/corral-*) rc=0 ;; esac
  for r in "$HOME/.cache/corral" "$HOME/.claude" "${CORRAL_WORKDIR:-}" "${TMPDIR:-}"; do
    [[ "$rc" -eq 1 && -n "$r" ]] || continue
    [[ "$r" != "${TMPDIR:-}" || "$r" == *corral* ]] || continue
    real="$(cd -P "$r" 2>/dev/null && pwd -P)" || real="$r"
    if [[ "$p" == "${r%/}/"* || "$p" == "${real%/}/"* ]]; then rc=0; fi
  done
  [[ "$nc" -eq 1 ]] || shopt -u nocasematch
  return "$rc"
}

# Host scripts (installer, sweep) look commands up only in absolute PATH
# entries no sandbox can write, and work from /: started in a workdir, a `.` or
# relative entry would find the sandbox's curl (arc42 §7.3).
mm_safe_path() {
  local rest="${PATH:-}:" e out=""
  while [[ -n "$rest" ]]; do
    e="${rest%%:*}" rest="${rest#*:}"
    mm_sandbox_can_write "${e%/}" || out="${out:+$out:}$e"
  done
  PATH="${out:-/usr/bin:/bin:/usr/sbin:/sbin}"
  export PATH
}

# Print a regular file, at most $2 bytes (default 64 KiB), giving up after one
# second: for files a sandbox may have replaced with a FIFO or a device, which
# would block a host hook (arc42 §7.1).
mm_read_file() {
  local f="$1" max="${2:-65536}" pid w rc
  [[ -f "$f" ]] || return 1
  head -c "$max" <"$f" 2>/dev/null &
  pid=$!
  { sleep 1; kill "$pid"; } >/dev/null 2>&1 &
  w=$!
  wait "$pid" 2>/dev/null
  rc=$?
  kill "$w" 2>/dev/null && wait "$w" 2>/dev/null
  return "$rc"
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
# Prints the reason when it says no. Explicit registration (the MCP server's
# session_start) is not affected.
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

# curl with "Authorization: Bearer <token>" read from stdin (-H @-), never in
# its argv, where `ps` shows it to every user of the machine. The other
# arguments are curl's; a body goes in them (-d), stdin is the header's.
#   mm_curl_auth "$tok" -fsS "$url"
mm_curl_auth() {
  local tok="$1"
  shift
  printf 'Authorization: Bearer %s\n' "$tok" | curl -H @- "$@"
}

mm_json_post() {
  local url="$1" tok="$2" body="$3"
  mm_curl_auth "$tok" -fsS -X POST "$url" \
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
  local args=(-sS -w $'\n%{http_code}' -X "$method")
  if [[ -n "$payload" ]]; then
    args+=(-H "Content-Type: application/json" -d "$payload")
  fi
  resp="$(mm_curl_auth "$tok" "${args[@]}" "${chat}${path}" || true)"
  MM_API_STATUS="${resp##*$'\n'}"
  MM_API_BODY="${resp%$'\n'*}"
  [[ "$MM_API_STATUS" =~ ^[0-9]{3}$ ]] || return 1
}

# Print the id of the work channel: the argument (an id or a channel name in
# MM_TEAM) when given, else the project channel, else the default channel.
# Prefers resolved ids from the environment and asks the API only for names.
# shellcheck disable=SC2120  # the argument comes from other scripts (stats)
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

# The registrar's bot username prefix (its BOT_USERNAME_PREFIX, arc42 §7.1):
# MM_AGENT_PREFIX when set, else MM_BOT_USERNAME minus the name it was registered
# with (MM_REG_BOT_NAME), else "agent-". Must match agent_prefix() in
# mcp/.../session_file.py.
mm_agent_prefix() {
  local user="${MM_BOT_USERNAME:-}" name="${MM_REG_BOT_NAME:-}"
  if [[ -n "${MM_AGENT_PREFIX:-}" ]]; then
    printf '%s' "$MM_AGENT_PREFIX"
  elif [[ -n "$name" && "$user" == *"$name" && ${#user} -gt ${#name} ]]; then
    printf '%s' "${user%"$name"}"
  else
    printf 'agent-'
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
      MM_CHAT_URL|MM_REG_BOT_NAME|MM_BOT_USERNAME|MM_AGENT_PREFIX|MM_BOT_USER_ID|MM_BOT_TOKEN|MATTERMOST_URL|MATTERMOST_TOKEN|MM_TEAM|MM_CHANNEL)
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

# The endpoint file of this session's MCP server (RT-6.17 step 2,
# mcp/.../session_file.py keyed_path and workspace(); the keys must match):
#   1. CLAUDE_CODE_SESSION_ID set (Claude Code: server, hooks and every command
#      the agent starts have it): endpoint-sid-<sha1(id)>.env, no fallback — a
#      workspace-keyed file may belong to another session (RT-6.10).
#   2. Else the workspace from CLAUDE_PROJECT_DIR, CURSOR_PROJECT_DIR or
#      MM_WORKSPACE, the first one set, resolved: endpoint-<sha1(dir)>.env of
#      exactly that directory, no walk.
#   3. Else walk up from the real path of $PWD (OpenCode's and Cursor's shells
#      start in the workspace, the agent may cd deeper) and take the closest.
# So a client never reaches the server of another workspace. The prefix names
# the file: endpoint (the hook API's, default) or mcp (the session file).
mm_bus_file() {
  local prefix="${1:-endpoint}" dir state key f sid v
  state="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
  sid="${CLAUDE_CODE_SESSION_ID:-}"
  sid="${sid//[[:space:]]/}"
  if [[ -n "$sid" ]]; then
    key="$(printf '%s' "$sid" | { sha1sum 2>/dev/null || shasum; } | cut -c1-16)"
    f="${state}/${prefix}-sid-${key}.env"
    [[ -n "$key" && -f "$f" ]] || return 1
    printf '%s' "$f"
    return 0
  fi
  for v in CLAUDE_PROJECT_DIR CURSOR_PROJECT_DIR MM_WORKSPACE; do
    [[ -n "${!v:-}" ]] || continue
    dir="$(cd "${!v}" 2>/dev/null && pwd -P)" || return 1
    key="$(printf '%s' "$dir" | { sha1sum 2>/dev/null || shasum; } | cut -c1-16)"
    f="${state}/${prefix}-${key}.env"
    [[ -n "$key" && -f "$f" ]] || return 1
    printf '%s' "$f"
    return 0
  done
  dir="$(pwd -P 2>/dev/null)" || return 1
  while :; do
    key="$(printf '%s' "$dir" | { sha1sum 2>/dev/null || shasum; } | cut -c1-16)"
    f="${state}/${prefix}-${key}.env"
    if [[ -n "$key" && -f "$f" ]]; then
      printf '%s' "$f"
      return 0
    fi
    [[ "$dir" == "/" ]] && return 1
    dir="$(dirname "$dir")"
  done
}

# For hooks: read the hook's JSON input once into MM_HOOK_INPUT and take the
# session id from it when the environment has none, so the endpoint lookup
# (mm_bus_file) keys on this session even then.
mm_read_hook_input() {
  MM_HOOK_INPUT="$(cat 2>/dev/null || true)"
  if [[ -z "${CLAUDE_CODE_SESSION_ID:-}" ]] && command -v jq >/dev/null 2>&1; then
    local sid
    sid="$(jq -r '.session_id // empty' <<<"$MM_HOOK_INPUT" 2>/dev/null || true)"
    [[ -n "$sid" ]] && export CLAUDE_CODE_SESSION_ID="$sid"
  fi
  return 0
}

# The MCP server's hook API (RT-6.17, mcp/.../hookapi.py): the turn hooks and
# the scripts an agent runs (listen, post, poll, status) ask it instead of
# Mattermost and hold no token.
#   mm_bus GET /v1/me                       → MM_BUS_STATUS, MM_BUS_BODY
#   mm_bus POST /v1/presence '{"state":"idle"}'
# Endpoint and key come from MM_BUS_ENDPOINT / MM_BUS_KEY (a corral session),
# else from the file the server writes (mm_bus_file), read on every call: a
# restarted server has a new port and key. MM_BUS_FROM says which (env, the
# file, or empty: none found). The key goes to curl on stdin, never in its
# argv. Succeeds only on HTTP 200; no endpoint, no server or a timeout fail,
# and a hook fails open. Each call takes at most MM_BUS_MAX_TIME (whole
# seconds, default 3), and none runs past MM_BUS_DEADLINE (epoch seconds)
# when a hook set one for its whole run.
mm_bus() {
  local method="$1" path="$2" payload="${3:-}" ep="${MM_BUS_ENDPOINT:-}" key="${MM_BUS_KEY:-}" f k v resp
  MM_BUS_STATUS="" MM_BUS_BODY="" MM_BUS_FROM=env
  if [[ -z "$ep" || -z "$key" ]]; then
    MM_BUS_FROM=""
    f="$(mm_bus_file)" || return 1
    MM_BUS_FROM="$f"
    while IFS='=' read -r k v; do
      case "$k" in MM_BUS_ENDPOINT) ep="$v" ;; MM_BUS_KEY) key="$v" ;; esac
    done <"$f"
    [[ -n "$ep" && -n "$key" ]] || return 1
  fi
  local max="${MM_BUS_MAX_TIME:-3}" left
  [[ "$max" =~ ^[1-9][0-9]*$ ]] || max=3
  if [[ "${MM_BUS_DEADLINE:-}" =~ ^[0-9]+$ ]]; then
    left=$((MM_BUS_DEADLINE - $(date +%s)))
    (( left > 0 )) || return 1
    (( left < max )) && max="$left"
  fi
  local args=(-sS --max-time "$max" -w $'\n%{http_code}' -X "$method")
  if [[ -n "$payload" ]]; then
    args+=(-H "Content-Type: application/json" --data-raw "$payload")
  fi
  resp="$(mm_curl_auth "$key" "${args[@]}" "${ep%/}${path}" 2>/dev/null)" || return 1
  MM_BUS_STATUS="${resp##*$'\n'}"
  MM_BUS_BODY="${resp%$'\n'*}"
  [[ "$MM_BUS_STATUS" == 200 ]]
}

# Why the last mm_bus call failed, for a person or agent reading stderr. The
# scripts an agent runs by hand have no other way onto the bus: no token.
mm_bus_explain() {
  local how="start the MCP server of this session: Claude Code, Cursor and OpenCode start it from their MCP config (adapters/, arc42 §7.2); the shell finds it by the session id, CLAUDE_PROJECT_DIR / CURSOR_PROJECT_DIR / MM_WORKSPACE, else from the workspace directory (RT-6.17)"
  if [[ -z "${MM_BUS_FROM:-}" ]]; then
    echo "mm-bus: no MCP server of this session found — ${how}" >&2
  elif [[ -z "${MM_BUS_STATUS:-}" || "${MM_BUS_STATUS}" == 000 ]]; then
    echo "mm-bus: the MCP server of this session does not answer (${MM_BUS_FROM}) — ${how}" >&2
  elif [[ "$MM_BUS_STATUS" == 401 ]]; then
    echo "mm-bus: the MCP server refused the key (${MM_BUS_FROM}): a stale file or another server — restart the MCP server of this session" >&2
  elif [[ "$MM_BUS_STATUS" == 503 ]]; then
    echo "mm-bus: no bot yet: register with the MCP tool session_start first" >&2
  else
    echo "mm-bus: the MCP server answered HTTP ${MM_BUS_STATUS}: $(jq -r '.error // empty' <<<"${MM_BUS_BODY:-}" 2>/dev/null)" >&2
  fi
}

# A keyless corral session (RT-6.18): the MCP server runs on the host, the
# sandbox has its endpoints and no token. The server's state files are not
# here; what the hooks need of them comes through the hook API.
mm_bus_keyless() {
  [[ -n "${MM_BUS_MCP_ENDPOINT:-}" ]]
}

# This session's bot id without asking anyone: MM_BOT_USER_ID from the
# environment, else the one the MCP server put into its endpoint file. For the
# local steps that must not wait on the server (the prompt hook stopping the
# listener and clearing the turn state, RT-6.17 error handling). Exports it.
mm_bus_uid() {
  local uid="${MM_BOT_USER_ID:-}" f
  if ! [[ "$uid" =~ ^[a-z0-9]{26}$ ]]; then
    f="$(mm_bus_file)" || return 1
    uid="$(sed -n 's/^MM_BOT_USER_ID=//p' "$f" 2>/dev/null | head -1)"
    [[ "$uid" =~ ^[a-z0-9]{26}$ ]] || return 1
  fi
  export MM_BOT_USER_ID="$uid"
}

# Who this session's bot is, from the hook API: exports MM_BOT_USER_ID and
# MM_BOT_USERNAME (the per-bot state files key on them), sets MM_BUS_PROJECT
# (1 in a project channel), MM_CROWN_UNTIL (epoch, 0 without the 👑) and
# MM_BUS_CHANNEL (1 once the agent confirmed the channel check, RT-6.12). Fails
# without jq or a bus session: the hook then exits 0 and stays quiet (RT-6.4).
# shellcheck disable=SC2034  # MM_BUS_PROJECT, MM_CROWN_UNTIL are read by the callers
mm_bus_me() {
  local uid name project crown channel
  command -v jq >/dev/null 2>&1 || return 1
  mm_bus GET /v1/me || return 1
  { read -r uid; read -r name; read -r project; read -r crown; read -r channel; } < <(jq -r '
    (.user_id // ""), (.username // ""), (if .project then 1 else 0 end), (.crown_until // 0),
    (if .channel then 1 else 0 end)' <<<"$MM_BUS_BODY" 2>/dev/null)
  [[ "${uid:-}" =~ ^[a-z0-9]{26}$ ]] || return 1
  export MM_BOT_USER_ID="$uid" MM_BOT_USERNAME="${name:-}"
  MM_BUS_PROJECT="${project:-0}"
  MM_BUS_CHANNEL="${channel:-0}"
  MM_CROWN_UNTIL="${crown:-0}"
  [[ "$MM_CROWN_UNTIL" =~ ^[0-9]+$ ]] || MM_CROWN_UNTIL=0
  return 0
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

# Local state of the presence lease (RT-6.6, kept by the MCP server's lease.py),
# keyed on the bot like the turn state: presence-<uid>.env (last state and
# label reported) and coord-<uid>.env (crown lease, pending take, yield after a
# release).
mm_bot_state_file() {
  local kind="$1" uid="${2:-${MM_BOT_USER_ID:-}}"
  [[ "$uid" =~ ^[a-z0-9]{26}$ ]] || return 1
  printf '%s' "$(mm_state_dir)/${kind}-${uid}.env"
}

# Remember the bot's latest post in the work channel, for the Stop hook and the
# MCP server's exit handler (RT-6.2; mcp/.../ownpost.py writes the same file):
# mm_record_own_post <channel_id> <post JSON as the API returned it>. A post in
# another channel leaves it as it was; when the work channel cannot be told, the
# file goes, and the hook asks the channel as before. Never fails.
mm_record_own_post() {
  local cid="$1" post="$2" f work uid id root at msg hello=""
  uid="$(jq -r '.user_id // empty' <<<"$post" 2>/dev/null)" || return 0
  f="$(mm_bot_state_file last "$uid")" || return 0
  work="$(mm_work_channel_id 2>/dev/null)" || { rm -f "$f"; return 0; }
  [[ "$cid" == "$work" ]] || return 0
  {
    read -r id; read -r root; read -r at; read -r msg
  } < <(jq -r '.id, (.root_id // ""), (.create_at // 0),
               (.message | ltrimstr("👑 ") | gsub("\\s+"; " ") | .[0:80])' <<<"$post" 2>/dev/null)
  [[ -n "$id" ]] || return 0
  if [[ -z "$root" && "$msg" =~ ^[^A-Za-z0-9@#]*HELLO([^A-Za-z]|$) ]]; then
    hello="$id"
  elif [[ "$(sed -n 's/^CHANNEL=//p' "$f" 2>/dev/null)" == "$cid" ]]; then
    hello="$(sed -n 's/^HELLO=//p' "$f" 2>/dev/null)"
  fi
  mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
  printf 'CHANNEL=%s\nID=%s\nAT=%s\nMSG=%s\nHELLO=%s\n' "$cid" "$id" "$at" "$msg" "$hello" >"$f" 2>/dev/null || true
}

# How long a custom status lives without renewal (MM_LEASE_SEC, default 900 s).
mm_lease_sec() {
  local s="${MM_LEASE_SEC:-900}"
  [[ "$s" =~ ^[0-9]+$ && "$s" -gt 0 ]] || s=900
  printf '%s' "$s"
}

# Overhead counters (RT-6.9): one JSON line per event in stats-<uid>.jsonl,
#   mm_stat KIND [key=value …]     e.g. mm_stat wake reason=digest
# Counted by hooks, listener and MCP server, posted once at session end to
# MM_STATS_CHANNEL (default agents-stats); MM_STATS_CHANNEL=off counts nothing.
# In a keyless corral session the server's state dir is on the host (RT-6.18):
# the counter goes to its hook API instead (POST /v1/stat, at most 1 s).
# Never fails, never prints: a counter must not disturb a session.
mm_stats_off() { [[ "${MM_STATS_CHANNEL:-agents-stats}" =~ ^(off|none|0|false)$ ]]; }
mm_stat() {
  local kind="$1" f kv
  shift || true
  mm_stats_off && return 0
  [[ "${MM_BOT_USER_ID:-}" =~ ^[a-z0-9]{26}$ ]] || return 0
  f="$(mm_state_dir)/stats-${MM_BOT_USER_ID}.jsonl"
  command -v jq >/dev/null 2>&1 || return 0
  kv="$(printf '%s\n' "$@" | jq -cRn --arg k "$kind" --argjson t "$(date +%s)" '
    [inputs | select(contains("=")) | capture("^(?<k>[^=]+)=(?<v>.*)$")
     | {(.k): (.v | tonumber? // .)}] | add // {} | {k: $k, t: $t} + .' 2>/dev/null)" || return 0
  if mm_bus_keyless; then
    MM_BUS_MAX_TIME=1 mm_bus POST /v1/stat "$kv" >/dev/null 2>&1 || true
    return 0
  fi
  mkdir -p "$(dirname "$f")" 2>/dev/null || return 0
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

# After a registration (RT-6.8): does the profile the registrar wrote carry every
# trusted name sent? It may write only part of the list (it caps it, and keeps
# human accounts only), while the bus trusts all of it. Reads the description
# from the bot's own user object (bot_description), else /bots/<id>; when
# neither is readable there is nothing to compare and it stays silent. Warns on
# stderr, never fails. Same as mcp/.../profile.py.
# mm_warn_profile_trusted <bot user id> <bot username> <name>...
mm_warn_profile_trusted() {
  local uid="$1" bot="$2" n desc="" got=0 listed missing=""
  shift 2
  [[ $# -gt 0 ]] || return 0
  if mm_api GET "/api/v4/users/${uid}" && [[ "$MM_API_STATUS" == "200" ]] &&
    jq -e 'has("bot_description")' >/dev/null 2>&1 <<<"$MM_API_BODY"; then
    desc="$(jq -r '.bot_description // ""' <<<"$MM_API_BODY")" got=1
  elif mm_api GET "/api/v4/bots/${uid}" && [[ "$MM_API_STATUS" == "200" ]] &&
    jq -e 'has("description")' >/dev/null 2>&1 <<<"$MM_API_BODY"; then
    desc="$(jq -r '.description // ""' <<<"$MM_API_BODY")" got=1
  fi
  [[ "$got" -eq 1 ]] || return 0
  listed="$(sed -n 's/.*trusts @\([^·]*\).*/\1/p' <<<"$desc" | tr 'A-Z ' 'a-z,' | sed 's/,@/,/g')"
  for n in "$@"; do
    [[ ",${listed//,,/,}," == *",$(tr 'A-Z' 'a-z' <<<"$n"),"* ]] || missing+="${missing:+, }$n"
  done
  [[ -z "$missing" ]] ||
    echo "mm-bus: WARNING: the profile of @${bot} lacks some of the $# trusted names (${missing}), but the bus trusts all of them: the registrar likely writes only part of the list (its maxTrusted), human accounts only" >&2
  return 0
}

# Who answers for the agent: MM_RESPONSIBLE_HUMAN, else the first trusted human.
mm_responsible_name() {
  local r="${MM_RESPONSIBLE_HUMAN:-}"
  r="${r//[[:space:]]/}"; r="${r#@}"
  [[ -n "$r" ]] || r="$(mm_trusted_names | head -1)"
  printf '%s' "$r"
}

# Newest version of the mattermost-ai-agent-bus Claude Code plugin installed on
# this machine: the host's own Claude config and every corral private home.
# For a version notice only (arc42 §7.1): a sandbox can write the corral homes,
# and by default ~/.claude too, so nothing read here may decide which code runs
# on the host. Hence a bare version (digits and dots, an optional suffix;
# anything else is dropped), never an install path.
# Every read is of a regular file, bounded in size and time (mm_read_file), and
# all of them together stop after a few seconds: a FIFO or a long list there
# must not stall a session's start.
mm_newest_plugin_version() {
  local f d v deadline=$((SECONDS + 5))
  for f in "$HOME/.claude/plugins/installed_plugins.json" \
           "$HOME"/.cache/corral/home-*/.claude/plugins/installed_plugins.json; do
    (( SECONDS < deadline )) || break
    mm_read_file "$f" 1048576 | jq -r '.. | objects | select(has("installPath")) | .installPath
           | select(type == "string" and test("mattermost-ai-agent-bus"))' 2>/dev/null || true
  done | head -n 64 | while read -r d; do
    (( SECONDS < deadline )) || break
    v="$(mm_tree_version "$d")"
    if [[ "$v" =~ ^[0-9]+(\.[0-9]+)*([-+][0-9A-Za-z.]+)?$ ]]; then printf '%s\n' "$v"; fi
  done | sort -V -r | sed -n 1p | grep .
}

# Is version $2 newer than version $1?
mm_version_newer() {
  [[ "$1" != "?" && "$2" != "?" && "$1" != "$2" ]] || return 1
  [[ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | tail -1)" == "$2" ]]
}

# A newer installed plugin than the tree these scripts run from: prints its
# version, else fails. A running session keeps the plugin it started with, so a
# plugin update applies only after a restart (quit, then `claude --continue`).
mm_newer_plugin() {
  local have want
  have="$(mm_tree_version "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)")"
  want="$(mm_newest_plugin_version)" || return 1
  mm_version_newer "$have" "$want" || return 1
  printf '%s' "$want"
}

# Version of a mattermost-ai-agent-bus tree (checkout, plugin or host copy).
mm_tree_version() {
  local v
  v="$(mm_read_file "$1/.claude-plugin/plugin.json" | jq -r '.version // "?"' 2>/dev/null)" || v=""
  printf '%s\n' "${v:-?}"
}

# Epoch seconds → RFC 3339 UTC / local HH:MM, on GNU and BSD date alike.
mm_epoch_rfc3339() {
  date -u -d "@$1" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -r "$1" +%Y-%m-%dT%H:%M:%SZ
}
mm_epoch_hhmm() {
  date -d "@$1" +%H:%M 2>/dev/null || date -r "$1" +%H:%M
}

# Claude Code was started with the MCP server as a channel (RT-6.12): the
# server pushes bus messages into the session, so no listener is needed. The
# server finds that out itself; it counts once the agent confirmed the server's
# check (channel-<uid>.env). MM_CHANNEL_PUSH=0 turns it off. In a keyless
# session that file is on the host: /v1/me says (MM_BUS_CHANNEL, RT-6.18).
mm_channel_push() {
  case "$(printf '%s' "${MM_CHANNEL_PUSH:-}" | tr '[:upper:]' '[:lower:]')" in
    0 | false | no | off) return 1 ;;
  esac
  local f
  if mm_bus_keyless; then
    [[ -n "${MM_BUS_CHANNEL:-}" ]] || mm_bus_me || return 1
    [[ "${MM_BUS_CHANNEL:-0}" == 1 ]]
    return
  fi
  f="$(mm_bot_state_file channel "${1:-}")" && [[ -f "$f" ]]
}

# Lines for eval. With credentials from an fd (the corral host hooks) the token
# lines are plain assignments: the caller keeps the token in a variable that
# no process it starts inherits (arc42 §7.3). For the shell CLI they export.
mm_emit_session_exports() {
  local x=export
  [[ "${MM_CREDS_FROM_FD:-0}" != 1 ]] || x=""
  cat <<EOF
export MM_CHAT_URL=$(printf '%q' "${MM_CHAT_URL:-}")
export MM_REG_BOT_NAME=$(printf '%q' "${MM_REG_BOT_NAME:-}")
export MM_BOT_USERNAME=$(printf '%q' "${MM_BOT_USERNAME:-}")
export MM_AGENT_PREFIX=$(printf '%q' "$(mm_agent_prefix)")
export MM_BOT_USER_ID=$(printf '%q' "${MM_BOT_USER_ID:-}")
${x:+$x }MM_BOT_TOKEN=$(printf '%q' "${MM_BOT_TOKEN:-}")
export MATTERMOST_URL=$(printf '%q' "${MATTERMOST_URL:-${MM_CHAT_URL:-}}")
${x:+$x }MATTERMOST_TOKEN=$(printf '%q' "${MATTERMOST_TOKEN:-${MM_BOT_TOKEN:-}}")
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
MM_AGENT_PREFIX=$(mm_agent_prefix)
MM_BOT_USER_ID=${MM_BOT_USER_ID:-}
MM_BOT_TOKEN=${MM_BOT_TOKEN:-}
MATTERMOST_URL=${MATTERMOST_URL:-${MM_CHAT_URL:-}}
MATTERMOST_TOKEN=${MATTERMOST_TOKEN:-${MM_BOT_TOKEN:-}}
MM_TEAM=${MM_TEAM:-}
MM_CHANNEL=${MM_CHANNEL:-}
EOF
}

#!/usr/bin/env bash
# Shared helpers for mm-agent-*.sh (source this file; do not exec).
# shellcheck shell=bash

# shellcheck source=mm-agent-symbols.sh
# Pure bash: the hardened callers run it with no command from the workdir (dirname, cd).
source "${BASH_SOURCE[0]%/*}/mm-agent-symbols.sh"

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

# Unregister bot NAME as the corral host hooks do: with its own token first,
# which the registrar always accepts for it, then with the registration secret;
# the helper's output is dropped. On failure MM_UNREG_WHY says why, for a log:
# "token: <reason>; secret: <reason>", each the helper's stderr on one line,
# printable, cut to 160, with the token and the secret blanked.
# MM_UNREG_TOKEN_HTTP is the registrar's status for the token attempt ("" for
# none or no answer).
#   mm_unregister_bot "$NAME" || log "could not unregister ${NAME}: ${MM_UNREG_WHY}"
mm_unregister_bot() {
  local name="$1" tok="no token" sec="no secret"
  MM_UNREG_WHY="" MM_UNREG_TOKEN_HTTP=""
  if [[ -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]]; then
    mm_unregister_with mm_with_token "$name" && return 0
    tok="$MM_UNREG_WHY" MM_UNREG_TOKEN_HTTP="$MM_UNREG_HTTP"
  fi
  if [[ -n "${MM_REG_SECRET:-}" ]]; then
    mm_unregister_with mm_with_secret "$name" && return 0
    sec="$MM_UNREG_WHY"
  fi
  MM_UNREG_WHY="token: ${tok}; secret: ${sec}"
  return 1
}
# As mm_unregister_bot, for a bot whose MCP server may have unregistered it
# first (its exit work, RT-6.18 step 7): returns 0 unregistered, 2 gone already
# (mm_token_gone), 1 failed (MM_UNREG_WHY says why).
#   mm_unregister_or_gone "$NAME" || rc=$?
mm_unregister_or_gone() {
  mm_unregister_bot "$1" && return 0
  if mm_token_gone "$MM_UNREG_TOKEN_HTTP"; then return 2; fi
  return 1
}
# The bot of this shell's token is gone already (RT-6.1 step 7): the
# registrar refused that token (HTTP status $1: 401, or 404) and Mattermost
# does too (401 on /users/me). A registrar that fails (5xx) or does not answer,
# and any other answer of Mattermost, prove nothing. mm_api runs in a
# subshell, so MM_API_* stay as they were.
mm_token_gone() {
  [[ "$1" == 401 || "$1" == 404 ]] || return 1
  [[ -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]] || return 1
  [[ "$( (mm_api GET "/api/v4/users/me" >/dev/null 2>&1 || exit 0; printf '%s' "$MM_API_STATUS") )" == 401 ]]
}
# One unregister through the helper; on failure MM_UNREG_WHY (as above) and
# MM_UNREG_HTTP, the status the helper reports ("" for none).
mm_unregister_with() {
  local out v
  MM_UNREG_WHY="" MM_UNREG_HTTP=""
  out="$("$1" "${BASH_SOURCE[0]%/*}/mm-agent-unregister.sh" "$2" 2>&1 >/dev/null)" && return 0
  if [[ "$out" =~ ^unregister\ failed\ http=([0-9]{3}) ]]; then MM_UNREG_HTTP="${BASH_REMATCH[1]}"; fi
  for v in "${MM_BOT_TOKEN:-}" "${MATTERMOST_TOKEN:-}" "${MM_REG_SECRET:-}"; do
    [[ -z "$v" ]] || out="${out//"$v"/[hidden]}"
  done
  out="$(printf '%s' "$out" | tr '\n\t' '  ' | tr -cd '[:print:]' | cut -c1-160)" || out=""
  MM_UNREG_WHY="${out:-no reason given}"
  return 1
}

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

# MM_PROJECT_CHANNEL as a list (ADR-002, RT-6.22), one name per line: the
# comma-separated entries without blanks or a leading ~, empty and repeated
# ones dropped, as client.channel_list reads them. The first is the home, the
# others are guest channels. Splits on ASCII only, so any locale (CI's C too)
# reads the same names.
mm_project_channels() {
  local raw="${MM_PROJECT_CHANNEL:-}" entry seen=","
  local -a parts=()
  [[ -n "$raw" ]] || return 0
  IFS=, read -r -a parts <<<"$raw"
  for entry in "${parts[@]+"${parts[@]}"}"; do
    entry="${entry//[[:space:]]/}"
    entry="${entry#\~}"
    [[ -n "$entry" && "$seen" != *",${entry},"* ]] || continue
    seen+="${entry},"
    printf '%s\n' "$entry"
  done
}

# The agent's home (ADR-002): the first MM_PROJECT_CHANNEL entry, else MM_CHANNEL.
mm_home_channel() {
  local first
  first="$(mm_project_channels | head -n 1)"
  printf '%s' "${first:-$(mm_channel)}"
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

# POST a JSON body with the bearer token; a success answer goes to stdout.
# On an HTTP error (>= 400) nothing goes to stdout: stderr gets one line,
# "<label>: HTTP <code>: <reason>" (label: the 4th argument), and the return
# code is 22 as curl's -f has it. The reason is the registrar's own plain text
# or its JSON .message/.error, printable characters, cut to 200. A success body
# is never printed or kept (it holds a bot token, arc42 §7.3); an error body
# only when it does not hold the bearer token (a proxy echoing headers).
#   RESP="$(mm_json_post "$url" "$secret" "$json" "register failed")"
mm_json_post() {
  local url="$1" tok="$2" body="$3" label="${4:-request failed}"
  local resp code out why
  resp="$(mm_curl_auth "$tok" -sS -w $'\n%{http_code}' -X POST "$url" \
    -H "Content-Type: application/json" \
    -d "$body")" || return $?
  code="${resp##*$'\n'}"
  out="${resp%$'\n'*}"
  [[ "$code" =~ ^[0-9]{3}$ ]] || { echo "$label: no HTTP status in the answer" >&2; return 1; }
  if [[ "$code" -lt 400 ]]; then
    printf '%s' "$out"
    return 0
  fi
  why="$(printf '%s' "$out" | jq -r '(.message // .error // empty) | tostring' 2>/dev/null || true)"
  [[ -n "$why" ]] || why="$out"
  if [[ -n "$tok" && ( "$out" == *"$tok"* || "$why" == *"$tok"* ) ]]; then
    echo "$label: HTTP $code (the answer is not shown: it holds the secret)" >&2
    return 22
  fi
  why="$(printf '%s' "$why" | head -c 200 | tr -cd '[:print:]')" || why=""
  echo "$label: HTTP $code${why:+: $why}" >&2
  return 22
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
# MM_TEAM) when given, else the home (the first MM_PROJECT_CHANNEL entry,
# RT-6.22), else the default channel.
# Prefers resolved ids from the environment and asks the API only for names.
# shellcheck disable=SC2120  # the argument comes from other scripts (stats)
mm_work_channel_id() {
  local arg="${1:-}" id="" name="" team_id
  if [[ -n "$arg" ]]; then
    # Mattermost ids are 26 lowercase alphanumerics; anything else is a name.
    if [[ "$arg" =~ ^[a-z0-9]{26}$ ]]; then id="$arg"; else name="$arg"; fi
  elif [[ -n "${MM_PROJECT_CHANNEL_ID:-}" ]]; then
    id="$MM_PROJECT_CHANNEL_ID"
  elif [[ -n "$(mm_project_channels)" ]]; then
    name="$(mm_home_channel)"
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
      MM_CHAT_URL|MM_REG_BOT_NAME|MM_BOT_USERNAME|MM_AGENT_PREFIX|MM_BOT_USER_ID|MM_BOT_ATTESTATION|MM_BOT_TOKEN|MATTERMOST_URL|MATTERMOST_TOKEN|MM_TEAM|MM_CHANNEL|MM_PROJECT_CHANNEL)
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
#      start in the workspace, the agent may cd deeper) and take the closest,
#      but never above the nearest git top (a directory with a .git directory
#      or file: a checkout or worktree): a repository nested in another
#      workspace, or a worktree under a checkout, is its own workspace.
# A workspace named by a variable is taken exactly. The walk can still reach an
# outer workspace's server from a directory that is in no repository of its
# own below it (RT-6.17 step 2). The file is the hook API's endpoint file.
mm_bus_file() {
  local prefix=endpoint dir state key f sid v
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
    [[ "$dir" == "/" || -e "$dir/.git" ]] && return 1
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
# (1 in a project channel), MM_CROWN_UNTIL (epoch, 0 without the crown) and
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
  } < <(jq -r --arg crown "$MM_CROWN " '.id, (.root_id // ""), (.create_at // 0),
               (.message | ltrimstr($crown) | gsub("\\s+"; " ") | .[0:80])' <<<"$post" 2>/dev/null)
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
# stderr, never fails. Same as mcp/.../profile.py. Also warns when the bot is
# unbound (registered with the shared secret): no owner split (RT-6.16 step 1).
# mm_warn_profile_trusted <bot user id> <bot username> <name>...
mm_warn_profile_trusted() {
  local uid="$1" bot="$2" n desc="" got=0 listed missing=""
  shift 2
  if mm_api GET "/api/v4/users/${uid}" && [[ "$MM_API_STATUS" == "200" ]] &&
    jq -e 'has("bot_description")' >/dev/null 2>&1 <<<"$MM_API_BODY"; then
    desc="$(jq -r '.bot_description // ""' <<<"$MM_API_BODY")" got=1
  elif mm_api GET "/api/v4/bots/${uid}" && [[ "$MM_API_STATUS" == "200" ]] &&
    jq -e 'has("description")' >/dev/null 2>&1 <<<"$MM_API_BODY"; then
    desc="$(jq -r '.description // ""' <<<"$MM_API_BODY")" got=1
  fi
  [[ "$got" -eq 1 ]] || return 0
  case " · ${desc} · " in
    *" · unbound · "*)
      echo "mm-bus: WARNING: @${bot} is unbound (registered with the shared secret): no owner split, and other owners' agents treat it as foreign; for a bound bot put an enrollment token (/agent-token create) into MM_REG_SECRET" >&2 ;;
  esac
  [[ $# -gt 0 ]] || return 0
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
# plugin update applies only after a restart (quit, then `claude --resume <id>`).
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

# What kind of host copy a release tree is (ADR-011, RT-6.29): "signed release
# v<x.y.z>" when the installer marked it SOURCE=signed after verifying its tag,
# "release v<x.y.z> (signature not checked)" for SOURCE=release (installed
# without MM_RELEASE_SIGNERS), else "unsigned" (from a clone, or from before
# 0.81.0). For notices only.
mm_release_kind() {
  local s tag
  s="$(mm_read_file "$1/SOURCE" 4096)" || s=""
  tag="$(sed -n 's/^TAG=//p' <<<"$s" | head -n1)"
  if [[ ! "$tag" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    printf 'unsigned\n'
  elif [[ $'\n'"$s"$'\n' == *$'\n'SOURCE=signed$'\n'* ]]; then
    printf 'signed release %s\n' "$tag"
  elif [[ $'\n'"$s"$'\n' == *$'\n'SOURCE=release$'\n'* ]]; then
    printf 'release %s (signature not checked)\n' "$tag"
  else
    printf 'unsigned\n'
  fi
}

# --- release tags (ADR-011, RT-6.29, RT-6.30) -----------------------------------
# Shared by the installer (--release) and the release check: the pinned keys,
# the repository, the tools, git's clean environment and the checks of a tag.
# On a failure each sets MM_REL_ERR (a message that echoes no argument) and
# returns 1.

# The pinned keys (decisions (3) and (4) of 2026-11-12) → MM_REL_ENFORCE (1
# with keys: the check is opt-in), MM_REL_ALLOWED, an allowed-signers file's
# text (Ed25519 keys under one fixed principal, mm-release, namespaces="git"),
# MM_REL_KEYS (one `<type> <base64>` per line, the keys in use), MM_REL_PINNED
# (the same of release-signers, "" without it), MM_REL_FROM (env, file or
# none) and MM_REL_FROM_TEXT (MM_RELEASE_SIGNERS or the file's path).
# $1 is the host copy's layout directory (MM_BUS_HOST_DIR's parent, beside
# releases/ and src.git), never the state directory: XDG_STATE_HOME comes from
# the operator's environment, which a workdir's .envrc can redirect. The
# source: MM_RELEASE_SIGNERS when it is set, else $1/release-signers, which
# only a --release run with keys writes, else none. Set but holding no key is a
# mistake, not opt-out. The file, when it exists, is checked in every mode: a
# symlink, no regular file, in a directory a sandbox can write or that is not
# the user's own or others can write, not the user's own or writable by
# others, too large, without a key or with anything but keys is refused, never
# skipped. MM_RELEASE_SIGNERS with keys other than the pinned ones (rotation
# included) is refused unless MM_REL_REPLACE=1 (--replace-signers).
# shellcheck disable=SC2034 # read by the installer and the release check
MM_SIGNERS_FILE=release-signers MM_SIGNERS_MAX=16384 MM_WITHDRAWN_FILE=release-withdrawn.env
# Key lines of $2 → MM_REL_PARSED (`<type> <base64>` per line); $1 names the source.
mm_release_parse_keys() {
  local line
  MM_REL_PARSED=""
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -n "$line" && "$line" != \#* ]] || continue
    if [[ ! "$line" =~ ^(ssh-ed25519|sk-ssh-ed25519@openssh\.com)[[:space:]]+([A-Za-z0-9+/]+={0,2})([[:space:]].*)?$ ]]; then
      MM_REL_ERR="$1 has a line that is no Ed25519 public key (ssh-ed25519 AAAA…); refused"
      return 1
    fi
    MM_REL_PARSED+="${BASH_REMATCH[1]} ${BASH_REMATCH[2]}"$'\n'
  done <<<"$2"
}
# Whether $1 (a resolved path) is this user's own and writable by no one else.
mm_own_path() {
  [[ -O "$1" && -z "$(find "$1" -maxdepth 0 \( -perm -020 -o -perm -002 \) 2>/dev/null)" ]]
}
# shellcheck disable=SC2034 # MM_REL_* are read by the caller
mm_release_signers() {
  local dir="${1:-}" f real text="" k
  MM_REL_ENFORCE=0 MM_REL_ALLOWED="" MM_REL_KEYS="" MM_REL_PINNED="" MM_REL_ERR="" MM_REL_FROM=none MM_REL_FROM_TEXT=""
  f="${dir%/}/$MM_SIGNERS_FILE"
  if [[ -n "$dir" && ( -e "$f" || -L "$f" ) ]]; then
    real="$(cd -P "$dir" 2>/dev/null && pwd -P)" || real=""
    if [[ -L "$f" ]]; then
      MM_REL_ERR="$f is a symlink; refused (look at it, then remove it)"
    elif [[ ! -f "$f" ]]; then
      MM_REL_ERR="$f is no regular file; refused"
    elif [[ -z "$real" ]] || mm_sandbox_can_write "$dir" || mm_sandbox_can_write "$real"; then
      MM_REL_ERR="$f lies where a corral sandbox can write; refused"
    elif ! mm_own_path "$real"; then
      MM_REL_ERR="$real, the directory of $MM_SIGNERS_FILE, is not this user's own or others can write it; refused"
    elif ! mm_own_path "$f"; then
      MM_REL_ERR="$f is not this user's own or others can write it; refused"
    elif (( $(wc -c <"$f") > MM_SIGNERS_MAX )) || ! text="$(mm_read_file "$f" "$MM_SIGNERS_MAX")"; then
      MM_REL_ERR="$f is larger than $MM_SIGNERS_MAX bytes or cannot be read; refused"
    fi
    [[ -z "$MM_REL_ERR" ]] || return 1
    mm_release_parse_keys "$f" "$text" || return 1
    MM_REL_PINNED="$MM_REL_PARSED"
    if [[ -z "$MM_REL_PINNED" ]]; then
      MM_REL_ERR="$f holds no public key; refused (remove it with mm-agent-host-install.sh --forget-signers)"
      return 1
    fi
  fi
  if [[ -n "${MM_RELEASE_SIGNERS+set}" ]]; then
    MM_REL_FROM=env MM_REL_FROM_TEXT=MM_RELEASE_SIGNERS
    mm_release_parse_keys MM_RELEASE_SIGNERS "${MM_RELEASE_SIGNERS//;/$'\n'}" || return 1
    MM_REL_KEYS="$MM_REL_PARSED"
    if [[ -z "$MM_REL_KEYS" ]]; then
      MM_REL_ERR="MM_RELEASE_SIGNERS is set but holds no public key (unset it to install without a signature check); nothing changed"
      return 1
    fi
    if [[ -n "$MM_REL_PINNED" && "${MM_REL_REPLACE:-0}" -ne 1 &&
          "$(sort -u <<<"$MM_REL_KEYS")" != "$(sort -u <<<"$MM_REL_PINNED")" ]]; then
      MM_REL_ERR="the keys pinned in $f differ from MM_RELEASE_SIGNERS: mm-agent-host-install.sh --forget-signers first, or --release with --replace-signers; nothing changed"
      return 1
    fi
  elif [[ -n "$MM_REL_PINNED" ]]; then
    MM_REL_FROM=file MM_REL_FROM_TEXT="$f" MM_REL_KEYS="$MM_REL_PINNED"
  else
    return 0
  fi
  MM_REL_ENFORCE=1
  while IFS= read -r k; do
    [[ -z "$k" ]] || MM_REL_ALLOWED+="mm-release namespaces=\"git\" $k"$'\n'
  done <<<"$MM_REL_KEYS"
}

# mm_release_withdrawn <x.y.z> <file>: prints the version whose CHANGELOG
# withdrew x.y.z (NEWEST) and succeeds when <file> lists it as WITHDRAWN. The
# installer reads the copy the release check writes beside releases/
# (release-withdrawn.env, RT-6.30 step 4), not release.env in the state
# directory, which XDG_STATE_HOME could move. A missing, symlinked or
# malformed file withdraws nothing. No network (decision (5) of 2026-11-12).
mm_release_withdrawn() {
  local want="$1" file="$2" text k v newest="" hit=0
  [[ "$want" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  [[ -f "$file" && ! -L "$file" ]] || return 1
  text="$(mm_read_file "$file" 65536)" || return 1
  while IFS='=' read -r k v; do
    case "$k" in
      NEWEST) [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && newest="$v" ;;
      WITHDRAWN) [[ "$v" == "$want" ]] && hit=1 ;;
    esac
  done <<<"$text"
  if [[ "$hit" -eq 0 || -z "$newest" ]] || ! mm_version_newer "$want" "$newest"; then return 1; fi
  printf '%s\n' "$newest"
}

# MM_RELEASE_URL (default the GitHub repository) → MM_REL_URL: a plain https
# URL (no user, query or space), or an absolute local path (a mirror, the
# tests) out of every sandbox's reach.
mm_release_url() {
  local u
  MM_REL_URL="${MM_RELEASE_URL:-https://github.com/dembaca/mattermost-ai-agent-bus.git}" MM_REL_ERR=""
  case "$MM_REL_URL" in
    https://*)
      [[ "$MM_REL_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~%+-]+)+/?$ ]] && return 0
      MM_REL_ERR="MM_RELEASE_URL is no plain https URL (no user, query or space); refused" ;;
    /* | file:///*)
      if ! u="$(cd -P "${MM_REL_URL#file://}" 2>/dev/null && pwd -P)"; then
        MM_REL_ERR="MM_RELEASE_URL: no such directory: ${MM_REL_URL#file://}"
      elif mm_sandbox_can_write "$u"; then
        MM_REL_ERR="MM_RELEASE_URL $u can be changed from inside a corral sandbox; refused"
      else
        MM_REL_URL="$u"
        return 0
      fi ;;
    *) MM_REL_ERR="MM_RELEASE_URL must be an https:// URL or an absolute path; refused" ;;
  esac
  return 1
}

# With keys (MM_REL_ENFORCE=1): git 2.34 or newer and an ssh-keygen with
# -Y find-principals (OpenSSH 8.2 or newer) verify an SSH signature.
mm_release_tools() {
  local gv
  MM_REL_ERR=""
  [[ "${MM_REL_ENFORCE:-0}" -eq 1 ]] || return 0
  if ! command -v ssh-keygen >/dev/null 2>&1; then
    MM_REL_ERR="ssh-keygen is required to verify the release's signature"
    return 1
  fi
  case "$(LC_ALL=C ssh-keygen -Y find-principals -f /dev/null -s /dev/null 2>&1 || true)" in
    *[Uu]nsupported* | *"illegal option"* | *"unknown option"* | *"invalid option"*)
      MM_REL_ERR="this ssh-keygen cannot verify SSH signatures (-Y find-principals, OpenSSH 8.2 or newer); nothing changed"
      return 1 ;;
  esac
  gv="$(git --version | sed -n 's/^git version \([0-9]*\.[0-9]*\).*/\1/p')"
  if [[ -z "$gv" ]] || mm_version_newer "$gv.0" "2.34.0"; then
    MM_REL_ERR="git 2.34 or newer is required to verify SSH signatures"
    return 1
  fi
}

# git on the bare repository MM_REL_GIT_DIR (src.git beside releases/), its
# temporary files in MM_REL_TMP, a directory only the host sees: no system or
# global config, attributes file, fsmonitor, hook, credential helper or
# replace refs, LC_ALL=C (its messages are matched), only the https and file
# protocols, no HTTP redirect, a transfer slower than 1 byte/s for 60 s given up
# (RT-6.32: the end hook waits for it), every received object checked (RT-6.29 step 2).
mm_rgit() {
  LC_ALL=C TMPDIR="$MM_REL_TMP" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
    GIT_NO_REPLACE_OBJECTS=1 \
    git --no-optional-locks -c core.attributesFile=/dev/null -c core.fsmonitor=false \
    -c core.hooksPath=/dev/null -c gc.auto=0 -c maintenance.auto=false \
    -c protocol.allow=never -c protocol.https.allow=always -c protocol.file.allow=always \
    -c http.followRedirects=false -c http.lowSpeedLimit=1 -c http.lowSpeedTime=60 \
    -c credential.helper= -c transfer.fsckObjects=true --git-dir="$MM_REL_GIT_DIR" "$@"
}

# mm_release_tag <tag> <tag object id>: RT-6.29 steps 4–5 on a fetched tag. An
# annotated tag in both modes; with keys (MM_REL_ENFORCE=1, MM_REL_ALLOWED) an
# SSH signature, no PGP or X.509 one, that `git verify-tag` accepts for the one
# principal mm-release; its own name the one asked for; it tags a commit. Sets
# MM_REL_SIGNER (the key's SHA256 fingerprint, with keys) and MM_REL_COMMIT.
# shellcheck disable=SC2034 # MM_REL_* are read by the caller
mm_release_tag() {
  local tag="$1" id="$2" tobj vout vrc=0 hdr tname ttype obj
  MM_REL_SIGNER="" MM_REL_COMMIT="" MM_REL_ERR=""
  if [[ "$(mm_rgit cat-file -t "$id" 2>/dev/null)" != tag ]]; then
    MM_REL_ERR="$tag is a lightweight tag (no tag object, so no signature): not a release"
    return 1
  fi
  tobj="$(mm_rgit cat-file tag "$id")" || { MM_REL_ERR="$tag cannot be read"; return 1; }
  if [[ "$MM_REL_ENFORCE" -eq 1 ]]; then
    case "$tobj" in
      *"-----BEGIN PGP SIGNATURE-----"* | *"-----BEGIN SIGNED MESSAGE-----"*)
        MM_REL_ERR="$tag carries a signature that is not an SSH signature (PGP or X.509)"
        return 1 ;;
      *"-----BEGIN SSH SIGNATURE-----"*) ;;
      *) MM_REL_ERR="$tag carries no signature: not a signed release"; return 1 ;;
    esac
    printf '%s' "$MM_REL_ALLOWED" >"$MM_REL_TMP/allowed_signers"
    vout="$(mm_rgit -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$MM_REL_TMP/allowed_signers" \
              -c gpg.ssh.program=ssh-keygen verify-tag "$id" 2>&1)" || vrc=$?
    if [[ "$vout" == *"no signature found"* ]]; then
      MM_REL_ERR="$tag carries no signature: not a signed release"
      return 1
    fi
    # SSH signatures: verify-tag's exit code with allowedSignersFile set, and
    # the one principal written above, nothing else.
    MM_REL_SIGNER="$(sed -n 's/^Good "git" signature for mm-release with [A-Z0-9-]* key \(SHA256:[A-Za-z0-9+/]*\)$/\1/p' <<<"$vout" | head -n1)"
    if [[ "$vrc" -ne 0 || -z "$MM_REL_SIGNER" ]]; then
      MM_REL_SIGNER=""
      if [[ "$vout" == *"No principal matched"* ]]; then
        MM_REL_ERR="$tag is signed by a key not pinned in ${MM_REL_FROM_TEXT:-MM_RELEASE_SIGNERS} ($(grep -o 'SHA256:[A-Za-z0-9+/]*' <<<"$vout" | head -n1 || true))"
        return 1
      fi
      MM_REL_ERR="$tag does not verify against ${MM_REL_FROM_TEXT:-MM_RELEASE_SIGNERS}:"$'\n'"$(tail -n 3 <<<"$vout" | tr -cd '[:print:]\n')"
      return 1
    fi
  fi
  hdr="$(sed '/^$/q' <<<"$tobj")"
  tname="$(sed -n 's/^tag //p' <<<"$hdr" | head -n1)"
  ttype="$(sed -n 's/^type //p' <<<"$hdr" | head -n1)"
  obj="$(sed -n 's/^object //p' <<<"$hdr" | head -n1)"
  if [[ "$tname" != "$tag" ]]; then
    MM_REL_ERR="the tag $tag is a tag object named $(tr -cd '[:alnum:]._-' <<<"$tname" | cut -c1-40): a ref pointed at another release"
    return 1
  fi
  if [[ "$ttype" != commit || ! "$obj" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]]; then
    MM_REL_ERR="$tag does not tag a commit"
    return 1
  fi
  MM_REL_COMMIT="$obj"
}

# The Needs: line of a CHANGELOG entry (ADR-011 point 3, RT-6.30) says what a
# release needs first, and it reaches every agent's context: only variable
# names (MM_*), versions (x.y.z) and lowercase words of at most 15 letters,
# each with an optional `,` or `.`, one space between, at most 120 characters.
# The same text as NEEDS_PATTERN in mcp/src/…/release.py, which
# scripts/doc-check.py reads; a test compares the two.
MM_NEEDS_RE='^(MM_[A-Z0-9_]+|[0-9]+\.[0-9]+\.[0-9]+|[a-z]{1,15})[,.]?( (MM_[A-Z0-9_]+|[0-9]+\.[0-9]+\.[0-9]+|[a-z]{1,15})[,.]?)*$'
mm_release_needs_ok() {
  [[ -n "$1" && ${#1} -le 120 && "$1" =~ $MM_NEEDS_RE ]]
}

# mm_release_notice <version> [state dir]: one line about the newest release
# the host's release check verified (RT-6.30), for a host copy or server of
# <version>, from release.env in the state directory (default mm_state_dir),
# which only that check writes; fails when there is nothing to say (no check
# yet, nothing newer, nothing withdrawn). Every field is checked again here.
# The same text as release.py's line(), which bus_info and the turn-end banner use.
mm_release_notice() {
  local have="$1" dir="${2:-$(mm_state_dir)}" text k v ver rest newest="" verified="" step
  local breaking=0 needs="" withdrawn=0 out=""
  [[ "$have" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  [[ -f "$dir/release.env" && ! -L "$dir/release.env" ]] || return 1
  text="$(mm_read_file "$dir/release.env" 65536)" || return 1
  while IFS='=' read -r k v; do
    case "$k" in
      NEWEST) [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && newest="$v" ;;
      VERIFIED) [[ "$v" == yes || "$v" == no ]] && verified="$v" ;;
    esac
  done <<<"$text"
  [[ -n "$newest" && -n "$verified" ]] || return 1
  while IFS='=' read -r k v; do
    ver="${v%% *}" rest="${v#* }"
    [[ "$ver" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || continue
    case "$k" in
      ENTRY)
        if mm_version_newer "$have" "$ver" && ! mm_version_newer "$newest" "$ver" && [[ "$rest" == breaking ]]; then
          breaking=1
        fi ;;
      NEEDS)
        if mm_version_newer "$have" "$ver" && ! mm_version_newer "$newest" "$ver" && [[ "$v" == *" "* ]] &&
           mm_release_needs_ok "$rest" && [[ "; $needs; " != *"; $rest; "* ]]; then
          needs="${needs:+$needs; }$rest"
        fi ;;
      WITHDRAWN) [[ "$v" == "$have" ]] && withdrawn=1 ;;
    esac
  done <<<"$text"
  if [[ "$withdrawn" -eq 1 ]]; then
    out="this host's ${have} is withdrawn (CHANGELOG of v${newest})"
  fi
  if mm_version_newer "$have" "$newest"; then
    if [[ "${newest%%.*}" != "${have%%.*}" ]]; then step="major"
    elif [[ "$(cut -d. -f2 <<<"$newest")" != "$(cut -d. -f2 <<<"$have")" ]]; then step="minor"
    else step="patch"; fi
    [[ "$breaking" -eq 0 ]] || step+=", Breaking"
    [[ -z "$needs" ]] || step+="; needs: $needs"
    if [[ "$verified" == yes ]]; then step+="; signature checked"; else step+="; signature not checked"; fi
    out="${out:+$out; }release v${newest} is out (${step})"
  elif mm_version_newer "$newest" "$have"; then
    out="${out:+$out; }the newest release that passes the check is v${newest}, older than ${have}: the repository may be frozen or rewound"
  fi
  [[ -n "$out" ]] || return 1
  printf '%s\n' "$out"
}

# --- self-update between sessions (ADR-011 phase 3, RT-6.32) --------------------
# The registrar may offer the release keys and name the version hosts update
# themselves to, in a record signed with its key. Beside releases/ (the layout
# directory, out of XDG_STATE_HOME's reach, like release-signers):
# release-hold, which stops self-updates (--hold, --unhold). In the host state
# directory, for the notice only: release-auto.env (what the registrar named
# when last read, the keys' state, the last decision) and release-auto.log
# (one line per decision).
# shellcheck disable=SC2034 # read by the installer and the release check
MM_HOLD_FILE=release-hold MM_AUTO_FILE=release-auto.env MM_AUTO_LOG=release-auto.log

# Whether self-updates are held on the host copy in layout directory $1:
# anything named release-hold there counts, a symlink too (fail safe).
mm_release_held() {
  [[ -e "$1/$MM_HOLD_FILE" || -L "$1/$MM_HOLD_FILE" ]]
}

# mm_bounded <seconds> <command…>: run the command, its stdout and stderr as
# they are, and stop it after <seconds>: TERM to it and its children (the
# installer's EXIT trap then removes its lock and half-made release). No
# watchdog process is left behind. Its exit status, 124 when stopped.
mm_bounded() {
  local max="$1" pid end rc=0
  shift
  "$@" &
  pid=$!
  end=$((SECONDS + max))
  while kill -0 "$pid" 2>/dev/null; do
    if (( SECONDS >= end )); then
      pkill -TERM -P "$pid" 2>/dev/null || true
      kill -TERM "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      return 124
    fi
    sleep 0.2
  done
  wait "$pid" 2>/dev/null || rc=$?
  return "$rc"
}

# mm_install_lock_live <lock dir>: whether the installer's lock (.install.lock
# beside releases/) belongs to a running installer: the pid it holds lives, or
# it holds none yet and is younger than ten minutes (an installer between its
# mkdir and writing the pid, or one from before 0.86.0). A lock whose
# installer is gone is stale (RT-6.28, RT-6.32).
mm_install_lock_live() {
  local pid
  [[ -d "$1" ]] || return 1
  pid="$(mm_read_file "$1/pid" 64 2>/dev/null | tr -cd '0-9')" || pid=""
  if [[ -n "$pid" ]]; then
    kill -0 "$pid" 2>/dev/null
  else
    [[ -z "$(find "$1" -maxdepth 0 -mmin +10 2>/dev/null)" ]]
  fi
}

# mm_registrar_release <python>: the registrar's release record, read and
# verified by the host copy's own Python (mattermost_ai_agent_bus.selfupdate,
# attest.verify_release) with MM_REGISTRAR_KEYS, from /, with an environment
# of PATH, HOME, LC_ALL, the registrar's keys and URLs and TLS and proxy settings only, stopped after
# 30 s. Its output is checked again here: MM_RR_STATUS (ok, none, failed,
# nokeys), MM_RR_WHY, MM_RR_KID, MM_RR_VERSION (x.y.z or ""), MM_RR_KEYS (one
# `<type> <base64>` per line, by mm_release_parse_keys). Never fails.
# shellcheck disable=SC2034 # MM_RR_* are read by the caller
mm_registrar_release() {
  local py="$1" out="" line k v st="" keys="" bad=0
  local -a e
  MM_RR_STATUS=failed MM_RR_WHY="" MM_RR_KID="" MM_RR_VERSION="" MM_RR_KEYS=""
  if [[ -z "${MM_REGISTRAR_KEYS:-}" ]]; then
    MM_RR_STATUS=nokeys MM_RR_WHY="no registrar keys pinned (MM_REGISTRAR_KEYS): nothing taken from the registrar"
    return 0
  fi
  if [[ ! -x "$py" || -d "$py" ]]; then
    MM_RR_WHY="no Python environment in the host copy ($py)"
    return 0
  fi
  e=(HOME="$HOME" PATH=/usr/bin:/bin:/usr/sbin:/sbin LC_ALL=C MM_REGISTRAR_KEYS="$MM_REGISTRAR_KEYS")
  for k in MM_REGISTER_URL MM_CHAT_URL SSL_CERT_FILE SSL_CERT_DIR HTTPS_PROXY https_proxy NO_PROXY no_proxy; do
    if [[ -n "${!k:-}" ]]; then e+=("$k=${!k}"); fi
  done
  out="$(cd / && mm_bounded 30 env -i "${e[@]}" "$py" -I -m mattermost_ai_agent_bus.selfupdate \
           </dev/null 2>/dev/null | head -c 16384)" || out=""
  while IFS= read -r line; do
    k="${line%%=*}" v="${line#*=}"
    [[ "$line" == *=* ]] || continue
    case "$k" in
      STATUS) [[ -z "$st" && "$v" =~ ^(ok|none|failed|nokeys)$ ]] && st="$v" ;;
      WHY) MM_RR_WHY="$(tr -cd '[:print:]' <<<"$v" | cut -c1-200)" ;;
      KID) if [[ "$v" =~ ^[A-Za-z0-9_-]{1,64}$ ]]; then MM_RR_KID="$v"; fi ;;
      VERSION) if [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && -z "$MM_RR_VERSION" ]]; then MM_RR_VERSION="$v"; else bad=1; fi ;;
      SIGNER) keys+="$v"$'\n' ;;
    esac
  done <<<"$out"
  case "$st" in
    "") MM_RR_STATUS=failed MM_RR_WHY="the registrar's release record was not read (no answer from the host copy's Python)"; return 0 ;;
    ok) ;;
    *) MM_RR_STATUS="$st" MM_RR_VERSION=""; return 0 ;;
  esac
  if [[ "$bad" -eq 1 ]] || ! mm_release_parse_keys "the registrar's release record" "$keys" || [[ -z "$MM_REL_PARSED" ]]; then
    MM_RR_STATUS=failed MM_RR_VERSION="" MM_RR_WHY="the registrar's release record has a line out of its form; refused"
    return 0
  fi
  MM_RR_STATUS=ok MM_RR_KEYS="$MM_REL_PARSED"
}

# After mm_release_signers and an `ok` mm_registrar_release: how the keys the
# registrar offers relate to the host's: `none` (no pin yet: they would be
# pinned), `match` (the same set) or `mismatch` (refused: a person changes keys
# on the host, --replace-signers). Compared as sets of keys, by type and the
# decoded key (ssh-keygen's fingerprint), so another spelling of the same key
# (padding, order, a comment) is the same key; without ssh-keygen by text.
mm_release_key_ids() {
  local n ids
  n="$(grep -c . <<<"$1")"
  if ids="$(grep . <<<"$1" | ssh-keygen -l -f /dev/stdin 2>/dev/null | awk '{print $2, $NF}')" &&
     [[ "$(grep -c . <<<"$ids")" -eq "$n" ]]; then
    sort -u <<<"$ids"
  else
    sort -u <<<"$1"
  fi
}
mm_release_keystate() {
  if [[ "${MM_REL_FROM:-none}" == none ]]; then
    printf 'none\n'
  elif [[ "$(mm_release_key_ids "$MM_RR_KEYS")" == "$(mm_release_key_ids "$MM_REL_KEYS")" ]]; then
    printf 'match\n'
  else
    printf 'mismatch\n'
  fi
}

# mm_auto_state <state dir> KEY=value…: merge into release-auto.env (0600, one
# rename); the values are words, versions and epochs only. For the notice.
mm_auto_state() {
  local dir="$1" f line k kv keep="" new="" tmp
  shift
  f="$dir/$MM_AUTO_FILE"
  [[ -d "$dir" && ! -L "$dir" && ! -L "$f" ]] || return 0
  for kv in "$@"; do new+="$kv"$'\n'; done
  if [[ -f "$f" ]]; then
    while IFS= read -r line; do
      k="${line%%=*}"
      [[ "$line" == *=* && "$k" =~ ^[A-Z_]+$ && $'\n'"$new" != *$'\n'"$k="* ]] || continue
      keep+="$line"$'\n'
    done < <(mm_read_file "$f" 4096 || true)
  fi
  tmp="$dir/.$MM_AUTO_FILE.$$"
  { (umask 077 && printf '%s%s' "$keep" "$new" >"$tmp") && mv -f "$tmp" "$f"; } 2>/dev/null || rm -f "$tmp"
}

# mm_auto_log <state dir> <text>: one line per self-update decision in
# release-auto.log, the newest 200 kept.
mm_auto_log() {
  local dir="$1" f tmp text
  f="$dir/$MM_AUTO_LOG"
  [[ -d "$dir" && ! -L "$dir" && ! -L "$f" ]] || return 0
  text="$(head -n1 <<<"$2" | tr -cd '[:print:]' | cut -c1-400)"
  tmp="$dir/.$MM_AUTO_LOG.$$"
  { { tail -n 199 "$f" 2>/dev/null || true; printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$text"; } >"$tmp" &&
    chmod 600 "$tmp" && mv -f "$tmp" "$f"; } 2>/dev/null || rm -f "$tmp"
}

# mm_release_auto_notice <version> <state dir> <layout dir>: what self-update
# will do for a host copy session of <version>, in plain words (RT-6.32); fails
# when there is nothing to say. The same text as release.py's auto_line().
mm_release_auto_notice() {
  local have="$1" dir="$2" layout="$3" text k v target="" keys="" out=""
  [[ "$have" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  if [[ -f "$dir/$MM_AUTO_FILE" && ! -L "$dir/$MM_AUTO_FILE" ]] && text="$(mm_read_file "$dir/$MM_AUTO_FILE" 4096)"; then
    while IFS='=' read -r k v; do
      case "$k" in
        TARGET) [[ "$v" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] && target="$v" ;;
        KEYS) [[ "$v" =~ ^(none|match|mismatch|bad)$ ]] && keys="$v" ;;
      esac
    done <<<"$text"
  fi
  if mm_release_held "$layout"; then
    out="self-update held on this host (mm-agent-host-install.sh --unhold lifts it)"
  elif [[ "$keys" == mismatch ]]; then
    out="the registrar names a release key this host doesn't trust: no self-update"
  elif [[ "$keys" == bad ]]; then
    out="the release keys on this host can't be used (see release-auto.log): no self-update"
  elif [[ -n "$target" ]] && mm_version_newer "$have" "$target"; then
    out="updates itself to v${target} when this session ends"
  fi
  [[ -n "$out" ]] || return 1
  printf '%s\n' "$out"
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
  # The registrar's signed owner claim (RT-6.24), only when it sent one.
  [[ -z "${MM_BOT_ATTESTATION:-}" ]] || printf 'export MM_BOT_ATTESTATION=%q\n' "$MM_BOT_ATTESTATION"
}

# A registrar attestation has the shape of a compact JWS: three base64url
# segments, at most 8192 characters (RT-6.24; the MCP server checks the rest).
mm_attestation_shape() {
  [[ ${#1} -le 8192 && "$1" =~ ^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$ ]]
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
${MM_BOT_ATTESTATION:+MM_BOT_ATTESTATION=${MM_BOT_ATTESTATION}}
MM_BOT_TOKEN=${MM_BOT_TOKEN:-}
MATTERMOST_URL=${MATTERMOST_URL:-${MM_CHAT_URL:-}}
MATTERMOST_TOKEN=${MATTERMOST_TOKEN:-${MM_BOT_TOKEN:-}}
MM_TEAM=${MM_TEAM:-}
MM_CHANNEL=${MM_CHANNEL:-}
${MM_PROJECT_CHANNEL:+MM_PROJECT_CHANNEL=${MM_PROJECT_CHANNEL}}
EOF
}

# --- corral sessions kept for `claude --continue` (RT-6.18 steps 9–10) ---------
# postEnd keeps the bot of a Claude session that may be resumed: its session
# file stays in the host state dir, marked MM_SERVER_ENDED=<epoch s>,
# MM_KEPT_WORKDIR=<key of the real workdir> and MM_KEPT_CLAUDE_SID=<key of the
# Claude session id>, and its state directory hosted/<session>/ stays with it.
# Only keys (sha1, 16 hex digits): the workdir and the session id are never
# used as paths on the host.

# The grace period of a kept bot: MM_SESSION_KEEP_SEC, default 24 h.
mm_keep_sec() {
  local k="${MM_SESSION_KEEP_SEC:-86400}"
  [[ "$k" =~ ^[0-9]+$ ]] || k=86400
  printf '%s' "$k"
}

# sha1 of a string, the first 16 hex digits (session_file.py keys the same way).
mm_key16() { printf '%s' "$1" | { sha1sum 2>/dev/null || shasum; } | cut -c1-16; }

# The key of a workdir: its real path, else the path as given.
mm_workdir_key() {
  local real
  [[ -n "${1:-}" ]] || return 1
  real="$(cd -P "$1" 2>/dev/null && pwd -P)" || real="$1"
  mm_key16 "$real"
}

# When a kept corral session ended (epoch s). Only a file with both marks is
# one; anything else fails.
mm_kept_ended() {
  local ended
  [[ -f "$1" && ! -L "$1" ]] || return 1
  ended="$(sed -n 's/^MM_SERVER_ENDED=//p' "$1" | head -n1)"
  [[ "$ended" =~ ^[0-9]+$ ]] && grep -Eq '^MM_KEPT_WORKDIR=[0-9a-f]{16}$' "$1" || return 1
  printf '%s' "$ended"
}

# How corral started the agent, best effort (RT-6.18 step 10): corral's own
# command line (its pid is the hook's parent), the words after its first
# `--`, which corral passes to the agent. Prints `continue` (-c, --continue)
# or `resume <id>` (-r <id>, --resume <id>, --resume=<id>); nothing for a
# plain start, the resume picker (no id), --fork-session, or more than one of
# them. Fails when the command line cannot be read. corral does not document
# its command line for hooks; a CORRAL_AGENT_ARGS of its own would replace this.
mm_corral_resume() {
  local line t mode="" id="" after=0 want=0 n=0 idre='^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
  local -a words
  [[ "${1:-}" =~ ^[0-9]+$ ]] || return 1
  line="$(ps -ww -o args= -p "$1" 2>/dev/null)" && [[ -n "$line" ]] || return 1
  read -r -a words <<<"$line"
  for t in "${words[@]}"; do
    if (( ! after )); then
      [[ "$t" != -- ]] || after=1
      continue
    fi
    if (( want )); then
      want=0
      [[ "$t" =~ $idre ]] || return 0      # --resume without an id: the picker
      mode=resume id="$t" n=$((n + 1))
      continue
    fi
    case "$t" in
      -c|--continue) mode=continue n=$((n + 1)) ;;
      -r|--resume) want=1 ;;
      --resume=*)
        t="${t#--resume=}"
        [[ "$t" =~ $idre ]] || return 0
        mode=resume id="$t" n=$((n + 1)) ;;
      --fork-session) return 0 ;;
    esac
  done
  (( ! want && n == 1 )) || return 0
  if [[ "$mode" == resume ]]; then printf 'resume %s' "$id"; else printf 'continue'; fi
}

# The kept session a start may take over (RT-6.18 step 10), of this workdir
# (key $2): with `continue` the one that ended last, with `resume` the one
# kept for that Claude session (key $4). Prints its file.
mm_kept_candidate() {
  local dir="$1" wkey="$2" mode="$3" skey="${4:-}" f ended best="" best_at=-1
  for f in "$dir"/*.env; do
    ended="$(mm_kept_ended "$f")" || continue
    [[ "$(sed -n 's/^MM_KEPT_WORKDIR=//p' "$f" | head -n1)" == "$wkey" ]] || continue
    if [[ "$mode" == resume ]]; then
      [[ -n "$skey" && "$(sed -n 's/^MM_KEPT_CLAUDE_SID=//p' "$f" | head -n1)" == "$skey" ]] || continue
    fi
    if (( ended > best_at )); then best="$f" best_at="$ended"; fi
  done
  [[ -n "$best" ]] || return 1
  printf '%s' "$best"
}

# Unregister the bot of a session file with the file's own token, which the
# registrar always accepts for it; when the registrar refuses that token and
# Mattermost does too, the bot is gone already (mm_token_gone). Either way the
# file goes, and with it the
# session's host state directory (hosted/<file name>/). Returns 0 unregistered,
# 2 gone already, 1 failed (the reason on stdout). The file is loaded in a
# subshell, so one bot's values never reach the next. Needs BIN.
mm_unregister_session_file() {
  local f="$1" name="$2" err rc=0 d
  # shellcheck disable=SC2030,SC2031
  err="$( (
      export MM_AGENT_SESSION_FILE="$f"
      mm_load_session_file >/dev/null 2>&1 || exit 1
      export -n MM_BOT_TOKEN MATTERMOST_TOKEN
      mm_unregister_with mm_with_token "$name" && exit 0
      if mm_token_gone "$MM_UNREG_HTTP"; then exit 2; fi
      printf '%s' "$MM_UNREG_WHY"
      exit 1
     ) )" || rc=$?
  if [[ "$rc" -ne 0 && "$rc" -ne 2 ]]; then
    printf '%s' "${err:-no reason given}"
    return 1
  fi
  rm -f "$f"
  d="$(dirname "$f")/hosted/$(basename "$f" .env)"
  if [[ -d "$d" && ! -L "$d" ]]; then rm -rf "$d"; fi
  return "$rc"
}

# A claim whose start died (.<session>.env.taken-<pid>, RT-6.18 step 10)
# goes back under its name, a kept session again: reaped in time, never lost.
mm_restore_claims() {
  local dir="$1" f pid orig uid
  for f in "$dir"/.*.env.taken-*; do
    [[ -f "$f" && ! -L "$f" ]] || continue
    pid="${f##*.taken-}"
    [[ "$pid" =~ ^[0-9]+$ ]] || continue
    ! kill -0 "$pid" 2>/dev/null || continue
    # It got as far as this session's file: the bot is in use, the claim is
    # only a copy and must never be reaped as kept.
    uid="$(sed -n 's/^MM_BOT_USER_ID=//p' "$f" | head -n1)"
    if [[ "$uid" =~ ^[a-z0-9]{26}$ ]] && grep -qx "MM_BOT_USER_ID=${uid}" "$dir"/*.env 2>/dev/null; then
      rm -f "$f"
      continue
    fi
    orig="${f%.taken-*}"
    orig="${dir}/${orig##*/.}"
    if [[ ! -e "$orig" ]]; then mv "$f" "$orig" 2>/dev/null || true; fi
  done
}

# Reap what kept corral sessions left in a state dir (RT-6.18 step 10): claims
# of dead starts go back (mm_restore_claims); a kept bot whose grace period is
# over is unregistered with its own token, its file and state directory
# removed. Notes on stderr. mm-agent-sweep.sh does the same per file.
mm_reap_kept() {
  local dir="$1" keep now f ended name why rc
  keep="$(mm_keep_sec)"
  now="$(date +%s)"
  mm_restore_claims "$dir"
  for f in "$dir"/*.env; do
    ended="$(mm_kept_ended "$f")" || continue
    (( now - ended >= keep )) || continue
    name="$(sed -n 's/^MM_REG_BOT_NAME=//p' "$f" | head -n1)"
    [[ -n "$name" ]] || continue
    rc=0
    why="$(mm_unregister_session_file "$f" "$name")" || rc=$?
    case "$rc" in
      0) echo "mm-bus: unregistered ${name}, kept for claude --continue since $(( (now - ended) / 3600 )) h" >&2 ;;
      2) echo "mm-bus: ${name}, kept for claude --continue, is gone already; removed its file" >&2 ;;
      *) echo "mm-bus: WARNING: could not unregister ${name}, kept past its grace period: ${why}" >&2 ;;
    esac
  done
}

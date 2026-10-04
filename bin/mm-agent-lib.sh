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

mm_agent_env_dir() {
  local d="${MM_AGENT_ENV_DIR:-${HOME}/.config/mm-agent-bus/agents}"
  if [[ "$d" == ~* ]]; then
    d="${d/#\~/$HOME}"
  fi
  printf '%s' "$d"
}

mm_session_file() {
  local f="${MM_AGENT_SESSION_FILE:-${XDG_RUNTIME_DIR:-/tmp}/mm-agent-session.env}"
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
      MM_CHAT_URL|MM_BOT_NAME|MM_BOT_USERNAME|MM_BOT_USER_ID|MM_BOT_TOKEN|MATTERMOST_URL|MATTERMOST_TOKEN|MM_REG_SECRET|MM_TEAM|MM_CHANNEL|MM_REGISTER_URL)
        export "$key=$val"
        ;;
    esac
  done <"$f"
}

mm_load_session_file() {
  mm_load_env_file "$(mm_session_file)"
}

# Session file written by the MCP server's session_start (see
# mcp/.../session_file.py). Keyed on the directory the server runs in, which is
# the project directory Claude Code was started in. A hook may run deeper (the
# agent cd'ed into a subdirectory), so the lookup walks up from CLAUDE_PROJECT_DIR
# / $PWD to the root and takes the nearest session file. The key must match
# session_file_path() there.
mm_hook_session_file() {
  local dir state key f
  dir="$(cd "${CLAUDE_PROJECT_DIR:-$PWD}" 2>/dev/null && pwd -P)" || return 1
  state="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
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

# For hooks: pick up the bot token of an MCP-managed session. A no-op when the
# environment already carries a token (host-managed session).
mm_load_hook_session() {
  [[ -z "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]] || return 0
  local f
  f="$(mm_hook_session_file)" || return 1
  mm_load_env_file "$f"
}

mm_emit_session_exports() {
  cat <<EOF
export MM_CHAT_URL=$(printf '%q' "${MM_CHAT_URL:-}")
export MM_BOT_NAME=$(printf '%q' "${MM_BOT_NAME:-}")
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
  f="$(mm_session_file)"
  umask 077
  mkdir -p "$(dirname "$f")"
  cat >"$f" <<EOF
# ephemeral session — do not commit
MM_CHAT_URL=${MM_CHAT_URL:-}
MM_BOT_NAME=${MM_BOT_NAME:-}
MM_BOT_USERNAME=${MM_BOT_USERNAME:-}
MM_BOT_USER_ID=${MM_BOT_USER_ID:-}
MM_BOT_TOKEN=${MM_BOT_TOKEN:-}
MATTERMOST_URL=${MATTERMOST_URL:-${MM_CHAT_URL:-}}
MATTERMOST_TOKEN=${MATTERMOST_TOKEN:-${MM_BOT_TOKEN:-}}
MM_TEAM=${MM_TEAM:-}
MM_CHANNEL=${MM_CHANNEL:-}
EOF
}

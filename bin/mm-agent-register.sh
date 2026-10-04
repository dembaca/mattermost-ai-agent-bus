#!/usr/bin/env bash
# Register an ephemeral Mattermost agent bot via the registrar: print export
# lines and write the session file.
set -euo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

NAME=""
DISPLAY=""
EXPORTS_ONLY=0

usage() {
  echo "usage: $0 [--exports] <short-name> [display-name]" >&2
  echo "  requires MM_REG_SECRET; uses MM_CHAT_URL / MM_REGISTER_URL" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --exports) EXPORTS_ONLY=1; shift ;;
    -h|--help) usage ;;
    *)
      if [[ -z "$NAME" ]]; then NAME="$1"
      elif [[ -z "$DISPLAY" ]]; then DISPLAY="$1"
      else usage
      fi
      shift
      ;;
  esac
done

[[ -n "$NAME" ]] || usage
DISPLAY="${DISPLAY:-$NAME}"
mm_require_reg_secret
mm_validate_short_name "$NAME"

CHAT_URL="$(mm_chat_url)"
REG_URL="$(mm_register_url)"

RESP="$(mm_json_post "$REG_URL" "$MM_REG_SECRET" \
  "$(jq -nc --arg n "$NAME" --arg d "$DISPLAY" '{name:$n, display_name:$d}')")"

USER_NAME="$(jq -r '.username // empty' <<<"$RESP")"
BOT_TOKEN="$(jq -r '.bot_token // empty' <<<"$RESP")"
USER_ID="$(jq -r '.user_id // empty' <<<"$RESP")"
URL="$(jq -r '.url // empty' <<<"$RESP")"
URL="${URL:-$CHAT_URL}"

if [[ -z "$BOT_TOKEN" || -z "$USER_NAME" ]]; then
  echo "register failed: $RESP" >&2
  exit 1
fi

export MM_CHAT_URL="$URL"
export MM_BOT_NAME="$NAME"
export MM_BOT_USERNAME="$USER_NAME"
export MM_BOT_USER_ID="$USER_ID"
export MM_BOT_TOKEN="$BOT_TOKEN"
export MATTERMOST_URL="$URL"
export MATTERMOST_TOKEN="$BOT_TOKEN"

mm_write_session_file
if [[ "$EXPORTS_ONLY" -eq 0 ]]; then
  echo "registered ${USER_NAME} (${USER_ID}) ephemeral" >&2
  echo "session: $(mm_session_file)" >&2
fi
mm_emit_session_exports

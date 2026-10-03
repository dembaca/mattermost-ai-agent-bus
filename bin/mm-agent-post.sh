#!/usr/bin/env bash
# Post as the bot from MM_BOT_TOKEN / MATTERMOST_TOKEN.
# Usage: mm-agent-post.sh <channel_id> <message> [root_id]
set -euo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

CHANNEL_ID="${1:-}"
MESSAGE="${2:-}"
ROOT_ID="${3:-}"
CHAT_URL="$(mm_chat_url)"
TOKEN="$(mm_bot_token)"

if [[ -z "$CHANNEL_ID" || -z "$MESSAGE" ]]; then
  echo "usage: $0 <channel_id> <message> [root_id]" >&2
  exit 2
fi

BODY="$(jq -nc --arg c "$CHANNEL_ID" --arg m "$MESSAGE" --arg r "$ROOT_ID" '
  {channel_id:$c, message:$m} + (if $r == "" then {} else {root_id:$r} end)
')"

curl -fsS -X POST "${CHAT_URL}/api/v4/posts" \
  -H "$(mm_auth_header "$TOKEN")" \
  -H "Content-Type: application/json" \
  -d "$BODY" | jq '{id, channel_id, root_id, create_at, message}'

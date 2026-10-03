#!/usr/bin/env bash
# Poll a channel for new posts (bot token). Prints JSON lines for new posts.
set -euo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

CHAT_URL="$(mm_chat_url)"
TOKEN="$(mm_bot_token)"
CHANNEL_NAME="$(mm_channel)"
TEAM_NAME="$(mm_team)"
INTERVAL=15
SINCE_MS=""
ONCE=0

usage() {
  echo "usage: $0 [--channel NAME] [--team NAME] [--interval SEC] [--since-ms MS] [--once]" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel) CHANNEL_NAME="$2"; shift 2 ;;
    --team) TEAM_NAME="$2"; shift 2 ;;
    --interval) INTERVAL="$2"; shift 2 ;;
    --since-ms) SINCE_MS="$2"; shift 2 ;;
    --once) ONCE=1; shift ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

auth=(-H "$(mm_auth_header "$TOKEN")")
TEAM_ID="$(curl -fsS "${auth[@]}" "${CHAT_URL}/api/v4/teams/name/${TEAM_NAME}" | jq -r .id)"
CHANNEL_ID="$(curl -fsS "${auth[@]}" \
  "${CHAT_URL}/api/v4/teams/${TEAM_ID}/channels/name/${CHANNEL_NAME}" | jq -r .id)"

if [[ -z "$SINCE_MS" ]]; then
  SINCE_MS="$(( $(date +%s) * 1000 - 60000 ))"
fi

echo "polling team=${TEAM_NAME} channel=${CHANNEL_NAME} id=${CHANNEL_ID} since_ms=${SINCE_MS}" >&2

declare -A SEEN=()

while true; do
  POSTS="$(curl -fsS "${auth[@]}" \
    "${CHAT_URL}/api/v4/channels/${CHANNEL_ID}/posts?since=${SINCE_MS}&per_page=50")"

  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    id="$(jq -r .id <<<"$line")"
    ts="$(jq -r .create_at <<<"$line")"
    if [[ -n "${SEEN[$id]:-}" ]]; then
      continue
    fi
    SEEN[$id]=1
    echo "$line"
    if [[ "$ts" =~ ^[0-9]+$ && "$ts" -gt "$SINCE_MS" ]]; then
      SINCE_MS="$ts"
    fi
  done < <(jq -c '(.posts // {}) | to_entries | map(.value) | sort_by(.create_at)[]' <<<"$POSTS")

  [[ "$ONCE" -eq 1 ]] && break
  sleep "$INTERVAL"
done

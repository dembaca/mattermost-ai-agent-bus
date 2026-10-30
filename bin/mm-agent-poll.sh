#!/usr/bin/env bash
# Print new posts of a channel as JSON lines, oldest first, through this
# session's MCP server (RT-6.17): it only looks, nothing is marked read.
#
#   mm-agent-poll.sh [--channel ID|NAME] [--interval SEC] [--since-ms MS] [--once]
#
# Default channel: the home (the first MM_PROJECT_CHANNEL entry, else MM_CHANNEL); a
# NAME is looked up in the bot's team. Only the work, project and default
# channel can be read, never a DM or group channel (the server's 403, exit 1). Default start: one minute ago. Each line
# is one post as GET /v1/posts gives it: id, user_id, root_id, create_at, type,
# message, to (all: for all agents), intent, fyi, gated, and files when it has
# any (get_file fetches one, RT-6.23). An edited post is not
# printed again. Collecting what is for you (and marking it read) is the MCP
# tool wait_for_events. No token here: without a reachable server of this
# session, exit 3 and what to do; a server that goes away later is asked again
# next round.
set -uo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

CHANNEL=""
INTERVAL=15
SINCE_MS=""
ONCE=0

usage() {
  echo "usage: $0 [--channel ID|NAME] [--interval SEC] [--since-ms MS] [--once]" >&2
  exit 2
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel) [[ $# -ge 2 ]] || usage; CHANNEL="$2"; shift 2 ;;
    --team) echo "$0: --team is gone: the server reads the bot's team (MM_TEAM)" >&2; exit 2 ;;
    --interval) [[ "${2:-}" =~ ^[1-9][0-9]*$ ]] || usage; INTERVAL="$2"; shift 2 ;;
    --since-ms) [[ "${2:-}" =~ ^[0-9]+$ ]] || usage; SINCE_MS="$2"; shift 2 ;;
    --once) ONCE=1; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) usage ;;
  esac
done
command -v jq >/dev/null 2>&1 || { echo "mm-agent-poll.sh: jq is required" >&2; exit 2; }
[[ "$CHANNEL" =~ ^[A-Za-z0-9._-]*$ ]] || { echo "$0: --channel: an id or a channel name" >&2; exit 2; }

if [[ -z "$SINCE_MS" ]]; then
  SINCE_MS="$(( $(date +%s) * 1000 - 60000 ))"
fi

declare -A SEEN=()
reached=0
while true; do
  if MM_BUS_MAX_TIME="${MM_BUS_MAX_TIME:-10}" mm_bus GET "/v1/posts?since=${SINCE_MS}${CHANNEL:+&channel=${CHANNEL}}"; then
    if [[ "$reached" -eq 0 ]]; then
      # The server names the channel: the work channel when none was given.
      echo "polling channel=$(jq -r '.channel // empty' <<<"$MM_BUS_BODY") since_ms=${SINCE_MS}" >&2
    fi
    reached=1
    while IFS= read -r line; do
      [[ -z "$line" ]] && continue
      id="$(jq -r .id <<<"$line")"
      ts="$(jq -r .create_at <<<"$line")"
      [[ -n "${SEEN[$id]:-}" ]] && continue
      SEEN[$id]=1
      echo "$line"
      if [[ "$ts" =~ ^[0-9]+$ && "$ts" -gt "$SINCE_MS" ]]; then
        SINCE_MS="$ts"
      fi
    done < <(jq -c '.posts | sort_by(.create_at)[]' <<<"$MM_BUS_BODY")
  elif [[ "$reached" -eq 0 || "$MM_BUS_STATUS" == 400 || "$MM_BUS_STATUS" == 403 ]]; then
    mm_bus_explain
    [[ "$MM_BUS_STATUS" == 400 || "$MM_BUS_STATUS" == 403 ]] && exit 1
    exit 3
  fi
  [[ "$ONCE" -eq 1 ]] && break
  sleep "$INTERVAL"
done

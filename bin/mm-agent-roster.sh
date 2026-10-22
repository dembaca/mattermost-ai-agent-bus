#!/usr/bin/env bash
# List the reachable agents in a channel, coordinator first.
#
#   mm-agent-roster.sh             # project channel, else default channel
#   mm-agent-roster.sh <channel>   # a channel name (in MM_TEAM) or id
#   mm-agent-roster.sh --all       # also list idle, offline and stale bots
#   mm-agent-roster.sh --json      # the same rows as one JSON array (MCP server, RT-6.14)
#
# Prints one line per agent-* bot: username, creation time (UTC), presence,
# custom status. The first line is the coordinator: the agent wearing a live 👑
# (RT-6.6), else the oldest reachable agent, which the lease hands the crown to
# next. Session bots are ephemeral, so create_at is when that session started.
#
# Reachable means not deactivated, not offline, a custom status that has not
# expired, and not 💤 idle. Statuses are leases renewed by a live session, so a
# failed teardown — enabled, maybe even "online", but nobody renewing — drops
# out once its lease runs out. Leftovers are the operator's to remove with
# mm-agent-sweep.sh.
#
# Read-only, needs only the bot token, so it works in host-managed sessions too.
set -euo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

die() {
  echo "$1" >&2
  exit 1
}

ALL=0
JSON=0
ARG=""
for a in "$@"; do
  case "$a" in
    --all) ALL=1 ;;
    --json) JSON=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    -*) echo "unknown option: $a" >&2; exit 2 ;;
    *) ARG="$a" ;;
  esac
done

CHANNEL_ID="$(mm_work_channel_id "$ARG")" || exit 1

mm_roster_json "$CHANNEL_ID" ||
  die "could not list channel members or their presence: HTTP ${MM_API_STATUS:-?}"
[[ "$(jq 'length' <<<"$ROSTER_JSON")" -gt 0 ]] || {
  [[ "$JSON" == 1 ]] && echo "[]"
  echo "no agents in channel" >&2
  exit 0
}
if [[ "$JSON" == 1 ]]; then
  jq -c --argjson all "$ALL" '
    (map(select(.crown))[0].id // map(select(.reachable))[0].id // "") as $c
    | (map(select(.id == $c)) + map(select(.id != $c)))
    | map(select($all == 1 or .reachable) | .coordinator = (.id == $c))' <<<"$ROSTER_JSON"
  exit 0
fi

jq -r --argjson all "$ALL" '
  (map(select(.crown))[0].id // map(select(.reachable))[0].id // "") as $c
  | (map(select(.id == $c)) + map(select(.id != $c)))
  | .[]
  | select($all == 1 or .reachable)
  | [.username, (.create_at / 1000 | floor | todate), .presence, .text]
  | @tsv' <<<"$ROSTER_JSON"

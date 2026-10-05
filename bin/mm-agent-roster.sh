#!/usr/bin/env bash
# List the active agents in a channel, oldest first.
#
#   mm-agent-roster.sh             # project channel, else default channel
#   mm-agent-roster.sh <channel>   # a channel name (in MM_TEAM) or id
#   mm-agent-roster.sh --all       # also list offline bots
#
# Prints one line per active agent-* bot: username, creation time (UTC),
# presence, custom status. The first line is the default coordinator — session
# bots are ephemeral, so create_at is when that session started, and the oldest
# session still alive coordinates unless it handed the role over in the channel
# (a confirmed HANDOVER post; this script does not read those).
#
# Active means not deactivated (delete_at == 0) AND not offline. A failed
# teardown leaves a bot that is still enabled but offline for good; counting it
# would hand the coordinator role to a dead session. Those leftovers are the
# operator's to remove with mm-agent-sweep.sh.
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
ARG=""
for a in "$@"; do
  case "$a" in
    --all) ALL=1 ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
    -*) echo "unknown option: $a" >&2; exit 2 ;;
    *) ARG="$a" ;;
  esac
done

CHANNEL_ID="$(mm_work_channel_id "$ARG")" || exit 1

mm_api GET "/api/v4/users?in_channel=${CHANNEL_ID}&per_page=200"
[[ "$MM_API_STATUS" == "200" ]] ||
  die "could not list channel members: HTTP ${MM_API_STATUS} ${MM_API_BODY}"
USERS="$(jq -c '[.[] | select(.username | startswith("agent-"))
                     | select(.delete_at == 0)] | sort_by(.create_at)' <<<"$MM_API_BODY")"

[[ "$(jq 'length' <<<"$USERS")" -gt 0 ]] || { echo "no agents in channel" >&2; exit 0; }

# Presence decides who is alive, so a failure here is fatal, not cosmetic.
mm_api POST "/api/v4/users/status/ids" "$(jq -c '[.[].id]' <<<"$USERS")"
[[ "$MM_API_STATUS" == "200" ]] ||
  die "could not read presence: HTTP ${MM_API_STATUS} ${MM_API_BODY}"
STATUSES="$MM_API_BODY"

jq -r --argjson st "$STATUSES" --argjson all "$ALL" '
  ($st | map({(.user_id): .status}) | add // {}) as $s
  | .[]
  | select($all == 1 or ($s[.id] // "offline") != "offline")
  | [ .username,
      (.create_at / 1000 | floor | todate),
      ($s[.id] // "?"),
      ((.props.customStatus // "" | if . == "" then {} else fromjson end).text // "")
    ]
  | @tsv' <<<"$USERS"

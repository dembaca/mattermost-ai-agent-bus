#!/usr/bin/env bash
# Coordination overhead per plugin version (RT-6.9), from the STATS lines that
# every session posts to MM_STATS_CHANNEL at its end.
#
#   mm-agent-stats.sh report [channel]   # default: MM_STATS_CHANNEL, else agents-stats
#   mm-agent-stats.sh report --json      # the aggregated numbers as JSON
#
# Columns: sessions, hours on the bus, turns the plugin caused per hour
# (listener wake-ups + rounds the Stop hook forced), their share of all turns,
# bus data put into the context per turn (tool results + compaction handoff),
# characters posted per session, and the fixed text in every turn (skill,
# instructions, tool docs). Characters, not tokens: roughly 4 per token.
# Read-only; needs a bot token and reads at most the last 2000 posts.
set -euo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

CMD="${1:-}" CHANNEL="" JSON=0
[[ "$CMD" == report ]] || { sed -n '2,15p' "$0"; exit 2; }
shift
for a in "$@"; do
  case "$a" in
    --json) JSON=1 ;;
    *) CHANNEL="$a" ;;
  esac
done
CHANNEL="${CHANNEL:-${MM_STATS_CHANNEL:-agents-stats}}"
CHANNEL="${CHANNEL#\~}"
[[ "$CHANNEL" =~ ^(off|none|0|false)$ ]] && { echo "stats are off (MM_STATS_CHANNEL=$CHANNEL); pass a channel" >&2; exit 2; }

CID="$(mm_work_channel_id "$CHANNEL")" || exit 1
# A non-member cannot read a channel; joining a public one is harmless.
if [[ -n "${MM_BOT_USER_ID:-}" ]]; then
  mm_api POST "/api/v4/channels/${CID}/members" "{\"user_id\":\"${MM_BOT_USER_ID}\"}" >/dev/null || true
fi

lines="[]"
for page in 0 1 2 3 4 5 6 7 8 9; do
  mm_api GET "/api/v4/channels/${CID}/posts?per_page=200&page=${page}" || break
  [[ "$MM_API_STATUS" == "200" ]] || { echo "could not read ~${CHANNEL}: HTTP ${MM_API_STATUS}" >&2; exit 1; }
  got="$(jq -c '[.posts // {} | .[] | .message | select(startswith("STATS "))
                 | ltrimstr("STATS ") | (try fromjson catch empty)]' <<<"$MM_API_BODY")"
  lines="$(jq -c --argjson a "$lines" --argjson b "$got" -n '$a + $b')"
  [[ "$(jq '.order | length' <<<"$MM_API_BODY")" -eq 200 ]] || break
done

report="$(jq -c '
  def sum(f): map(f) | add // 0;
  group_by(.v) | map({
    version: .[0].v,
    sessions: length,
    hours: (sum(.min) / 60 * 10 | round / 10),
    turns: sum(.turns),
    plugin_turns: sum(((.wakes // {}) | add // 0) + ((.blocks // {}) | add // 0)),
    bus_chars: sum(((.tools // {}) | map(.[1]) | add // 0) + ((.compact // [0, 0])[1])),
    post_chars: sum((.posts // [0, 0])[1]),
    static: (map(.static) | max)
  } | . + {
    plugin_turns_per_h: (if .hours > 0 then (.plugin_turns / .hours * 10 | round / 10) else null end),
    plugin_share_pct: (if .turns > 0 then (.plugin_turns * 100 / .turns | round) else null end),
    bus_chars_per_turn: (if .turns > 0 then (.bus_chars / .turns | round) else null end),
    post_chars_per_session: (.post_chars / .sessions | round)
  }) | sort_by(.version | split(".") | map(tonumber? // 0))' <<<"$lines")"

if [[ "$JSON" -eq 1 ]]; then
  printf '%s\n' "$report"
  exit 0
fi
[[ "$(jq length <<<"$report")" -gt 0 ]] || { echo "no STATS posts in ~${CHANNEL} yet"; exit 0; }
{
  printf 'version\tsessions\thours\tplugin turns/h\tshare of turns\tbus chars/turn\tposted chars/session\tfixed chars/turn\n'
  jq -r '.[] | [.version, .sessions, .hours, (.plugin_turns_per_h // "–"),
                ((.plugin_share_pct // "–") | tostring + "%"), (.bus_chars_per_turn // "–"),
                .post_chars_per_session, .static] | @tsv' <<<"$report"
} | column -t -s $'\t'

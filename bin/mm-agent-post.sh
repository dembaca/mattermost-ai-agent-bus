#!/usr/bin/env bash
# Post as this session's bot, through the session's MCP server (RT-6.17).
#
#   mm-agent-post.sh [--channel ID] [--root ID] [--to AGENT|all] [--kind WORD] MESSAGE
#   mm-agent-post.sh CHANNEL_ID MESSAGE [ROOT_ID]      # the form before 0.40.0
#
# MESSAGE "-" reads it from stdin. Without --channel the post goes to the work
# channel (MM_PROJECT_CHANNEL, else MM_CHANNEL); with --root it is a reply in
# that thread (in the root's channel). The server posts it with the code of
# post_message / reply_in_thread: humans' @-mentions become plain names,
# @channel/@here/@all plain words, --kind leads the text and goes into the
# props, --to all wakes every agent, --to AGENT is a DM, 👑 while this agent
# holds the crown. Prints the server's answer (JSON: id, channel, author, at,
# message, and hints such as unmentioned). A refused post (posted: false)
# goes to stderr, exit 1. No token here: without a reachable server of this
# session, exit 3 and what to do.
set -uo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

usage() {
  echo "usage: $0 [--channel ID] [--root ID] [--to AGENT|all] [--kind WORD] MESSAGE|-" >&2
  echo "       $0 CHANNEL_ID MESSAGE [ROOT_ID]" >&2
  exit 2
}

CHANNEL_ID="" ROOT_ID="" TO="" KIND=""
args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --channel) [[ $# -ge 2 ]] || usage; CHANNEL_ID="$2"; shift 2 ;;
    --root) [[ $# -ge 2 ]] || usage; ROOT_ID="$2"; shift 2 ;;
    --to) [[ $# -ge 2 ]] || usage; TO="$2"; shift 2 ;;
    --kind) [[ $# -ge 2 ]] || usage; KIND="$2"; shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    --) shift; args+=("$@"); break ;;
    -?*) usage ;;
    *) args+=("$1"); shift ;;
  esac
done
case "${#args[@]}" in
  1) MESSAGE="${args[0]}" ;;
  2|3) CHANNEL_ID="${args[0]}"; MESSAGE="${args[1]}"; ROOT_ID="${args[2]:-$ROOT_ID}" ;;
  0) [[ -n "$KIND" ]] || usage; MESSAGE="" ;;
  *) usage ;;
esac
[[ "$MESSAGE" == "-" ]] && MESSAGE="$(cat)"
command -v jq >/dev/null 2>&1 || { echo "mm-agent-post.sh: jq is required" >&2; exit 2; }

body="$(jq -nc --arg m "$MESSAGE" --arg c "$CHANNEL_ID" --arg r "$ROOT_ID" --arg t "$TO" --arg k "$KIND" '
  {message: $m} + ({channel_id: $c, root_id: $r, to: $t, kind: $k} | with_entries(select(.value != "")))')"
route=/v1/post
[[ -n "$ROOT_ID" ]] && route=/v1/reply
# Posting looks names up and may build a table: more than a hook's 3 s.
if ! MM_BUS_MAX_TIME="${MM_BUS_MAX_TIME:-30}" mm_bus POST "$route" "$body"; then
  [[ "$MM_BUS_STATUS" == 400 ]] && { mm_bus_explain; exit 1; }
  mm_bus_explain
  exit 3
fi
if [[ "$(jq -r '.posted' <<<"$MM_BUS_BODY")" == false ]]; then
  jq . <<<"$MM_BUS_BODY" >&2
  exit 1
fi
jq . <<<"$MM_BUS_BODY"

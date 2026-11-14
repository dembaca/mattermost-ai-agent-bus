#!/usr/bin/env bash
# Post as this session's bot, through the session's MCP server (RT-6.17).
#
#   mm-agent-post.sh [--channel ID|NAME] [--root ID] [--to AGENT|all] [--kind WORD]
#                    [--file PATH]... MESSAGE
#   mm-agent-post.sh CHANNEL_ID MESSAGE [ROOT_ID]      # the form before 0.40.0
#
# Only --channel, --root, --to, --kind, --file, -h/--help and "--" are options: a
# message may start with "-" ("- done", "-1", "---"); one that is exactly an
# option's name goes after "--". The 0.36 form is taken when the first
# argument is a channel id followed by a message: its MESSAGE and ROOT_ID are
# never read as options. MESSAGE "-" reads it from stdin, in the new form
# only (in the 0.36 form it is the text "-"). Without --channel the post goes to the home
# (the first MM_PROJECT_CHANNEL entry, else MM_CHANNEL); --channel NAME names the home, a
# guest channel or the lobby (RT-6.22), any other channel is refused and nothing is
# joined; with --root it is a reply in that thread (in the root's channel). The server posts it with the code of
# post_message / reply_in_thread: humans' @-mentions become plain names,
# @channel/@here/@all plain words, --kind leads the text and goes into the
# props, --to all wakes every agent, --to AGENT is a DM, 👑 while this agent
# holds the crown. Prints the server's answer (JSON: id, channel, author, at,
# message, and hints such as unmentioned). --file PATH (up to 10) attaches a
# file (RT-6.33): this script reads it with your own rights and sends its bytes
# to the server, which checks the type by its bytes (png, jpeg, gif, webp, pdf;
# txt, md, csv, tsv, json, log, yaml as UTF-8), the name's extension must match,
# at most 10 MB, and uploads it into the post's channel; with a file MESSAGE
# may be "". A refused post or file (posted: false, or the server's error)
# goes to stderr, exit 1. No token here: without a reachable server of this
# session, exit 3 and what to do. Exit 2: usage.
set -uo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

usage() {
  echo "usage: $0 [--channel ID|NAME] [--root ID] [--to AGENT|all] [--kind WORD] [--file PATH]... MESSAGE|-" >&2
  echo "       $0 CHANNEL_ID MESSAGE [ROOT_ID]" >&2
  exit 2
}

CHANNEL_ID="" CHANNEL_NAME="" ROOT_ID="" TO="" KIND="" OLD_FORM=""
args=() FILES=()
while [[ $# -gt 0 ]]; do
  # The 0.36 form: a channel id first, then the message (and root), all literal.
  if [[ ${#args[@]} -eq 0 && $# -ge 2 && $# -le 3 && "$1" =~ ^[a-z0-9]{26}$ ]]; then
    OLD_FORM=1; args=("$@"); break
  fi
  case "$1" in
    --channel) [[ $# -ge 2 ]] || usage; CHANNEL_ID="$2"; shift 2 ;;
    --root) [[ $# -ge 2 ]] || usage; ROOT_ID="$2"; shift 2 ;;
    --to) [[ $# -ge 2 ]] || usage; TO="$2"; shift 2 ;;
    --kind) [[ $# -ge 2 ]] || usage; KIND="$2"; shift 2 ;;
    --file) [[ $# -ge 2 ]] || usage; FILES+=("$2"); shift 2 ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    --) shift; args+=("$@"); break ;;
    *) args+=("$1"); shift ;;
  esac
done
case "${#args[@]}" in
  1) MESSAGE="${args[0]}" ;;
  2|3) [[ "${args[0]}" == -* ]] && usage  # no channel starts with "-": a mistyped option
    OLD_FORM=1; CHANNEL_ID="${args[0]}"; MESSAGE="${args[1]}"; ROOT_ID="${args[2]:-$ROOT_ID}" ;;
  0) [[ -n "$KIND" || ${#FILES[@]} -gt 0 ]] || usage; MESSAGE="" ;;
  *) usage ;;
esac
[[ "$MESSAGE" == "-" && -z "$OLD_FORM" ]] && MESSAGE="$(cat)"
# --channel NAME: the server resolves it among the bot's channels (RT-6.22).
if [[ -n "$CHANNEL_ID" && ! "$CHANNEL_ID" =~ ^[a-z0-9]{26}$ ]]; then
  CHANNEL_NAME="$CHANNEL_ID" CHANNEL_ID=""
  [[ -n "$ROOT_ID" ]] && { echo "mm-agent-post.sh: a reply stays in its root's channel: --channel NAME is for a new post" >&2; exit 2; }
fi
command -v jq >/dev/null 2>&1 || { echo "mm-agent-post.sh: jq is required" >&2; exit 2; }
(( ${#FILES[@]} <= 10 )) || { echo "mm-agent-post.sh: at most 10 files per post" >&2; exit 2; }
for f in "${FILES[@]}"; do
  [[ -f "$f" && -r "$f" ]] || { echo "mm-agent-post.sh: --file $f: no readable file" >&2; exit 2; }
done

# RT-6.33: each file to the server first, into the channel the post goes to.
ids=()
if (( ${#FILES[@]} )); then
  where=""
  if [[ -n "$ROOT_ID" ]]; then where="root=$ROOT_ID"
  elif [[ -n "$TO" && "$TO" != all ]]; then where="to=$(jq -rn --arg v "$TO" '$v|@uri')"
  elif [[ -n "$CHANNEL_ID$CHANNEL_NAME" ]]; then where="channel=$(jq -rn --arg v "$CHANNEL_ID$CHANNEL_NAME" '$v|@uri')"
  fi
  for f in "${FILES[@]}"; do
    q="name=$(jq -rn --arg v "${f##*/}" '$v|@uri')${where:+&$where}"
    if ! MM_BUS_UPLOAD="$f" MM_BUS_MAX_TIME="${MM_BUS_MAX_TIME:-120}" mm_bus POST "/v1/files?$q"; then
      mm_bus_explain
      case "$MM_BUS_STATUS" in 400|403|413|422) exit 1 ;; *) exit 3 ;; esac
    fi
    ids+=("$(jq -r '.file_id' <<<"$MM_BUS_BODY")")
  done
fi

body="$(jq -nc --arg m "$MESSAGE" --arg c "$CHANNEL_ID" --arg n "$CHANNEL_NAME" --arg r "$ROOT_ID" --arg t "$TO" --arg k "$KIND" \
  --args '
  {message: $m} + ({channel_id: $c, channel: $n, root_id: $r, to: $t, kind: $k} | with_entries(select(.value != "")))
  + (if ($ARGS.positional | length) > 0 then {files: $ARGS.positional} else {} end)' ${ids[@]+"${ids[@]}"})"
route=/v1/post
[[ -n "$ROOT_ID" ]] && route=/v1/reply
# Posting looks names up and may build a table: more than a hook's 3 s.
if ! MM_BUS_MAX_TIME="${MM_BUS_MAX_TIME:-30}" mm_bus POST "$route" "$body"; then
  [[ "$MM_BUS_STATUS" == 400 ]] && { mm_bus_explain; exit 1; }  # also: not a channel of the bot
  mm_bus_explain
  exit 3
fi
if [[ "$(jq -r '.posted' <<<"$MM_BUS_BODY")" == false ]]; then
  jq . <<<"$MM_BUS_BODY" >&2
  exit 1
fi
jq . <<<"$MM_BUS_BODY"

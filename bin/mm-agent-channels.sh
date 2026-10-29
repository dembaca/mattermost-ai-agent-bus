#!/usr/bin/env bash
# Resolve the channels this agent uses, and make sure it can post in them.
#
#   eval "$(./bin/mm-agent-channels.sh resolve)"
#
# Emits export lines for MM_TEAM_ID, MM_CHANNEL_ID and — when MM_PROJECT_CHANNEL
# is set — MM_PROJECT_CHANNEL (the list as read) and MM_PROJECT_CHANNEL_ID (the
# home's id).
#
# The roles (ADR-002, RT-6.22):
#   MM_CHANNEL          the registrar's default channel, the lobby; the agent is
#                       already a member (ensureTeamChannel adds it at register
#                       time) and stays addressable there.
#   MM_PROJECT_CHANNEL  optional, comma-separated. The first entry is the home,
#                       where this session works; the others are guest
#                       channels. Not known to the registrar, so the bot joins
#                       them here. The home must resolve and join; a guest that
#                       does not is reported once on stderr and left out.
#
# This script NEVER creates a team or a channel. A missing one is reported with
# the name that is missing, so the operator can create it deliberately.
set -euo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

CMD="${1:-resolve}"

die() {
  echo "$1" >&2
  exit 1
}

# Resolve a channel name within the team; prints the id. 404 is fatal on purpose.
resolve_channel() {
  local cname="$1" label="$2" tid="$3" team="$4"
  mm_api GET "/api/v4/teams/${tid}/channels/name/${cname}"
  case "$MM_API_STATUS" in
    200) jq -r '.id' <<<"$MM_API_BODY" ;;
    404)
      die "${label} '${cname}' does not exist in team '${team}' — create it in Mattermost first (this tool never creates channels)"
      ;;
    *) die "could not resolve ${label} '${cname}': HTTP ${MM_API_STATUS} ${MM_API_BODY}" ;;
  esac
}

# Join an existing channel. Joining is not creating, but Mattermost checks
# create_post against membership — a non-member gets 403 even in a public
# channel.
#
# Deliberately no membership pre-check first: reading a channel's member list
# needs read_channel, which a non-member does not have, so Mattermost answers
# 403 — not 404 — for exactly the case such a check exists to detect. It can
# never succeed. Joining is idempotent (201 even when already a member), so just
# join.
ensure_member() {
  local cid="$1" cname="$2" uid="$3"
  mm_api POST "/api/v4/channels/${cid}/members" \
    "$(jq -nc --arg u "$uid" '{user_id:$u}')"
  case "$MM_API_STATUS" in
    200|201) echo "member of ${cname}" >&2 ;;
    403)
      die "cannot join '${cname}': either it is private and a human must add the bot, or this instance denies join_public_channels"
      ;;
    *) die "could not join '${cname}': HTTP ${MM_API_STATUS} ${MM_API_BODY}" ;;
  esac
}

# Join a guest channel (RT-6.22): like resolve_channel and ensure_member, but a
# failure is reported and returns 1 instead of ending the session start.
join_guest() {
  local cname="$1" tid="$2" uid="$3" gid why=""
  mm_api GET "/api/v4/teams/${tid}/channels/name/${cname}"
  case "$MM_API_STATUS" in
    200) gid="$(jq -r '.id' <<<"$MM_API_BODY")" ;;
    404) why="does not exist (this tool never creates channels)" ;;
    *) why="HTTP ${MM_API_STATUS}" ;;
  esac
  if [[ -z "$why" ]]; then
    mm_api POST "/api/v4/channels/${gid}/members" "$(jq -nc --arg u "$uid" '{user_id:$u}')"
    case "$MM_API_STATUS" in
      200|201) echo "member of guest channel ${cname}" >&2; return 0 ;;
      403) why="private (a human must add the bot) or joining is denied" ;;
      *) why="HTTP ${MM_API_STATUS}" ;;
    esac
  fi
  echo "guest channel '${cname}' not joined: ${why}; the session goes on" >&2
  return 1
}

case "$CMD" in
  resolve)
    TEAM="$(mm_team)"
    CHANNEL="$(mm_channel)"
    PROJECT=""
    GUESTS=()
    while IFS= read -r entry; do
      if [[ -z "$PROJECT" ]]; then PROJECT="$entry"; else GUESTS+=("$entry"); fi
    done < <(mm_project_channels)

    mm_api GET "/api/v4/users/me"
    [[ "$MM_API_STATUS" == "200" ]] ||
      die "bot token rejected: HTTP ${MM_API_STATUS} ${MM_API_BODY}"
    USER_ID="$(jq -r '.id' <<<"$MM_API_BODY")"

    mm_api GET "/api/v4/teams/name/${TEAM}"
    case "$MM_API_STATUS" in
      200) TEAM_ID="$(jq -r '.id' <<<"$MM_API_BODY")" ;;
      404) die "team '${TEAM}' does not exist" ;;
      *) die "could not resolve team '${TEAM}': HTTP ${MM_API_STATUS} ${MM_API_BODY}" ;;
    esac

    # Default channel: membership comes from the registrar, so only resolve it.
    # A 404 here usually means MM_CHANNEL and the registrar's
    # DEFAULT_CHANNEL_NAME disagree.
    CHANNEL_ID="$(resolve_channel "$CHANNEL" "default channel" "$TEAM_ID" "$TEAM")"

    # Buffer the exports: callers use eval "$(…)", which would otherwise pick up
    # a half-resolved environment when a later step fails.
    OUT="export MM_TEAM_ID=$(printf '%q' "$TEAM_ID")"
    OUT+=$'\n'"export MM_CHANNEL_ID=$(printf '%q' "$CHANNEL_ID")"

    if [[ -n "$PROJECT" ]]; then
      PROJECT_ID="$(resolve_channel "$PROJECT" "project channel" "$TEAM_ID" "$TEAM")"
      ensure_member "$PROJECT_ID" "$PROJECT" "$USER_ID"
      LIST="$PROJECT"
      JOINED=""
      for guest in "${GUESTS[@]+"${GUESTS[@]}"}"; do
        LIST+=",${guest}"
        join_guest "$guest" "$TEAM_ID" "$USER_ID" && JOINED+=" #${guest}"
      done
      OUT+=$'\n'"export MM_PROJECT_CHANNEL=$(printf '%q' "$LIST")"
      OUT+=$'\n'"export MM_PROJECT_CHANNEL_ID=$(printf '%q' "$PROJECT_ID")"
    fi

    printf '%s\n' "$OUT"
    echo "channels resolved: lobby #${CHANNEL}${PROJECT:+, home #${PROJECT}}${JOINED:+, guests${JOINED}} in ${TEAM}" >&2
    ;;
  *)
    echo "usage: $0 resolve" >&2
    echo "  requires MM_BOT_TOKEN; uses MM_TEAM / MM_CHANNEL / MM_PROJECT_CHANNEL" >&2
    exit 2
    ;;
esac

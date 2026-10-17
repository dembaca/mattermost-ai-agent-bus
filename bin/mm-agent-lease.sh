#!/usr/bin/env bash
# Coordinator crown and presence lease (RT-6.6 in docs/arc42/06-runtime-view.md).
#
#   mm-agent-lease.sh tick       # renew own status; claim or yield the crown
#   mm-agent-lease.sh take       # take the crown now, or as soon as it is free
#   mm-agent-lease.sh release    # give the crown up (after a HANDOVER is ACKed)
#   mm-agent-lease.sh who        # print the current coordinator's username
#
# The coordinator is whoever wears a live 👑 custom status in the work channel.
# Nobody else can remove a status, so the crown is a lease: mm-agent-status.sh
# extends it while this agent is reachable and lets it run out while idle or
# dead. The MCP server runs `tick` every few minutes.
#
# tick, in order:
#   1. renew the own status (mm-agent-status.sh renew)
#   2. two crowns (a race): the older agent keeps it, the other yields
#   3. no live crown anywhere: take it if a take is pending (handover), or if
#      this agent is the oldest reachable one — unless it released the crown
#      recently, which leaves room for the agent it handed over to.
#
# Fails open and silent like the other status scripts: exit 0 on any API error.
set -uo pipefail

BIN="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=mm-agent-lib.sh
source "$BIN/mm-agent-lib.sh"

CMD="${1:-tick}"
case "$CMD" in
  tick|take|release|who) ;;
  -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
  *) echo "usage: $0 tick|take|release|who" >&2; exit 2 ;;
esac

[[ -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]] || exit 0
command -v jq >/dev/null 2>&1 || exit 0

if [[ -z "${MM_BOT_USER_ID:-}" ]]; then
  mm_api GET "/api/v4/users/me" && [[ "$MM_API_STATUS" == "200" ]] || exit 0
  MM_BOT_USER_ID="$(jq -r '.id' <<<"$MM_API_BODY")"
  export MM_BOT_USER_ID
fi
ME="$MM_BOT_USER_ID"
COORD="$(mm_bot_state_file coord "$ME")" || exit 0
LEASE="$(mm_lease_sec)"
NOW="$(date +%s)"

CROWN_UNTIL=0 PENDING=0 YIELD_UNTIL=0
if [[ -f "$COORD" ]]; then
  while IFS='=' read -r k v; do
    [[ "$v" =~ ^[0-9]+$ ]] || continue
    case "$k" in
      CROWN_UNTIL) CROWN_UNTIL="$v" ;; PENDING) PENDING="$v" ;; YIELD_UNTIL) YIELD_UNTIL="$v" ;;
    esac
  done <"$COORD"
fi

save() {
  mkdir -p "$(dirname "$COORD")" 2>/dev/null || return 0
  printf 'CROWN_UNTIL=%s\nPENDING=%s\nYIELD_UNTIL=%s\n' \
    "$CROWN_UNTIL" "$PENDING" "$YIELD_UNTIL" >"$COORD" 2>/dev/null || true
}
render() { "$BIN/mm-agent-status.sh" renew >/dev/null 2>&1 || true; }

CID="$(mm_work_channel_id 2>/dev/null)" || exit 0

if [[ "$CMD" == tick ]]; then
  render
  # Re-read: the renewal may have let an idle crown run out.
  v="$(sed -n 's/^CROWN_UNTIL=//p' "$COORD" 2>/dev/null | head -1)"
  [[ "$v" =~ ^[0-9]+$ ]] && CROWN_UNTIL="$v"
fi

mm_roster_json "$CID" || exit 0
# Other agents wearing a live crown, oldest first.
others="$(jq -c --arg me "$ME" '[.[] | select(.crown and .id != $me)]' <<<"$ROSTER_JSON")"
n_others="$(jq 'length' <<<"$others")"
holding=0; [[ "$CROWN_UNTIL" -gt "$NOW" ]] && holding=1

take_now() {
  CROWN_UNTIL=$((NOW + LEASE)) PENDING=0 YIELD_UNTIL=0
  save
  render
}

case "$CMD" in
  who)
    jq -r '(map(select(.crown)) + map(select(.reachable)))[0].username // empty' <<<"$ROSTER_JSON"
    ;;
  take)
    if [[ "$n_others" -eq 0 ]]; then
      take_now
      echo "taken"
    else
      PENDING=1
      save
      echo "pending: @$(jq -r '.[0].username' <<<"$others") still holds the crown; it passes to you once released or expired"
    fi
    ;;
  release)
    CROWN_UNTIL=0 PENDING=0 YIELD_UNTIL=$((NOW + 2 * LEASE))
    save
    render
    echo "released"
    ;;
  tick)
    if [[ "$holding" -eq 1 && "$n_others" -gt 0 ]]; then
      mine="$(jq -r --arg me "$ME" '.[] | select(.id == $me) | .create_at' <<<"$ROSTER_JSON")"
      theirs="$(jq -r '.[0].create_at' <<<"$others")"
      if [[ "$mine" =~ ^[0-9]+$ && "$theirs" =~ ^[0-9]+$ && "$theirs" -lt "$mine" ]]; then
        CROWN_UNTIL=0
        save
        render
      fi
    elif [[ "$holding" -eq 0 && "$n_others" -eq 0 && "$YIELD_UNTIL" -le "$NOW" ]]; then
      me_reachable="$(jq -r --arg me "$ME" 'any(.[]; .id == $me and .reachable)' <<<"$ROSTER_JSON")"
      oldest="$(jq -r 'map(select(.reachable))[0].id // empty' <<<"$ROSTER_JSON")"
      if [[ "$me_reachable" == true ]] && [[ "$PENDING" -eq 1 || "$oldest" == "$ME" ]]; then
        take_now
      fi
    fi
    ;;
esac

exit 0

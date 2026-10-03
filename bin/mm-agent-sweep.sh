#!/usr/bin/env bash
# Clean up agents a failed teardown left behind.
#
#   mm-agent-sweep.sh            # dry run: list what would be removed
#   mm-agent-sweep.sh --apply    # actually unregister
#
# Two sources, deliberately in this order:
#
#   1. Leftover session files. Each holds the bot's own token, which the
#      registrar accepts for deleting that bot — so this works with no
#      MM_REG_SECRET at all, which is the normal case on a gopass setup.
#   2. With MM_REG_SECRET set, also every agent-* account in the team that has
#      no session file. That catches bots whose session file was already lost.
#
# Runs on the HOST: it needs the session files, which never enter a sandbox.
set -uo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
BIN="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
removed=0
kept=0

note() { echo "$*" >&2; }

# --- 1. session files --------------------------------------------------------
shopt -s nullglob
for f in "$STATE_DIR"/*.env; do
  name="$(sed -n 's/^MM_BOT_NAME=//p' "$f" | head -n1)"
  [[ -n "$name" ]] || { note "skip $f (no MM_BOT_NAME)"; continue; }
  if [[ "$APPLY" -eq 0 ]]; then
    echo "would unregister ${name}  (from $(basename "$f"))"
    continue
  fi
  if (
      export MM_AGENT_SESSION_FILE="$f"
      mm_load_session_file || exit 1
      exec "$BIN/mm-agent-unregister.sh" "$name" >/dev/null 2>&1
     ); then
    note "unregistered ${name}"
    rm -f "$f"
    removed=$((removed + 1))
  else
    note "FAILED ${name} — keeping $(basename "$f")"
    kept=$((kept + 1))
  fi
done
shopt -u nullglob

# --- 2. orphans with no session file ----------------------------------------
# Needs the registration secret: a bot's own token can only delete that bot, and
# for these we no longer have it.
if [[ -n "${MM_REG_SECRET:-}" && -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]]; then
  team="${MM_TEAM_ID:-}"
  if [[ -z "$team" ]]; then
    mm_api GET "/api/v4/teams/name/$(mm_team)" &&
      [[ "$MM_API_STATUS" == "200" ]] &&
      team="$(jq -r '.id' <<<"$MM_API_BODY")"
  fi
  if [[ -n "$team" ]] && mm_api GET "/api/v4/users?in_team=${team}&per_page=200" &&
     [[ "$MM_API_STATUS" == "200" ]]; then
    while read -r uname; do
      [[ -n "$uname" ]] || continue
      short="${uname#agent-}"
      # Never remove the agent running this sweep.
      [[ "$short" == "${MM_BOT_NAME:-}" ]] && continue
      [[ "$uname" == "${MM_BOT_USERNAME:-}" ]] && continue
      if [[ "$APPLY" -eq 0 ]]; then
        echo "would unregister ${uname}  (orphan, no session file)"
        continue
      fi
      if "$BIN/mm-agent-unregister.sh" "$short" >/dev/null 2>&1; then
        note "unregistered ${uname}"
        removed=$((removed + 1))
      else
        note "FAILED ${uname}"
        kept=$((kept + 1))
      fi
    done < <(jq -r '.[] | select(.username | startswith("agent-")) | select(.delete_at == 0) | .username' <<<"$MM_API_BODY")
  fi
elif [[ "$APPLY" -eq 1 ]]; then
  note "(set MM_REG_SECRET to also sweep orphans that have no session file)"
fi

if [[ "$APPLY" -eq 1 ]]; then
  note "sweep done: ${removed} removed, ${kept} still present"
else
  note "dry run — re-run with --apply to remove"
fi

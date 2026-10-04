#!/usr/bin/env bash
# Ephemeral Mattermost agent session: start (register) / stop (unregister).
# Usage:
#   eval "$(./bin/mm-agent-session.sh start [short-name])"
#   ./bin/mm-agent-session.sh stop
set -euo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
BIN="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

CMD="${1:-}"
NAME="${2:-}"

case "$CMD" in
  start)
    mm_require_reg_secret
    EXPLICIT_NAME=1
    if [[ -z "$NAME" ]]; then
      EXPLICIT_NAME=0
      host="$(hostname -s 2>/dev/null || hostname | cut -d. -f1)"
      host="$(printf '%s' "$host" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-' | cut -c1-12)"
      NAME="${host:-agent}-$$"
      NAME="$(printf '%s' "$NAME" | tr -cd 'a-z0-9-' | cut -c1-32)"
      if [[ ${#NAME} -lt 3 ]]; then
        NAME="agt-$$"
      fi
    fi
    # A name stays taken at the registrar even after its bot is removed (409), so
    # a second session on the host — or a restart — would be locked out: append
    # a registration suffix to an explicit name (the default already has $$).
    # Opt out with MM_AGENT_NAME_EXACT=1.
    if [[ "$EXPLICIT_NAME" == "1" && "${MM_AGENT_NAME_EXACT:-0}" != "1" ]]; then
      sid="$(printf '%s' "${CLAUDE_CODE_SESSION_ID:-${CLAUDE_SESSION_ID:-}}" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9' | cut -c1-5)"
      sid+="$(od -An -N2 -tx1 /dev/urandom | tr -d ' \n')"
      NAME="$(printf '%s' "${NAME:0:$((31 - ${#sid}))}" | sed 's/-*$//')-${sid}"
    fi
    # shellcheck disable=SC1090
    eval "$("${BIN}/mm-agent-register.sh" --ephemeral --exports "$NAME")"
    mm_write_session_file
    mm_emit_session_exports
    echo "session started as ${MM_BOT_USERNAME}" >&2
    ;;
  stop)
    mm_load_session_file || true
    if [[ -z "${MM_BOT_NAME:-}" ]]; then
      echo "no MM_BOT_NAME / session file; nothing to stop" >&2
      exit 0
    fi
    "${BIN}/mm-agent-unregister.sh" "$MM_BOT_NAME" || true
    rm -f "$(mm_session_file)"
    cat <<'EOF'
unset MM_BOT_NAME MM_BOT_USERNAME MM_BOT_USER_ID MM_BOT_TOKEN MATTERMOST_TOKEN
EOF
    echo "session stopped" >&2
    ;;
  *)
    echo "usage: $0 start [short-name] | stop" >&2
    echo "  start: eval \"\$($0 start [name])\"   # requires MM_REG_SECRET" >&2
    exit 2
    ;;
esac

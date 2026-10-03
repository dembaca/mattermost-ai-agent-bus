#!/usr/bin/env bash
# Delete a Mattermost agent bot via the registrar.
# Auth: MM_REG_SECRET (preferred) or MM_BOT_TOKEN / MATTERMOST_TOKEN.
set -euo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

NAME="${1:-${MM_BOT_NAME:-}}"
REG_URL="$(mm_register_url)"
TOKEN="${MM_REG_SECRET:-${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}}"

if [[ -z "$NAME" ]]; then
  echo "usage: $0 <short-name>" >&2
  echo "  or set MM_BOT_NAME; requires MM_REG_SECRET or MM_BOT_TOKEN" >&2
  exit 2
fi
NAME="${NAME#agent-}"

if [[ -z "$TOKEN" ]]; then
  echo "MM_REG_SECRET or MM_BOT_TOKEN required" >&2
  exit 2
fi

# Status on its own trailing line instead of a temp file: unregister must still
# work where TMPDIR is not writable (e.g. inside a sandbox), otherwise teardown
# fails silently and leaves bot accounts behind.
RESP="$(curl -sS -w $'\n%{http_code}' -X DELETE \
  "${REG_URL}/${NAME}" \
  -H "$(mm_auth_header "$TOKEN")" || true)"
CODE="${RESP##*$'\n'}"
BODY="${RESP%$'\n'*}"

if [[ "$CODE" != "200" && "$CODE" != "204" && "$CODE" != "404" ]]; then
  echo "unregister failed http=${CODE}: ${BODY}" >&2
  exit 1
fi
echo "unregistered agent-${NAME} (http=${CODE})"

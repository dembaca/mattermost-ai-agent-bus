#!/usr/bin/env bash
# Show the registrar's public keys for pinning (RT-6.24, arc42 §7.3).
#
# Reads GET <registrar>/register/v1/keys (from MM_REGISTER_URL, else
# MM_CHAT_URL) and prints each key's `x` (base64url raw Ed25519 public key)
# with its kid, the kid computed here from `x` when openssl is at hand. The bus
# never trusts this endpoint at runtime: compare the values with the
# registrar's operator out of band, then pin them yourself in
# MM_REGISTRAR_KEYS. This script writes no configuration.
set -euo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

case "${1:-}" in
  -h | --help)
    echo "usage: $0    (uses MM_REGISTER_URL, else MM_CHAT_URL)" >&2
    exit 2 ;;
esac

REG_URL="$(mm_register_url)"
KEYS_URL="${REG_URL%/agents}/keys"
[[ "$KEYS_URL" != "$REG_URL" ]] || KEYS_URL="${REG_URL%/}/keys"

if ! BODY="$(curl -fsS --max-time 20 "$KEYS_URL")"; then
  echo "mm-agent-registrar-keys: could not read ${KEYS_URL}" >&2
  exit 1
fi

# The kid of a raw public key: the first 16 characters of
# base64url(sha256(key)), without padding (mcp/.../attest.py kid_of).
kid_of() {
  local x="$1" pad
  pad=$(( (4 - ${#x} % 4) % 4 ))
  x="$(printf '%s' "$x" | tr '_-' '/+')"
  [[ "$pad" -eq 0 ]] || x+="$(printf '%*s' "$pad" '' | tr ' ' '=')"
  printf '%s\n' "$x" | openssl base64 -d -A | openssl dgst -sha256 -binary |
    openssl base64 -A | tr '+/' '-_' | tr -d '=' | cut -c1-16
}

ROWS="$(printf '%s' "$BODY" | jq -r '
  .keys[]? | select(.kty == "OKP" and .crv == "Ed25519") | [(.kid // ""), (.x // "")] | @tsv' 2>/dev/null)" || ROWS=""
if [[ -z "$ROWS" ]]; then
  echo "mm-agent-registrar-keys: ${KEYS_URL} lists no Ed25519 key" >&2
  exit 1
fi

bad=0
pins=""
while IFS=$'\t' read -r kid x; do
  if ! [[ "$x" =~ ^[A-Za-z0-9_-]{43}$ ]]; then
    echo "skipped: a key whose x is no 32-byte base64url value (kid ${kid:-none})" >&2
    bad=1
    continue
  fi
  own=""
  if command -v openssl >/dev/null 2>&1; then own="$(kid_of "$x")"; fi
  if [[ -n "$own" && "$own" != "$kid" ]]; then
    echo "MISMATCH: the registrar names kid ${kid:-none}, the key's own kid is ${own}: do not pin ${x}" >&2
    bad=1
    continue
  fi
  echo "kid ${kid}  x ${x}${own:+  (kid checked)}"
  pins+="${pins:+,}${x}"
done <<<"$ROWS"

[[ -n "$pins" ]] || exit 1
cat <<EOF

Compare these with the registrar's operator out of band (not over this
connection), then pin them in the host config or the MCP server's environment:

  export MM_REGISTRAR_KEYS=${pins}

Nothing was written: the bus never fetches keys at runtime.
EOF
exit "$bad"

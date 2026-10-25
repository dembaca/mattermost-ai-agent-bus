#!/usr/bin/env bash
# Install the corral host hooks where no sandbox can change them.
#
#   bin/mm-agent-host-install.sh             # from the checkout this script is in
#   mm-agent-host-install.sh --from DIR      # from another checkout you reviewed
#   mm-agent-host-install.sh --check         # compare host copy vs installed plugin
#
# Run it on the HOST, in a normal terminal — never inside corral — from a
# checkout you reviewed that no session works in. corral's preStart/postEnd
# hooks run on the host with the registration secret. corral approves the
# `exec` file by its content, but not the scripts it sources and starts (bin/).
# The installed plugin lives under the corral homes and ~/.claude, a checkout
# may be a session's workdir; a sandbox can write all of them. So the source is
# never looked up from what is installed: it is this script's own tree or
# --from, and a source under ~/.cache/corral or ~/.claude is refused. This
# copies adapters/corral/ and bin/ to MM_BUS_HOST_DIR (default
# ~/.local/share/mm-agent-bus/current), which the sandbox neither sees nor
# writes. Point corral's `exec` there once; rerun this after a plugin update.
# corral then asks to approve the changed hook — that prompt is the point.
# It also copies the MCP server (mcp/, without a checkout's .venv or caches)
# and, with uv, prepares its Python environment there (uv sync --locked): the
# start hook runs each session's MCP server on the host from this tree, so the
# bot token never enters the sandbox (RT-6.18). Without it sessions get no bus.
#
# Spec: arc42 §7.1 (docs/arc42/07-deployment-view.md).
set -euo pipefail

SELF="$(cd -P "$(dirname "$0")/.." && pwd -P)"
# shellcheck source=mm-agent-lib.sh
source "$SELF/bin/mm-agent-lib.sh"

die() { echo "mm-agent-host-install: $*" >&2; exit 1; }

FROM="" CHECK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="${2:-}"; shift 2 ;;
    --check) CHECK=1; shift ;;
    -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

DEST="${MM_BUS_HOST_DIR:-$HOME/.local/share/mm-agent-bus/current}"

version_of() { mm_tree_version "$1"; }

command -v jq >/dev/null 2>&1 || die "jq is required"

if [[ "$CHECK" -eq 1 ]]; then
  # The installed plugins include the corral homes: a version to compare, no path.
  have="$(version_of "$DEST")" want="$(mm_newest_plugin_version || true)"
  printf 'host hooks: %s (%s)\nplugin:     %s\n' "$have" "$DEST" "${want:-not found}"
  [[ -n "$want" && "$have" == "$want" ]]
  exit $?
fi

# Inside corral the target is not the host's and the source may be tampered with.
[[ -z "${CORRAL_SANDBOX:-}" ]] || die "run this on the host, not inside a corral session"

# Places a corral sandbox can write: every corral home, and ~/.claude, which the
# homes share by default. $1 is a resolved path (pwd -P).
sandbox_writable() {
  local r real
  for r in "$HOME/.cache/corral" "$HOME/.claude"; do
    real="$(cd -P "$r" 2>/dev/null && pwd -P)" || real="$r"
    [[ "$1/" == "$r/"* || "$1/" == "$real/"* ]] && return 0
  done
  return 1
}

[[ -n "$FROM" ]] || FROM="$SELF"
src="$(cd -P "$FROM" 2>/dev/null && pwd -P)" || die "no such directory: $FROM"
FROM="$src"
if sandbox_writable "$FROM"; then
  die "$FROM can be changed from inside a corral sandbox; run the installer from a checkout you reviewed, outside corral and outside every session's workdir (or pass --from <checkout>)"
fi
for f in adapters/corral/10-mm-session-start.sh adapters/corral/90-mm-session-stop.sh \
         bin/mm-agent-lib.sh .claude-plugin/plugin.json mcp/pyproject.toml mcp/uv.lock \
         mcp/src/mattermost_ai_agent_bus/hosted.py; do
  [[ -f "$FROM/$f" ]] || die "$FROM is not a mattermost-ai-agent-bus tree (missing $f)"
done
[[ "$FROM" != "$(cd -P "$DEST" 2>/dev/null && pwd -P)" ]] ||
  die "source and target are the same directory; run it from a checkout, or pass --from <checkout>"

# Copy next to the target, then swap: a session starting meanwhile sees either
# the old or the new tree, never half of one.
parent="$(dirname "$DEST")"
mkdir -p "$parent"
parent="$(cd -P "$parent" && pwd -P)"
! sandbox_writable "$parent" || die "$DEST can be changed from inside a corral sandbox; pick another MM_BUS_HOST_DIR"
DEST="$parent/$(basename "$DEST")"
new="$(mktemp -d "$parent/.install.XXXXXX")"
trap 'rm -rf "$new"' EXIT
mkdir -p "$new/adapters" "$new/.claude-plugin"
cp -R "$FROM/bin" "$new/bin"
cp -R "$FROM/adapters/corral" "$new/adapters/corral"
cp "$FROM/.claude-plugin/plugin.json" "$new/.claude-plugin/plugin.json"
# The MCP server, for the host process (RT-6.18); the skill, whose size its
# overhead stats count (RT-6.9).
mkdir -p "$new/mcp" "$new/skills"
cp "$FROM/mcp/pyproject.toml" "$FROM/mcp/uv.lock" "$new/mcp/"
if [[ -f "$FROM/mcp/README.md" ]]; then cp "$FROM/mcp/README.md" "$new/mcp/"; fi
cp -R "$FROM/mcp/src" "$new/mcp/src"
find "$new/mcp" -name __pycache__ -prune -exec rm -rf {} +
if [[ -d "$FROM/skills/mattermost-ai-agent-bus" ]]; then
  cp -R "$FROM/skills/mattermost-ai-agent-bus" "$new/skills/"
fi
chmod -R go-w "$new"
chmod +x "$new"/adapters/corral/*.sh "$new"/bin/*.sh

old=""
if [[ -e "$DEST" ]]; then
  old="$parent/.old.$$"
  mv "$DEST" "$old"
fi
mv "$new" "$DEST"
trap - EXIT
[[ -n "$old" ]] && rm -rf "$old"

# After the swap: a virtual environment does not survive a move. From the host
# tree only (--directory), never from the source or the working directory.
if command -v uv >/dev/null 2>&1 &&
  (cd "$DEST/mcp" && uv sync --locked --no-dev --quiet --directory "$DEST/mcp") >&2; then
  server="MCP server environment ready: the bot token stays on the host (RT-6.18)"
else
  server="WARNING: no Python environment for the MCP server (needs uv): corral sessions get no bus until this is rerun"
fi

cat <<EOF
Installed mattermost-ai-agent-bus $(version_of "$DEST") host hooks
  from: $FROM
  to:   $DEST
$server

corral config (~/.config/corral/config.yml), set once:

  providers:
    hooks:
      enabled: true
      preStart:
        mm-bus:
          exec: $DEST/adapters/corral/10-mm-session-start.sh
          optional: true
      postEnd:
        mm-bus:
          exec: $DEST/adapters/corral/90-mm-session-stop.sh
          optional: true

The next corral launch asks to approve the changed hook files.
EOF

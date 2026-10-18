#!/usr/bin/env bash
# Install the corral host hooks where no sandbox can change them.
#
#   mm-agent-host-install.sh                 # from the installed Claude Code plugin
#   mm-agent-host-install.sh --from DIR      # from a checkout or plugin directory
#   mm-agent-host-install.sh --check         # compare installed vs plugin version
#
# Run it on the HOST, in a normal terminal — never inside corral. corral's
# preStart/postEnd hooks run on the host with the registration secret. corral
# approves the `exec` file by its content, but not the scripts it sources and
# starts (bin/). The plugin directory lives in the sandbox's private home and a
# checkout in a sandbox's workdir; both are writable from inside a sandbox, so
# running hooks from there lets any agent put code on the host. This copies
# adapters/corral/ and bin/ to MM_BUS_HOST_DIR (default
# ~/.local/share/mm-agent-bus/current), which the sandbox neither sees nor
# writes. Point corral's `exec` there once; rerun this after a plugin update.
# corral then asks to approve the changed hook — that prompt is the point.
#
# Spec: arc42 §7.1 (docs/arc42/07-deployment-view.md).
set -euo pipefail

# shellcheck source=mm-agent-lib.sh
source "$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"

die() { echo "mm-agent-host-install: $*" >&2; exit 1; }

FROM="" CHECK=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="${2:-}"; shift 2 ;;
    --check) CHECK=1; shift ;;
    -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

DEST="${MM_BUS_HOST_DIR:-$HOME/.local/share/mm-agent-bus/current}"

version_of() { mm_tree_version "$1"; }

command -v jq >/dev/null 2>&1 || die "jq is required"

if [[ "$CHECK" -eq 1 ]]; then
  newest="$(mm_installed_plugin_dirs | head -1)"
  printf 'host hooks: %s (%s)\nplugin:     %s (%s)\n' \
    "$(version_of "$DEST")" "$DEST" "${newest:+$(version_of "$newest")}" "${newest:-not found}"
  [[ -n "$newest" && "$(version_of "$DEST")" == "$(version_of "$newest")" ]]
  exit $?
fi

# Inside corral the target is not the host's and the source may be tampered with.
[[ -z "${CORRAL_SANDBOX:-}" ]] || die "run this on the host, not inside a corral session"

if [[ -z "$FROM" ]]; then
  FROM="$(mm_installed_plugin_dirs | head -1)"
  [[ -n "$FROM" ]] || die "no installed mattermost-ai-agent-bus plugin found; use --from DIR"
fi
FROM="$(cd "$FROM" && pwd)" || die "no such directory: $FROM"
for f in adapters/corral/10-mm-session-start.sh adapters/corral/90-mm-session-stop.sh \
         bin/mm-agent-lib.sh .claude-plugin/plugin.json; do
  [[ -f "$FROM/$f" ]] || die "$FROM is not a mattermost-ai-agent-bus tree (missing $f)"
done
[[ "$FROM" != "$DEST" ]] || die "source and target are the same directory"

# Copy next to the target, then swap: a session starting meanwhile sees either
# the old or the new tree, never half of one.
parent="$(dirname "$DEST")"
mkdir -p "$parent"
new="$(mktemp -d "$parent/.install.XXXXXX")"
trap 'rm -rf "$new"' EXIT
mkdir -p "$new/adapters" "$new/.claude-plugin"
cp -R "$FROM/bin" "$new/bin"
cp -R "$FROM/adapters/corral" "$new/adapters/corral"
cp "$FROM/.claude-plugin/plugin.json" "$new/.claude-plugin/plugin.json"
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

cat <<EOF
Installed mattermost-ai-agent-bus $(version_of "$DEST") host hooks
  from: $FROM
  to:   $DEST

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

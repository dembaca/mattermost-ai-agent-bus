#!/usr/bin/env bash
# Install the corral host hooks where no sandbox can change them.
#
#   mm-agent-host-install.sh --release vX.Y.Z  # a release tag signed by a pinned key
#   bin/mm-agent-host-install.sh             # from the checkout this script is in
#   mm-agent-host-install.sh --from DIR      # from another checkout you reviewed
#   mm-agent-host-install.sh --downgrade ... # allow an older version than current
#   mm-agent-host-install.sh --rollback      # swap current and previous
#   mm-agent-host-install.sh --check         # compare host copy vs installed plugin
#
# Run it on the HOST, in a normal terminal — never inside corral — from a git
# clone no session works in, after reviewing it (git log, git diff). corral's preStart/postEnd
# hooks run on the host with the registration secret. corral approves the
# `exec` file by its content, but not the scripts it sources and starts (bin/).
# The installed plugin lives under the corral homes and ~/.claude, a checkout
# may be a session's workdir; a sandbox can write all of them. So the source is
# never looked up from what is installed: it is this script's own tree or
# --from, and a source under ~/.cache/corral, ~/.claude or $CORRAL_WORKDIR is
# refused. Only what is committed is copied (git archive HEAD): a clone with
# uncommitted changes or untracked files in the copied paths is refused, and
# so is a symlink among them. git runs without system or global config, and a
# clone whose own config or info/attributes could make it run a command
# (filter, diff, merge drivers, includes) is refused; the installed commit is
# printed to compare with the one you reviewed. This
# copies adapters/corral/ and bin/ into a directory per release beside
# MM_BUS_HOST_DIR (default ~/.local/share/mm-agent-bus/current):
# releases/<version>-dev.<commit>/, marked SOURCE=unsigned. `--release vX.Y.Z`
# instead fetches that annotated tag from MM_RELEASE_URL (default: the GitHub
# repository) into src.git beside releases/, never moving a tag it fetched
# before, verifies its SSH signature against the public keys in
# MM_RELEASE_SIGNERS (one per line) and the version in its tree, and installs
# git archive of it into releases/X.Y.Z/, marked SOURCE=signed (ADR-011
# phase 1b, RT-6.29). `current` is a symlink to one of them, switched in one
# rename once the release is complete; the one before stays as `previous`, and
# --rollback swaps the two. The sandbox neither sees nor writes any of it.
# Point corral's `exec` once at current.hooks/, a dispatcher that stays the
# same across releases; rerun this after a plugin update. corral asks again
# only when the dispatcher itself changes: the check of a switch is you
# running this from a clone you reviewed.
# It also copies the MCP server (mcp/, without a checkout's .venv or caches)
# and prepares its Python environment in the release (uv sync --locked) before
# the switch; without uv, or when that fails, current stays as it was. The
# start hook runs each session's MCP server on the host from this tree, so the
# bot token never enters the sandbox (RT-6.18). A release goes when neither
# link nor a running session (recorded by preStart) uses it.
#
# Spec: arc42 §7.1 (docs/arc42/07-deployment-view.md), RT-6.28, RT-6.29.
set -euo pipefail

here="${0%/*}"
[[ "$here" != "$0" ]] || here=.
SELF="$(cd -P "$here/.." && pwd -P)"
# shellcheck source=mm-agent-lib.sh
source "$SELF/bin/mm-agent-lib.sh"
# Commands only from PATH entries no sandbox can write (arc42 §7.3).
mm_safe_path

die() { echo "mm-agent-host-install: $*" >&2; exit 1; }

FROM="" RELEASE="" CHECK=0 ROLLBACK=0 DOWNGRADE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="${2:-}"; shift 2 ;;
    --release) RELEASE="${2:-}"; [[ -n "$RELEASE" ]] || die "--release takes a version tag v<major>.<minor>.<patch>"; shift 2 ;;
    --check) CHECK=1; shift ;;
    --rollback) ROLLBACK=1; shift ;;
    --downgrade) DOWNGRADE=1; shift ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done
# The version is checked before it reaches a git argument or any output.
if [[ -n "$RELEASE" ]]; then
  [[ "$RELEASE" =~ ^v?([0-9]+\.[0-9]+\.[0-9]+)$ ]] ||
    die "--release takes a version tag v<major>.<minor>.<patch>; refused"
  VER="${BASH_REMATCH[1]}" TAG="v${BASH_REMATCH[1]}"
  [[ -z "$FROM" && "$ROLLBACK" -eq 0 && "$CHECK" -eq 0 ]] || die "--release takes no --from, --rollback or --check"
fi

DEST="${MM_BUS_HOST_DIR:-$HOME/.local/share/mm-agent-bus/current}"
# Relative paths are the operator's; then work from /, never from a checkout.
[[ -z "$FROM" || "$FROM" == /* ]] || FROM="$PWD/$FROM"
[[ "$DEST" == /* ]] || DEST="$PWD/$DEST"
cd /

version_of() { mm_tree_version "$1"; }

command -v jq >/dev/null 2>&1 || die "jq is required"

if [[ "$CHECK" -eq 1 ]]; then
  # The installed plugins include the corral homes: a version to compare, no path.
  have="$(version_of "$DEST")" want="$(mm_newest_plugin_version || true)"
  printf 'host hooks: %s, %s (%s)\nplugin:     %s\n' "$have" "$(mm_release_kind "$DEST")" "$DEST" "${want:-not found}"
  [[ -n "$want" && "$have" == "$want" ]]
  exit $?
fi

# Inside corral the target is not the host's and the source may be tampered with.
[[ -z "${CORRAL_SANDBOX:-}" ]] || die "run this on the host, not inside a corral session"

# Places a corral sandbox can write (mm_sandbox_can_write): every corral home,
# ~/.claude, which the homes share by default, corral's temporary directories;
# without regard to case. $1 is a resolved path (pwd -P).
sandbox_writable() { mm_sandbox_can_write "$1"; }

# --- the layout (RT-6.28) ------------------------------------------------------
# Beside the target: releases/<name>/ per release, and the target's own name
# (`current`) and `previous` as symlinks to releases/<name>. A name is a
# version, with -dev.<12 hex of the commit> for an install from a clone, or
# -legacy for a host copy from before this layout. postEnd checks the same form.
RELEASE_RE='^[0-9]+\.[0-9]+\.[0-9]+(-dev\.[0-9a-f]{12}|-legacy)?$'
layout() {
  parent="$(dirname "$DEST")"
  case "$(basename "$DEST")" in releases | previous) die "MM_BUS_HOST_DIR may not be named releases or previous" ;; esac
  mkdir -p "$parent"
  parent="$(cd -P "$parent" && pwd -P)"
  ! sandbox_writable "$parent" || die "$DEST can be changed from inside a corral sandbox; pick another MM_BUS_HOST_DIR"
  # One installer at a time: a second one's garbage collection could remove
  # the release the first is preparing. mkdir is atomic (no flock on macOS).
  mkdir "$parent/.install.lock" 2>/dev/null ||
    die "another installer runs on $parent (remove $parent/.install.lock if none does)"
  LOCK="$parent/.install.lock"
  DEST="$parent/$(basename "$DEST")"
  RELEASES="$parent/releases" PREV="$parent/previous" HOOKS="$DEST.hooks"
  [[ ! -L "$RELEASES" ]] || die "$RELEASES is a symlink; refused"
  [[ ! -L "$HOOKS" ]] || die "$HOOKS is a symlink; refused"
  SRC_GIT="$parent/src.git"
  [[ ! -L "$SRC_GIT" ]] || die "$SRC_GIT is a symlink; refused"
  [[ ! -e "$PREV" || -L "$PREV" ]] || die "$PREV is not a symlink; move it away, then rerun"
  [[ ! -e "$DEST" || -L "$DEST" || -d "$DEST" ]] || die "$DEST is neither a directory nor a symlink; move it away, then rerun"
  mkdir -p "$RELEASES"
  chmod go-w "$RELEASES"
  state_dir
}

# On every exit: the half-made release ($new, when set) and the lock.
new="" LOCK=""
cleanup() {
  [[ -z "$new" ]] || rm -rf "$new"
  [[ -z "$LOCK" ]] || { rm -f "$LOCK/allowed_signers"; rmdir "$LOCK" 2>/dev/null || true; }
}
trap cleanup EXIT

# The release a link points at, by name: only releases/<name> of this layout.
release_of() {
  local t
  [[ -L "$1" ]] && t="$(readlink "$1")" || return 1
  [[ "$t" == releases/* ]] || return 1
  t="${t#releases/}"
  [[ "$t" =~ $RELEASE_RE && -d "$RELEASES/$t" && ! -L "$RELEASES/$t" ]] || return 1
  printf '%s\n' "$t"
}

# Point link $1 at $2 in one rename, so a session starting meanwhile sees the
# old or the new release, never none. mv onto a symlink to a directory would
# move INTO that directory: GNU mv takes -T for "not into", BSD and macOS mv -h.
set_link() {
  local link="$1" target="$2" tmp
  tmp="$parent/.$(basename "$link").$$"
  rm -f "$tmp"
  ln -s "$target" "$tmp" || return 1
  if mv --version >/dev/null 2>&1; then
    mv -fT "$tmp" "$link" || { rm -f "$tmp"; return 1; }
  else
    mv -fh "$tmp" "$link" || { rm -f "$tmp"; return 1; }
  fi
  [[ -L "$link" && "$(readlink "$link")" == "$target" ]]
}

# The MCP server's environment, in the release itself: a virtual environment
# does not survive a move. From the host tree only (--directory), never from
# the source or the working directory.
sync_release() {
  (cd "$1/mcp" && uv sync --locked --no-dev --quiet --directory "$1/mcp") >&2
}

# The host state directory with the session records (RT-6.28): preStart
# writes <session>.release there, postEnd removes it. STATE is its resolved
# path ("" when there is none); STATE_OK=0 when it is one a sandbox can write,
# which is then not read. A record without a session file, an hour old, is a
# crash's and goes.
state_dir() {
  local s f
  s="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
  STATE="" STATE_OK=1 STATE_SHOWN="$s"
  [[ -d "$s" ]] || return 0
  if ! s="$(cd -P "$s" 2>/dev/null && pwd -P)" || sandbox_writable "$s"; then
    echo "mm-agent-host-install: $STATE_SHOWN can be changed from inside a corral sandbox; its session records are not read" >&2
    STATE_OK=0
    return 0
  fi
  STATE="$s"
  shopt -s nullglob
  for f in "$STATE"/*.release; do
    [[ ! -e "${f%.release}.env" && -n "$(find "$f" -mmin +60 2>/dev/null)" ]] && rm -f "$f"
  done
  shopt -u nullglob
}

# The session keys of all records, whatever they name.
recorded_keys() {
  local f k
  [[ -n "$STATE" ]] || return 0
  shopt -s nullglob
  for f in "$STATE"/*.release; do
    k="$(basename "$f" .release)"
    [[ "$k" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]] || k="?"
    printf '%s\n' "$k"
  done
  shopt -u nullglob
}

# The releases the records name: one per record of this layout.
recorded_releases() {
  local f rec
  [[ -n "$STATE" ]] || return 0
  shopt -s nullglob
  for f in "$STATE"/*.release; do
    [[ -f "$f" && ! -L "$f" ]] || continue
    rec="$(mm_read_file "$f" 4096 | head -n1)" || rec=""
    if [[ "$rec" == "$RELEASES"/* ]]; then printf '%s\n' "${rec#"$RELEASES"/}"; fi
  done
  shopt -u nullglob
}

# Running corral sessions without a record: a host-managed session file
# (MM_BUS_HOST_PID) whose corral process lives, and no <key>.release. Their
# hooks run by an older path (current/adapters/corral/…, a host copy from
# before RT-6.28) or preStart could not record them: a switch changes the file
# corral runs at their end, and corral then skips their postEnd.
unrecorded_sessions() {
  local f k pid content
  [[ -n "$STATE" ]] || return 0
  shopt -s nullglob
  for f in "$STATE"/*.env; do
    [[ -f "$f" && ! -L "$f" ]] || continue
    k="$(basename "$f" .env)"
    [[ "$k" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ && ! -e "$STATE/$k.release" ]] || continue
    content="$(mm_read_file "$f")" || continue
    [[ $'\n'"$content" == *$'\n'MM_BUS_HOST_PID=* ]] || continue
    pid="$(sed -n 's/^MM_SESSION_PID=//p' <<<"$content" | head -n1)"
    [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null && printf '%s\n' "$k"
  done
  shopt -u nullglob
  return 0
}

# Before a switch: name the sessions it may cost their postEnd.
warn_unrecorded() {
  local running
  running="$(unrecorded_sessions | tr '\n' ' ')"
  [[ -z "$running" ]] ||
    echo "mm-agent-host-install: WARNING: corral sessions without a release record run (${running% }): corral may skip their postEnd after this switch; their bots go with the host process's watchdog or bin/mm-agent-sweep.sh" >&2
}

# Remove each release neither link nor a running session uses; without the
# session records nothing goes.
collect_garbage() {
  local keep=" " n d
  for d in "$DEST" "$PREV"; do
    if n="$(release_of "$d")"; then keep+="$n "; fi
  done
  [[ "$STATE_OK" -eq 1 ]] || { echo "no release removed" >&2; return 0; }
  while IFS= read -r n; do [[ -z "$n" ]] || keep+="$n "; done < <(recorded_releases)
  shopt -s nullglob
  for d in "$RELEASES"/*; do
    n="$(basename "$d")"
    [[ -d "$d" && ! -L "$d" && "$n" =~ $RELEASE_RE && "$keep" != *" $n "* ]] || continue
    rm -rf "$d" && echo "removed release $n: neither linked nor used by a session"
  done
  shopt -u nullglob
}

# corral's hooks: the dispatcher of release $1 (adapters/corral/mm-hook-dispatch.sh)
# as <link>.hooks/mm-session-start.sh and mm-session-stop.sh. corral approves a
# hook by its configured path's content and skips a postEnd whose content
# changed during the session, so these stay byte for byte across switches and
# are replaced only when the dispatcher's own text changed, and not while a
# session runs (or none can be told): its postEnd would be skipped.
install_dispatcher() {
  local src="$RELEASES/$1/adapters/corral/mm-hook-dispatch.sh" f tmp changed=0 busy
  [[ -f "$src" && ! -L "$src" ]] || die "release $1 has no hook dispatcher"
  mkdir -p "$HOOKS"
  chmod go-w "$HOOKS"
  for f in mm-session-start.sh mm-session-stop.sh; do
    cmp -s "$src" "$HOOKS/$f" || changed=1
  done
  [[ "$changed" -eq 1 ]] || return 0
  if [[ -e "$HOOKS/mm-session-start.sh" || -e "$HOOKS/mm-session-stop.sh" ]]; then
    busy="$( { recorded_keys; unrecorded_sessions; } | sort -u | tr '\n' ' ')"
    if [[ "$STATE_OK" -eq 0 || -n "$busy" ]]; then
      dispatch_line="WARNING: the hook dispatcher changed, but corral sessions run (${busy:-the state directory cannot tell}): $HOOKS kept as it was, since corral would skip their postEnd; rerun this when none runs"
      return 0
    fi
  fi
  for f in mm-session-start.sh mm-session-stop.sh; do
    tmp="$HOOKS/.$f.$$"
    { cp "$src" "$tmp" && chmod 755 "$tmp" && mv -f "$tmp" "$HOOKS/$f"; } ||
      { rm -f "$tmp"; die "could not write $HOOKS/$f"; }
  done
  dispatch_line="The hook dispatcher in $HOOKS is new or changed: corral asks to approve it at the next launch."
}

if [[ "$ROLLBACK" -eq 1 ]]; then
  [[ -z "$FROM" && "$DOWNGRADE" -eq 0 ]] || die "--rollback takes no other option"
  layout
  cur="$(release_of "$DEST")" || die "$DEST is not a link to a release; nothing to roll back"
  prev="$(release_of "$PREV")" || die "no previous release to roll back to"
  [[ -f "$RELEASES/$prev/SOURCE" && -e "$RELEASES/$prev/mcp/.venv/bin/python" ]] ||
    die "the previous release $prev has no Python environment; install a release instead"
  warn_unrecorded
  set_link "$DEST" "releases/$prev" || die "could not switch $DEST; it is unchanged"
  set_link "$PREV" "releases/$cur" || die "switched to $prev, but could not point $PREV at $cur"
  echo "Rolled back: $DEST -> releases/$prev; previous -> releases/$cur (--rollback again returns to it)"
  echo "corral's hook files ($HOOKS) are unchanged, so corral asks nothing: this run was the check. Running sessions keep their release."
  echo "Session records read from ${STATE:-$STATE_SHOWN}."
  collect_garbage
  exit 0
fi
unset "${!GIT_@}"
# What every release tree has; a source without it is no mattermost-ai-agent-bus.
REQUIRED=(adapters/corral/10-mm-session-start.sh adapters/corral/90-mm-session-stop.sh
          bin/mm-agent-lib.sh bin/mm-agent-symbols.sh .claude-plugin/plugin.json mcp/pyproject.toml mcp/uv.lock
          mcp/src/mattermost_ai_agent_bus/hosted.py)
# What is installed; mcp/README.md and the skill when the source has them.
paths=(bin adapters/corral .claude-plugin/plugin.json mcp/pyproject.toml mcp/uv.lock mcp/src)
OPTIONAL=(mcp/README.md skills/mattermost-ai-agent-bus)

# --- a signed release (RT-6.29) -------------------------------------------------
# The pinned keys come from the host's environment only, never from the plugin,
# a workdir or a session; git runs without system or global config, hook or
# attributes file, fetches over https (or from a local path for a mirror) with
# no credential, and checks every object it receives.
if [[ -n "$RELEASE" ]]; then
  [[ -n "${MM_RELEASE_SIGNERS:-}" ]] ||
    die "MM_RELEASE_SIGNERS is not set: pin the release keys (ssh-ed25519 public key lines) before installing a release; nothing changed"
  SIGNERS=""
  while IFS= read -r line; do
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -n "$line" && "$line" != \#* ]] || continue
    [[ "$line" =~ ^(ssh-ed25519|sk-ssh-ed25519@openssh\.com)[[:space:]]+([A-Za-z0-9+/]+={0,2})([[:space:]].*)?$ ]] ||
      die "MM_RELEASE_SIGNERS has a line that is no Ed25519 public key (ssh-ed25519 AAAA…); refused"
    SIGNERS+="mm-release namespaces=\"git\" ${BASH_REMATCH[1]} ${BASH_REMATCH[2]}"$'\n'
  done <<<"${MM_RELEASE_SIGNERS//;/$'\n'}"
  [[ -n "$SIGNERS" ]] || die "MM_RELEASE_SIGNERS holds no public key; nothing changed"
  URL="${MM_RELEASE_URL:-https://github.com/dembaca/mattermost-ai-agent-bus.git}"
  case "$URL" in
    https://*)
      [[ "$URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?(/[A-Za-z0-9._~%+-]+)+/?$ ]] ||
        die "MM_RELEASE_URL is no plain https URL (no user, query or space); refused" ;;
    /* | file:///*)
      u="$(cd -P "${URL#file://}" 2>/dev/null && pwd -P)" || die "MM_RELEASE_URL: no such directory: ${URL#file://}"
      ! sandbox_writable "$u" || die "MM_RELEASE_URL $u can be changed from inside a corral sandbox; refused"
      URL="$u" ;;
    *) die "MM_RELEASE_URL must be an https:// URL or an absolute path; refused" ;;
  esac
  command -v ssh-keygen >/dev/null 2>&1 || die "ssh-keygen is required to verify the release's signature"
  gv="$(git --version | sed -n 's/^git version \([0-9]*\.[0-9]*\).*/\1/p')"
  if [[ -z "$gv" ]] || mm_version_newer "$gv.0" "2.34.0"; then
    die "git 2.34 or newer is required to verify SSH signatures"
  fi
  rgit() {
    # LC_ALL=C: its messages are matched below. TMPDIR: verify-tag's temporary
    # files go into the installer's own lock directory, which only the host sees.
    LC_ALL=C TMPDIR="$LOCK" GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
      git --no-optional-locks -c core.attributesFile=/dev/null -c core.fsmonitor=false \
      -c core.hooksPath=/dev/null -c gc.auto=0 -c maintenance.auto=false \
      -c protocol.allow=never -c protocol.https.allow=always -c protocol.file.allow=always \
      -c credential.helper= -c transfer.fsckObjects=true --git-dir="$SRC_GIT" "$@"
  }
  # Name, signature, then tree; any refusal before the switch leaves current as it was.
  stage_release() {
    local out old_id tag_id hdr tname ttype obj vout vrc=0 f v_py v_init
    [[ -d "$SRC_GIT" ]] ||
      GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git init -q --bare --template= "$SRC_GIT" ||
      die "could not create $SRC_GIT"
    old_id="$(rgit rev-parse -q --verify "refs/tags/$TAG" || true)"
    # No + in the refspec: a tag fetched before is never moved (git refuses to clobber it).
    if ! out="$(rgit fetch --no-tags --no-recurse-submodules --no-write-fetch-head \
          "$URL" "refs/tags/$TAG:refs/tags/$TAG" 2>&1)"; then
      if [[ -n "$old_id" && "$out" == *"would clobber existing tag"* ]]; then
        die "the tag $TAG at $URL moved since this host fetched it (it was ${old_id:0:12}): a signed release never moves; refused, $DEST is unchanged"
      fi
      die "could not fetch $TAG from $URL; $DEST is unchanged:"$'\n'"$(tail -n 5 <<<"$out" | tr -cd '[:print:]\n')"
    fi
    tag_id="$(rgit rev-parse -q --verify "refs/tags/$TAG")" || die "$TAG is not in $SRC_GIT after the fetch"
    [[ "$(rgit cat-file -t "$tag_id")" == tag ]] ||
      die "$TAG is a lightweight tag (no tag object, so no signature): not a release; refused, $DEST is unchanged"
    printf '%s' "$SIGNERS" >"$LOCK/allowed_signers"
    vout="$(rgit -c gpg.format=ssh -c gpg.ssh.allowedSignersFile="$LOCK/allowed_signers" \
              -c gpg.ssh.program=ssh-keygen verify-tag "$tag_id" 2>&1)" || vrc=$?
    if [[ "$vout" == *"no signature found"* ]]; then
      die "$TAG carries no signature: not a signed release; refused, $DEST is unchanged"
    fi
    # SSH signatures: verify-tag's exit code with allowedSignersFile set, and
    # the one principal the installer wrote, nothing else.
    SIGNER="$(sed -n 's/^Good "git" signature for mm-release with [A-Z0-9-]* key \(SHA256:[A-Za-z0-9+/]*\)$/\1/p' <<<"$vout" | head -n1)"
    if [[ "$vrc" -ne 0 || -z "$SIGNER" ]]; then
      if [[ "$vout" == *"No principal matched"* ]]; then
        die "$TAG is signed by a key not pinned in MM_RELEASE_SIGNERS ($(grep -o 'SHA256:[A-Za-z0-9+/]*' <<<"$vout" | head -n1)); refused, $DEST is unchanged"
      fi
      die "$TAG does not verify against MM_RELEASE_SIGNERS; refused, $DEST is unchanged:"$'\n'"$(tail -n 3 <<<"$vout" | tr -cd '[:print:]\n')"
    fi
    hdr="$(rgit cat-file tag "$tag_id")"
    hdr="$(sed '/^$/q' <<<"$hdr")"
    tname="$(sed -n 's/^tag //p' <<<"$hdr" | head -n1)"
    ttype="$(sed -n 's/^type //p' <<<"$hdr" | head -n1)"
    obj="$(sed -n 's/^object //p' <<<"$hdr" | head -n1)"
    [[ "$tname" == "$TAG" ]] ||
      die "the tag $TAG is a signed tag object named $(tr -cd '[:alnum:]._-' <<<"$tname" | cut -c1-40): a ref pointed at another release; refused, $DEST is unchanged"
    [[ "$ttype" == commit && "$obj" =~ ^[0-9a-f]{40}([0-9a-f]{24})?$ ]] ||
      die "$TAG does not tag a commit; refused"
    head_sha="$obj" TAG_ID="$tag_id"
    for f in "${OPTIONAL[@]}"; do
      if rgit cat-file -e "$head_sha:$f" 2>/dev/null; then paths+=("$f"); fi
    done
    rgit archive --format=tar "$head_sha" -- "${paths[@]}" | tar -x -C "$new" ||
      die "could not unpack $TAG"
    for f in "${REQUIRED[@]}"; do
      [[ -f "$new/$f" ]] || die "$TAG is no mattermost-ai-agent-bus release (missing $f); refused"
    done
    v_py="$(sed -n 's/^version = "\([^"]*\)"$/\1/p' "$new/mcp/pyproject.toml" | head -n1)"
    v_init="$(sed -n 's/^__version__ = "\([^"]*\)"$/\1/p' "$new/mcp/src/mattermost_ai_agent_bus/__init__.py" 2>/dev/null | head -n1)"
    version="$(version_of "$new")"
    [[ "$version" == "$VER" && "$v_py" == "$VER" && "$v_init" == "$VER" ]] ||
      die "the tree of $TAG has version $(tr -cd '[:alnum:]._-' <<<"$version/$v_py/$v_init" | cut -c1-60) (plugin.json/pyproject/__init__), not $VER; refused, $DEST is unchanged"
  }
else
[[ -n "$FROM" ]] || FROM="$SELF"
src="$(cd -P "$FROM" 2>/dev/null && pwd -P)" || die "no such directory: $FROM"
FROM="$src"
if [[ -n "${CORRAL_WORKDIR:-}" ]]; then
  wd="$(cd -P "$CORRAL_WORKDIR" 2>/dev/null && pwd -P)" || wd="$CORRAL_WORKDIR"
  shopt -s nocasematch
  [[ "$FROM/" != "$wd/"* && "$FROM/" != "${CORRAL_WORKDIR%/}/"* ]] ||
    die "$FROM is a corral session's workdir, which its agent can change; run the installer from a checkout you reviewed (or pass --from <checkout>)"
  shopt -u nocasematch
fi
if sandbox_writable "$FROM"; then
  die "$FROM can be changed from inside a corral sandbox; run the installer from a checkout you reviewed, outside corral and outside every session's workdir (or pass --from <checkout>)"
fi
for f in "${REQUIRED[@]}"; do
  [[ -f "$FROM/$f" ]] || die "$FROM is not a mattermost-ai-agent-bus tree (missing $f)"
done
[[ "$FROM" != "$(cd -P "$DEST" 2>/dev/null && pwd -P)" ]] ||
  die "source and target are the same directory; run it from a checkout, or pass --from <checkout>"

# What is committed, nothing else: git archive of HEAD, so neither an untracked
# file nor a change made after the review comes along. A clone whose copied
# paths differ from HEAD is refused rather than silently installed without the
# difference. A clone's config can make git run commands (filters on status
# and archive, diff and merge drivers, fsmonitor, hooks) and a sandbox may have
# written it, so git reads no system or global config, no attributes file of
# the operator's, runs no fsmonitor and no hook, and a clone whose config or
# info/attributes could make it run anything is refused (arc42 §7.1).
git_() {
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 \
    git --no-optional-locks -c core.attributesFile=/dev/null -c core.fsmonitor=false \
    -c core.hooksPath=/dev/null -c gc.auto=0 -c maintenance.auto=false -C "$FROM" "$@"
}
[[ "$(git_ rev-parse --show-toplevel 2>/dev/null)" == "$FROM" ]] ||
  die "$FROM is not the top of a git clone; install from a clone you reviewed (git log, git diff)"
repo_dirs=()
while IFS= read -r d; do repo_dirs+=("$d"); done < <(
  git_ rev-parse --path-format=absolute --git-dir --git-common-dir)
[[ "${#repo_dirs[@]}" -eq 2 ]] || die "cannot find the repository of $FROM"
for d in "${repo_dirs[@]}"; do
  real="$(cd -P "$d" 2>/dev/null && pwd -P)" || die "cannot resolve the repository of $FROM"
  ! sandbox_writable "$real" ||
    die "$FROM keeps its repository in $real, which a corral sandbox can change; install from a clone that was never a session's workdir and is not a worktree of one"
done
risky="$(git_ config --get-regexp '^(filter|diff|merge|include|includeif|tar)\.' 2>/dev/null || true)"
[[ -z "$risky" ]] ||
  die "$FROM has git config that can run commands (filter, diff or merge drivers, includes); refused — install from a clone that was never a session's workdir:"$'\n'"$(cut -d' ' -f1 <<<"$risky" | tr -cd '[:alnum:]._:/\n-')"
for a in "${repo_dirs[0]}/info/attributes" "${repo_dirs[1]}/info/attributes"; do
  [[ ! -e "$a" ]] || die "$FROM has $a, which can make git run commands; refused — install from a clone that was never a session's workdir"
done
for f in "${OPTIONAL[@]}"; do
  if git_ cat-file -e "HEAD:$f" 2>/dev/null; then paths+=("$f"); fi
done
dirty="$(git_ status --porcelain --untracked-files=all -- "${paths[@]}")" ||
  die "git status failed in $FROM"
[[ -z "$dirty" ]] ||
  die "$FROM has uncommitted changes or untracked files in what would be installed — commit or remove them, review, then rerun:"$'\n'"$dirty"
fi

command -v uv >/dev/null 2>&1 ||
  die "uv is required: it prepares the MCP server's environment, without which sessions get no bus; nothing changed"

# Copy into a directory of its own under releases/, prepare its environment
# there, and only then switch current to it in one rename (RT-6.28): a session
# starting meanwhile sees the old or the new release, never half of one, and a
# failed step leaves current as it was.
layout
# A host copy from before this layout moves (step 5): every corral session
# running from it would lose its postEnd, since corral runs it by a path whose
# file changes. Done with no session running.
if [[ -d "$DEST" && ! -L "$DEST" ]]; then
  running="$(unrecorded_sessions | tr '\n' ' ')"
  [[ -z "$running" ]] ||
    die "corral sessions run from the host copy in $DEST (${running% }): moving it into releases/ would make corral skip their postEnd; end them, then rerun (a session file a crash left: bin/mm-agent-sweep.sh)"
fi
new="$(mktemp -d "$RELEASES/.install.XXXXXX")"
# The MCP server, for the host process (RT-6.18); the skill, whose size its
# overhead stats count (RT-6.9).
if [[ -n "$RELEASE" ]]; then
  stage_release
  ORIGIN="$URL, tag $TAG"
  name="$VER"
  source_text="SOURCE=signed"$'\n'"TAG=$TAG"$'\n'"TAG_OBJECT=$TAG_ID"$'\n'"COMMIT=$head_sha"$'\n'"SIGNER=$SIGNER"$'\n'
else
  ORIGIN="$FROM"
  head_sha="$(git_ rev-parse --verify 'HEAD^{commit}')" || die "$FROM has no commit to install"
  head_line="$(git_ log -1 --no-show-signature --format='%H %s' "$head_sha")"
  git_ archive --format=tar "$head_sha" -- "${paths[@]}" | tar -x -C "$new" ||
    die "could not copy HEAD of $FROM"
  version="$(version_of "$new")"
  name="${version}-dev.${head_sha:0:12}"
  source_text="SOURCE=unsigned"$'\n'"COMMIT=$head_sha"$'\n'
fi
links="$(find "$new" -type l -print)"
[[ -z "$links" ]] || die "$ORIGIN has symlinks in what would be installed; refused:"$'\n'"${links//$new\//}"
chmod -R go-w "$new"
chmod +x "$new"/adapters/corral/*.sh "$new"/bin/*.sh

[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] ||
  die "$ORIGIN has no version of the form <major>.<minor>.<patch> in .claude-plugin/plugin.json"
[[ "$name" =~ $RELEASE_RE ]] || die "cannot name a release for commit $head_sha"
# An older version only on purpose: nobody is led back to a release with a
# known bug by accident.
if [[ -e "$DEST" ]]; then
  have="$(version_of "$DEST")"
  if [[ "$DOWNGRADE" -eq 0 ]] && mm_version_newer "$version" "$have"; then
    die "$DEST runs $have, newer than $version from $ORIGIN; pass --downgrade to install the older one (or --rollback to go back to previous)"
  fi
fi

cur="$(release_of "$DEST" || true)"
rel="$RELEASES/$name"
if [[ "$cur" == "$name" ]]; then
  # The same commit again: it is current, complete and in use; nothing changes.
  rm -rf "$new"
  new=""
  echo "mattermost-ai-agent-bus $name is already the current release in $DEST; nothing changed"
  dispatch_line=""
  install_dispatcher "$name"
  [[ -z "$dispatch_line" ]] || echo "$dispatch_line"
  collect_garbage
  exit 0
fi
# A signed release is reused only for the same tag object.
if [[ -d "$rel" && ! -L "$rel" && -f "$rel/SOURCE" && -e "$rel/mcp/.venv/bin/python" ]] &&
   { [[ -z "$RELEASE" ]] || grep -qx "TAG_OBJECT=$TAG_ID" "$rel/SOURCE"; }; then
  # Installed before and complete (SOURCE comes last): switch back to it.
  rm -rf "$new"
  new=""
  server="MCP server environment ready (release installed before): the bot token stays on the host (RT-6.18)"
else
  rm -rf "$rel"
  mv "$new" "$rel"
  new="$rel"
  if ! sync_release "$rel"; then
    die "could not prepare the MCP server's environment in $rel (uv sync --locked); $DEST is unchanged"
  fi
  # SOURCE=signed for --release (RT-6.29), SOURCE=unsigned from a clone.
  printf '%s' "$source_text" >"$rel/SOURCE"
  chmod go-w "$rel/SOURCE"
  new=""
  server="MCP server environment ready: the bot token stays on the host (RT-6.18)"
fi

# A host copy from before this layout is a directory: it moves to releases/
# and becomes previous. Between the move and the link no current exists for an
# instant; once only.
old="$cur" migrated=""
if [[ -d "$DEST" && ! -L "$DEST" ]]; then
  legacy="$(version_of "$DEST")"
  [[ "$legacy" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || legacy="0.0.0"
  migrated="${legacy}-legacy"
  [[ ! -e "$RELEASES/$migrated" ]] || die "$RELEASES/$migrated exists already; move it away, then rerun"
  mv "$DEST" "$RELEASES/$migrated"
  old="$migrated"
fi
warn_unrecorded
if ! set_link "$DEST" "releases/$name"; then
  [[ -z "$migrated" || -e "$DEST" ]] || mv "$RELEASES/$migrated" "$DEST"
  die "could not switch $DEST to $rel; it is unchanged"
fi
prev_line="previous: none" gc=1
if [[ -n "$old" ]]; then
  if set_link "$PREV" "releases/$old"; then
    prev_line="previous: releases/$old (--rollback returns to it)"
  else
    echo "mm-agent-host-install: WARNING: could not point $PREV at releases/$old" >&2
    gc=0
  fi
fi
# The migrated copy's environment did not survive its move: prepare it again,
# else it is no release to roll back to.
if [[ -n "$migrated" ]]; then
  if sync_release "$RELEASES/$migrated"; then
    printf 'SOURCE=unsigned\n' >"$RELEASES/$migrated/SOURCE"
  else
    rm -f "$PREV"
    prev_line="previous: none (the old host copy $migrated, kept in releases/, has no Python environment; no rollback)"
    gc=0
  fi
fi
dispatch_line="corral's hook files in $HOOKS are unchanged: corral asks nothing for this switch, so this run was the check."
install_dispatcher "$name"
# Not after a step that went wrong: the release it left unlinked may be the
# only way back.
if [[ "$gc" -eq 1 ]]; then collect_garbage; else echo "no release removed: a step above failed" >&2; fi

if [[ -n "$RELEASE" ]]; then
  kind="signed release $TAG, signed by $SIGNER, a key pinned in MM_RELEASE_SIGNERS"
  head_line="$head_sha (tag object $TAG_ID)"
  check="Verified: the tag's signature, its name and the version in its tree."
else
  kind="unsigned: from a clone, not a signed release"
  check="Compare the commit with the one you reviewed."
fi
cat <<EOF
Installed mattermost-ai-agent-bus $version host hooks ($kind)
  from: $ORIGIN
  commit: $head_line
  to:   $DEST -> releases/$name
  $prev_line
  $check
$server

corral config (~/.config/corral/config.yml), set once:

  providers:
    hooks:
      enabled: true
      preStart:
        mm-bus:
          exec: $HOOKS/mm-session-start.sh
          optional: true
      postEnd:
        mm-bus:
          exec: $HOOKS/mm-session-stop.sh
          optional: true

$dispatch_line
Running sessions keep the release they started with and end with it.
Session records read from ${STATE:-$STATE_SHOWN}: corral's hooks must see the
same XDG_STATE_HOME as this shell.
EOF

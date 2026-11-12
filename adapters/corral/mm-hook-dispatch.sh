#!/usr/bin/env bash
# corral's preStart and postEnd hook for the host copy: a dispatcher that
# stays the same from release to release (RT-6.28).
#
# corral approves a hook by the content of the path it is configured with, and
# skips a postEnd hook whose content changed during the session. A path under
# current/ changes with every switch of current; this file does not. The
# installer writes it twice, beside the link it serves (MM_BUS_HOST_DIR,
# default ~/.local/share/mm-agent-bus/current):
#
#   current.hooks/mm-session-start.sh   preStart: current's 10-mm-session-start.sh
#   current.hooks/mm-session-stop.sh    postEnd: the stop hook of the release
#                                       this session's preStart recorded, else
#                                       current's
#
# Same content under both names; the name decides. The installer replaces it
# only when this text changes, and then corral asks to approve it once.
#
# Runs on the HOST, outside the sandbox, before the hooks: sources nothing,
# and checks inline, as they do, that nothing it reads or runs lies where a
# sandbox can write. stdout is corral's contribution interface: one JSON
# object from preStart, or nothing.
#
# Spec: RT-6.28 in docs/arc42/06-runtime-view.md.
set -uo pipefail
unset CDPATH

# --- what a sandbox can write (arc42 §7.1, §7.3) -------------------------------
# Same as the hooks', inline: every corral home, ~/.claude, the session's
# workdir and corral's temporary directories, without regard to case; a path
# that is not absolute or has a . or .. component counts as one.
shopt -s nocasematch
sandbox_dirs=(/tmp/corral- /private/tmp/corral-)
for r in "$HOME/.cache/corral" "$HOME/.claude" "${CORRAL_WORKDIR:-}" "${TMPDIR:-}"; do
  [[ -n "$r" && ( "$r" != "${TMPDIR:-}" || "$r" == *corral* ) ]] || continue
  sandbox_dirs+=("${r%/}/")
  if real="$(cd -P "$r" 2>/dev/null && pwd -P)"; then sandbox_dirs+=("$real/"); fi
done
sandbox_can_write() {
  local d
  [[ "$1" == /* && "$1/" != */../* && "$1/" != */./* ]] || return 0
  for d in "${sandbox_dirs[@]}"; do [[ "$1/" == "$d"* ]] && return 0; done
  return 1
}
path_rest="${PATH:-}:" safe_path=""
while [[ -n "$path_rest" ]]; do
  e="${path_rest%%:*}" path_rest="${path_rest#*:}"
  sandbox_can_write "${e%/}" || safe_path="${safe_path:+$safe_path:}$e"
done
export PATH="${safe_path:-/usr/bin:/bin:/usr/sbin:/sbin}"
self="$0"
[[ "$self" == /* ]] || self="$PWD/$self"
cd /

log() { echo "mm-bus: $*" >&2; }

case "${self##*/}" in
  mm-session-start.sh) what=start hook=10-mm-session-start.sh ;;
  mm-session-stop.sh) what=stop hook=90-mm-session-stop.sh ;;
  *) log "unknown hook name ${self##*/}; nothing run"; exit 0 ;;
esac

# No release to run: preStart tells the sandbox (fail closed, no bot).
fail() {
  log "$1 — nothing run; install the host copy from a reviewed clone with bin/mm-agent-host-install.sh"
  if [[ "$what" == start ]]; then
    printf '%s\n' '{"corralContributionVersion": 1, "env": {"MM_BUS_UNAVAILABLE": "the host copy has no usable release (RT-6.28)"}, "agentNotes": ["The Mattermost agent bus is off in this session: the host copy has no usable release. The bot token stays on the host, so there is no bus here: do not register a bot or look for a token. Tell your human."], "status": ["no bus: the host copy has no usable release"]}'
  fi
  exit 0
}

# --- where this dispatcher lies ------------------------------------------------
# <host>/<link>.hooks/; the releases are <host>/releases/<name>/. Neither the
# path corral ran, nor this file as a symlink, nor its directory, written or
# resolved, may be in a sandbox's reach.
[[ ! -L "$self" ]] || fail "the hook ${self} is a symlink"
dir="$(cd -P "$(dirname "$self")" 2>/dev/null && pwd -P)" || fail "cannot resolve the directory of ${self}"
host="$(dirname "$dir")" link="${dir##*/}"
link="${link%.hooks}"
rels="$host/releases"
for p in "$self" "$(dirname "$self")" "$dir" "$rels"; do
  ! sandbox_can_write "$p" || fail "${p} can be changed from inside a sandbox"
done
[[ "${dir##*/}" == "$link.hooks" && "$link" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]] ||
  fail "${dir} is not named <link>.hooks"
[[ -d "$rels" && ! -L "$rels" ]] || fail "${rels} is no directory of releases"

# A release directory of this host copy: in releases/ by its own name (as the
# installer names them), owned by this user, no symlink on the way, out of
# every sandbox's reach, with the hook as a regular file.
release_ok() {
  local name="${1##*/}"
  [[ "$1" == "$rels/$name" && "$name" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-dev\.[0-9a-f]{12}|-legacy)?$ ]] || return 1
  [[ -d "$1" && ! -L "$1" && -O "$1" && "$(cd -P "$1" 2>/dev/null && pwd -P)" == "$1" ]] || return 1
  ! sandbox_can_write "$1" || return 1
  [[ ! -L "$1/adapters" && ! -L "$1/adapters/corral" && -f "$1/adapters/corral/$hook" &&
     ! -L "$1/adapters/corral/$hook" ]]
}

target=""
# postEnd: the release preStart recorded for this session, so that it ends
# with the code it started with, while current may point elsewhere by now.
if [[ "$what" == stop ]]; then
  state="${XDG_STATE_HOME:-$HOME/.local/state}/mm-agent-bus"
  key="${CORRAL_SESSION_ID:-}"
  rec_file="$state/$key.release"
  if [[ "$key" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ && -f "$rec_file" && ! -L "$rec_file" ]] &&
     ! sandbox_can_write "$state"; then
    rec="$(head -c 4096 "$rec_file" 2>/dev/null | head -n1)"
    if release_ok "$rec"; then
      target="$rec"
      log "ending this session with release ${rec##*/}, which it started with"
    else
      log "the release recorded for this session is not one of this host copy's releases; ending it with the current one"
    fi
  fi
fi
if [[ -z "$target" ]]; then
  [[ -L "$host/$link" ]] || fail "${host}/${link} is not a link to a release"
  t="$(readlink "$host/$link")"
  [[ "$t" == releases/* ]] && release_ok "$rels/${t#releases/}" ||
    fail "${host}/${link} does not point at a release of this host copy"
  target="$rels/${t#releases/}"
fi
shopt -u nocasematch
MM_BUS_HOOK_DISPATCH=1 exec "$target/adapters/corral/$hook"

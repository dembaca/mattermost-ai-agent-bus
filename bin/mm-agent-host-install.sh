#!/usr/bin/env bash
# Install the corral host hooks where no sandbox can change them.
#
#   mm-agent-host-install.sh --release vX.Y.Z  # a release tag signed by a pinned key
#   bin/mm-agent-host-install.sh             # from the checkout this script is in
#   mm-agent-host-install.sh --from DIR      # from another checkout you reviewed
#   mm-agent-host-install.sh --downgrade ... # allow an older version than current
#   mm-agent-host-install.sh --rollback      # swap current and previous
#   mm-agent-host-install.sh --withdrawn-ok ... # --release/--rollback to a withdrawn version
#   mm-agent-host-install.sh --forget-signers   # remove the release keys pinned on this host
#   mm-agent-host-install.sh --release vX.Y.Z --replace-signers  # pin other keys
#   mm-agent-host-install.sh --check         # compare host copy vs installed plugin
#   mm-agent-host-install.sh --hold          # stop self-updates on this host
#   mm-agent-host-install.sh --unhold        # allow them again
#   mm-agent-host-install.sh --auto          # self-update (corral's end hook runs it)
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
# before, checks its name and the version in its tree and, when
# MM_RELEASE_SIGNERS is set (opt-in; public keys, one per line), its SSH
# signature, and installs git archive of it into releases/X.Y.Z/, marked
# SOURCE=signed, or SOURCE=release VERIFIED=no without the variable (ADR-011
# phase 1b, RT-6.29). `current` is a symlink to one of them, switched in one
# rename once the release is complete; the one before stays as `previous`, and
# --rollback swaps the two. The sandbox neither sees nor writes any of it.
# A --release run with MM_RELEASE_SIGNERS stores the keys in release-signers
# beside releases/ once they verified the tag; later runs and the release check
# use that file when the variable is unset (--forget-signers removes it; other
# keys in the variable are refused unless --replace-signers). A version the
# host's release check saw withdrawn (release-withdrawn.env beside releases/,
# RT-6.30) is refused by --release and --rollback unless --withdrawn-ok.
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
# --auto (ADR-011 phase 3, RT-6.32) is what corral's end hook runs between
# sessions: it reads the registrar's signed release record (verified with
# MM_REGISTRAR_KEYS by this tree's own Python), pins the release keys it offers
# when the host has none yet (and refuses a record with other keys than the
# pinned ones), and installs the version it names as `--release` would (for
# `latest`, the newest release tag the host's release check knows that is not
# withdrawn), with every check: only forward, only a tag signed by a pinned
# key, not withdrawn, not held (--hold), and only when no other session of
# this host copy runs.
# It exits 0 when there is nothing to do and logs one line per decision in
# release-auto.log in the host state directory.
#
# Spec: arc42 §7.1 (docs/arc42/07-deployment-view.md), RT-6.28, RT-6.29, RT-6.32.
set -euo pipefail

here="${0%/*}"
[[ "$here" != "$0" ]] || here=.
SELF="$(cd -P "$here/.." && pwd -P)"
# shellcheck source=mm-agent-lib.sh
source "$SELF/bin/mm-agent-lib.sh"
# Commands only from PATH entries no sandbox can write (arc42 §7.3).
mm_safe_path

# In --auto every refusal is a decision, logged once in release-auto.log.
AUTO=0 AUTO_STATE=""
die() {
  if [[ "$AUTO" -eq 1 ]]; then echo "mm-agent-host-install: not updated: $*" >&2; else echo "mm-agent-host-install: $*" >&2; fi
  [[ "$AUTO" -eq 0 || -z "$AUTO_STATE" ]] || mm_auto_log "$AUTO_STATE" "not updated: $*"
  [[ "$AUTO" -eq 0 || -z "$AUTO_STATE" ]] || mm_auto_state "$AUTO_STATE" "RESULT=failed" "RESULT_AT=$(date +%s)"
  exit 1
}

FROM="" RELEASE="" ENFORCE=0 CHECK=0 ROLLBACK=0 DOWNGRADE=0 WITHDRAWN_OK=0 FORGET=0 MM_REL_REPLACE=0 HOLD=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --from) FROM="${2:-}"; shift 2 ;;
    --release) RELEASE="${2:-}"; [[ -n "$RELEASE" ]] || die "--release takes a version tag v<major>.<minor>.<patch>"; shift 2 ;;
    --check) CHECK=1; shift ;;
    --rollback) ROLLBACK=1; shift ;;
    --downgrade) DOWNGRADE=1; shift ;;
    --withdrawn-ok) WITHDRAWN_OK=1; shift ;;
    --forget-signers) FORGET=1; shift ;;
    --replace-signers) MM_REL_REPLACE=1; shift ;;
    --hold) HOLD=hold; shift ;;
    --unhold) HOLD=unhold; shift ;;
    --auto) AUTO=1; shift ;;
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
[[ "$WITHDRAWN_OK" -eq 0 || -n "$RELEASE" || "$ROLLBACK" -eq 1 ]] || die "--withdrawn-ok goes with --release or --rollback"
[[ "$FORGET" -eq 0 || ( -z "$FROM$RELEASE" && "$CHECK$ROLLBACK$DOWNGRADE$WITHDRAWN_OK$MM_REL_REPLACE" == 00000 ) ]] ||
  die "--forget-signers takes no other option"
[[ "$MM_REL_REPLACE" -eq 0 || -n "$RELEASE" ]] || die "--replace-signers goes with --release"
others="$FROM$RELEASE$CHECK$ROLLBACK$DOWNGRADE$WITHDRAWN_OK$FORGET$MM_REL_REPLACE"
[[ -z "$HOLD" || ( "$AUTO" -eq 0 && "$others" == 000000 ) ]] || die "--$HOLD takes no other option"
[[ "$AUTO" -eq 0 || ( -z "$HOLD" && "$others" == 000000 ) ]] || die "--auto takes no other option"

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
  # The release check (RT-6.30): on the host, from this installer's own tree;
  # its lines say what it found or why it could not tell. It installs nothing.
  if [[ -z "${CORRAL_SANDBOX:-}" ]] && ! mm_sandbox_can_write "$SELF"; then
    bash "$SELF/bin/mm-agent-release-check.sh" || true
    if note="$(mm_release_notice "$have")"; then
      printf 'release:    %s — to install it: %s/bin/mm-agent-host-install.sh --release v<version>\n' "$note" "$DEST"
    fi
    # Self-update (RT-6.32): what the registrar named, the keys, the hold.
    if note="$(mm_release_auto_notice "$have" "$(mm_state_dir)" "$(dirname "$DEST")")"; then
      printf 'self-update: %s\n' "$note"
    fi
  fi
  [[ -n "$want" && "$have" == "$want" ]]
  exit $?
fi

# Inside corral the target is not the host's and the source may be tampered with.
[[ -z "${CORRAL_SANDBOX:-}" ]] || die "run this on the host, not inside a corral session"

# Places a corral sandbox can write (mm_sandbox_can_write): every corral home,
# ~/.claude, which the homes share by default, corral's temporary directories;
# without regard to case. $1 is a resolved path (pwd -P).
sandbox_writable() { mm_sandbox_can_write "$1"; }

# The installer itself runs on the host, in every mode: never from a tree a
# sandbox can write (the plugin cache, a corral home, a session's workdir),
# --release included, whose code otherwise comes from the signed tag. This
# check runs after mm-agent-lib.sh was sourced from that tree, so it catches a
# mistake, not a tampered tree: running the host copy's installer is the
# control (RT-6.29).
not_from_here="run the host copy's installer ($DEST/bin/mm-agent-host-install.sh) or the one of a clone you reviewed"
if [[ -n "${CORRAL_WORKDIR:-}" ]]; then
  wd="$(cd -P "$CORRAL_WORKDIR" 2>/dev/null && pwd -P)" || wd="$CORRAL_WORKDIR"
  shopt -s nocasematch
  [[ "$SELF/" != "$wd/"* && "$SELF/" != "${CORRAL_WORKDIR%/}/"* ]] ||
    die "this installer lies in $SELF, a corral session's workdir, which its agent can change; $not_from_here"
  shopt -u nocasematch
fi
! sandbox_writable "$SELF" ||
  die "this installer lies in $SELF, which can be changed from inside a corral sandbox; $not_from_here"

# Forget the release keys pinned on this host (RT-6.29, decision (4) of
# 2026-11-12): the next --release run and the release check go without them,
# unless MM_RELEASE_SIGNERS is set.
# The keys lie beside releases/ (MM_BUS_HOST_DIR's parent), not in the state
# directory, which XDG_STATE_HOME could move; checked as the reader checks them.
if [[ "$FORGET" -eq 1 ]]; then
  d="$(cd -P "$(dirname "$DEST")" 2>/dev/null && pwd -P)" || d="$(dirname "$DEST")"
  f="$d/$MM_SIGNERS_FILE"
  if [[ -e "$f" || -L "$f" ]]; then
    ! sandbox_writable "$d" || die "$f lies where a corral sandbox can write; refused, nothing removed"
    [[ -f "$f" && ! -L "$f" ]] || die "$f is a symlink or no regular file; refused, nothing removed (look at it, then remove it by hand)"
    rm -f "$f" || die "could not remove $f"
    echo "Forgot the release keys pinned in $f: without MM_RELEASE_SIGNERS, --release installs and the release check run unchecked, with a WARNING."
  else
    echo "No release keys pinned in $f; nothing changed."
  fi
  [[ -z "${MM_RELEASE_SIGNERS+set}" ]] ||
    echo "MM_RELEASE_SIGNERS is set in this shell: runs from it still check the signature, and a --release run pins its keys again."
  exit 0
fi

# Release keys into release-signers in layout directory $1 (decision (4) of
# 2026-11-12; RT-6.32 for those the registrar offers): only `<type> <base64>`
# lines ($2), 0600, in one rename; $3 says what was done. Not when the
# directory is not this user's own or others can write it.
write_signers() {
  local d="$1" f="$1/$MM_SIGNERS_FILE" tmp="$1/.$MM_SIGNERS_FILE.$$" text
  text="# Release keys pinned by mm-agent-host-install.sh (RT-6.29, RT-6.32); --forget-signers removes them."$'\n'"$2"
  if ! mm_own_path "$d"; then
    echo "mm-agent-host-install: WARNING: $d is not this user's own or others can write it: the release keys are not pinned" >&2
    return 1
  fi
  { (umask 077 && printf '%s' "$text" >"$tmp") && chmod 600 "$tmp" && mv -f "$tmp" "$f"; } ||
    { rm -f "$tmp"; echo "mm-agent-host-install: WARNING: could not write $f: the release keys are not pinned" >&2; return 1; }
  echo "$3: later runs and the release check use them when MM_RELEASE_SIGNERS is unset (--forget-signers removes them)."
}

# The host state directory for --hold, --unhold and --auto's log: resolved,
# never one a sandbox can write ("" then).
auto_state_dir() {
  local s
  s="$(mm_state_dir)"
  mkdir -p "$s" 2>/dev/null && chmod 700 "$s" 2>/dev/null
  s="$(cd -P "$s" 2>/dev/null && pwd -P)" || return 0
  sandbox_writable "$s" || printf '%s\n' "$s"
}

# --hold and --unhold (RT-6.32): self-updates stop on this host until
# --unhold. The hold is a file beside releases/ (MM_BUS_HOST_DIR's parent),
# out of XDG_STATE_HOME's reach like release-signers; anything there of that
# name holds, so only a person on the host lifts it.
if [[ -n "$HOLD" ]]; then
  d="$(dirname "$DEST")"
  mkdir -p "$d" 2>/dev/null || die "cannot create $d"
  d="$(cd -P "$d" && pwd -P)"
  f="$d/$MM_HOLD_FILE"
  ! sandbox_writable "$d" || die "$d can be changed from inside a corral sandbox; refused"
  s="$(auto_state_dir)"
  if [[ "$HOLD" == hold ]]; then
    if mm_release_held "$d"; then
      echo "Self-updates are held on this host already ($f)."
    else
      tmp="$d/.$MM_HOLD_FILE.$$"
      { (umask 077 && printf 'held by mm-agent-host-install.sh --hold at %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >"$tmp") &&
        mv -f "$tmp" "$f"; } || { rm -f "$tmp"; die "could not write $f"; }
      echo "Held: this host no longer updates itself ($f) until mm-agent-host-install.sh --unhold; --release still installs by hand."
    fi
    [[ -z "$s" ]] || mm_auto_log "$s" "held by --hold"
  else
    if [[ -d "$f" && ! -L "$f" ]]; then
      die "$f is a directory; look at it, then remove it by hand"
    elif [[ -e "$f" || -L "$f" ]]; then
      rm -f "$f" || die "could not remove $f"
      echo "Unheld: this host updates itself again, between sessions, to the version the registrar names."
    else
      echo "Self-updates are not held on this host; nothing changed."
    fi
    [[ -z "$s" ]] || mm_auto_log "$s" "unheld by --unhold"
  fi
  exit 0
fi

# --- self-update between sessions (ADR-011 phase 3, RT-6.32) -------------------
# corral's end hook runs `--auto` after the session's bot is gone. Every step
# that finds nothing to do or a reason not to says so in one line of
# release-auto.log and exits 0; what goes on is the --release path below with
# all of its checks, the version the registrar named, and no --downgrade,
# --withdrawn-ok or --replace-signers.
auto_say() {
  echo "mm-agent-host-install: $1"
  mm_auto_log "$AUTO_STATE" "$1"
  mm_auto_state "$AUTO_STATE" "RESULT=$2" "RESULT_AT=$(date +%s)"
}
if [[ "$AUTO" -eq 1 ]]; then
  AUTO_STATE="$(auto_state_dir)"
  [[ -n "$AUTO_STATE" ]] || die "no host state directory out of every sandbox's reach; nothing done"
  pd="$(cd -P "$(dirname "$DEST")" 2>/dev/null && pwd -P)" || die "$(dirname "$DEST") does not exist: no host copy to update"
  ! sandbox_writable "$pd" || die "$pd can be changed from inside a corral sandbox; nothing done"
  # The stop switch (guard rail 4), before anything is read.
  if mm_release_held "$pd"; then
    auto_say "held ($pd/$MM_HOLD_FILE): no self-update until --unhold" held
    exit 0
  fi
  # The registrar's record, verified by this tree's own Python with the pinned
  # registrar keys; without them nothing is taken (guard rail 1).
  mm_registrar_release "$SELF/mcp/.venv/bin/python"
  case "$MM_RR_STATUS" in
    nokeys) mm_auto_state "$AUTO_STATE" REGISTRAR=nokeys TARGET= KEYS=
            auto_say "$MM_RR_WHY" nothing; exit 0 ;;
    none) mm_auto_state "$AUTO_STATE" REGISTRAR=none TARGET= KEYS=
          auto_say "the registrar names no release: nothing to do" nothing; exit 0 ;;
    failed) mm_auto_state "$AUTO_STATE" REGISTRAR=failed
            auto_say "${MM_RR_WHY:-the release record was not read}; nothing changed" failed; exit 0 ;;
  esac
  # Keys pinned once (guard rail 1): taken from the registrar only when the
  # host has none, neither release-signers nor MM_RELEASE_SIGNERS; then a
  # different set is refused, and changing keys is a person's step.
  # `latest` (RT-6.32 step 4) stands for the newest tag the host's release
  # check knows that is not withdrawn: `latest (v<x.y.z>)`, `latest` while
  # none is known.
  target="$(mm_release_target "$MM_RR_VERSION" "$pd/$MM_WITHDRAWN_FILE")"
  if ! mm_release_signers "$pd"; then
    mm_auto_state "$AUTO_STATE" REGISTRAR=ok "TARGET=$target" KEYS=bad "AT=$(date +%s)"
    auto_say "no self-update: $MM_REL_ERR" refused
    exit 0
  fi
  keystate="$(mm_release_keystate)"
  mm_auto_state "$AUTO_STATE" REGISTRAR=ok "TARGET=$target" "KEYS=$keystate" "AT=$(date +%s)"
  if [[ "$keystate" == mismatch ]]; then
    auto_say "refused: the registrar's record (key ${MM_RR_KID:-?}) names release keys other than those pinned in $MM_REL_FROM_TEXT; no self-update. Changing keys is a person's step on the host: --release v<version> --replace-signers with MM_RELEASE_SIGNERS" refused
    exit 0
  fi
  if [[ "$keystate" == none ]]; then
    n="$(grep -c . <<<"$MM_RR_KEYS")"
    write_signers "$pd" "$MM_RR_KEYS" "Pinned the $n release key(s) the registrar's signed record offers in $pd/$MM_SIGNERS_FILE" ||
      { auto_say "the release keys the registrar offers could not be pinned; no self-update" failed; exit 0; }
    mm_auto_log "$AUTO_STATE" "pinned the $n release key(s) the registrar offers (its record signed by key ${MM_RR_KID:-?}) in $pd/$MM_SIGNERS_FILE"
    mm_release_signers "$pd" || { auto_say "no self-update: $MM_REL_ERR" refused; exit 0; }
    mm_auto_state "$AUTO_STATE" KEYS=match
  fi
  if [[ -z "$MM_RR_VERSION" ]]; then
    auto_say "the registrar offers keys and names no version: nothing to do" nothing
    exit 0
  fi
  # Only forward, only signed (guard rail 2).
  if [[ "$MM_REL_ENFORCE" -ne 1 ]]; then
    auto_say "no release keys on this host: it never updates by itself (unchecked mode)" refused
    exit 0
  fi
  have="$(version_of "$DEST")"
  # The version to go to: the one named, or latest's tag as the last release
  # check left it (none known yet: the check below finds it).
  if [[ "$MM_RR_VERSION" == latest ]]; then
    want="$(mm_release_latest "$pd/$MM_WITHDRAWN_FILE" || true)" named="$target"
  else
    want="$MM_RR_VERSION" named="v$MM_RR_VERSION"
  fi
  if [[ -n "$want" ]] && ! mm_version_newer "$have" "$want"; then
    auto_say "the registrar names $named, not newer than $have here: nothing to do" current
    exit 0
  fi
  # The withdrawn versions as of now: the host's release check runs first
  # (RT-6.30; it writes release-withdrawn.env), and without that list there
  # is no self-update.
  echo "mm-agent-host-install: running the release check before updating" >&2
  bash "$SELF/bin/mm-agent-release-check.sh" --state-dir "$AUTO_STATE" >&2 ||
    echo "mm-agent-host-install: the release check failed; using the withdrawn versions it wrote before, if any" >&2
  if [[ ! -f "$pd/$MM_WITHDRAWN_FILE" || -L "$pd/$MM_WITHDRAWN_FILE" ]]; then
    auto_say "$named waits: the withdrawn versions are unknown (no release check passed on this host yet)" failed
    exit 0
  fi
  # latest as of this check: its newest tag not withdrawn, whether that tag
  # passed the check or not. The --release path below verifies it; a newest
  # tag that fails there is a refusal, never a step back to an older one.
  if [[ "$MM_RR_VERSION" == latest ]]; then
    named="$(mm_release_target latest "$pd/$MM_WITHDRAWN_FILE")"
    mm_auto_state "$AUTO_STATE" "TARGET=$named"
    if ! want="$(mm_release_latest "$pd/$MM_WITHDRAWN_FILE")"; then
      auto_say "the registrar names latest, and the release check knows no release tag that is not withdrawn: no self-update" failed
      exit 0
    fi
    if ! mm_version_newer "$have" "$want"; then
      auto_say "the registrar names $named, not newer than $have here: nothing to do" current
      exit 0
    fi
  fi
  if by="$(mm_release_withdrawn "$want" "$pd/$MM_WITHDRAWN_FILE")"; then
    auto_say "the registrar names $named, which the CHANGELOG of v$by withdrew: no self-update" refused
    exit 0
  fi
  RELEASE="v$want" VER="$want" TAG="v$want"
  echo "mm-agent-host-install: the registrar names $named (this host runs $have): updating" >&2
fi

# Whether the host's release check (RT-6.30) lists version $1 as withdrawn:
# refused unless --withdrawn-ok (decision (5) of 2026-11-12). From
# release-withdrawn.env beside releases/ ($parent, set by layout(), out of
# every sandbox's reach), which the check writes with release.env: not from the
# state directory, which XDG_STATE_HOME could move. No network; without the
# file nothing is refused, and that is said.
refuse_withdrawn() {
  local by w="$parent/$MM_WITHDRAWN_FILE"
  if [[ ! -e "$w" ]]; then
    echo "mm-agent-host-install: withdrawn versions unknown: no release check on this host yet; run --check" >&2
    return 0
  fi
  by="$(mm_release_withdrawn "$1" "$w")" || return 0
  if [[ "$WITHDRAWN_OK" -eq 1 ]]; then
    echo "mm-agent-host-install: $1 is withdrawn (by the CHANGELOG of v$by, per $w); going on, as --withdrawn-ok asks" >&2
    return 0
  fi
  die "$1 is withdrawn: the CHANGELOG of v$by withdrew it (per the host's release check, $w); $2, or pass --withdrawn-ok to use it on purpose; nothing changed"
}

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
  # The lock holds the installer's pid; one whose installer is gone (killed,
  # a stopped end hook) is taken over by one rename and said so.
  if ! mkdir "$parent/.install.lock" 2>/dev/null; then
    local stale="$parent/.install.lock.stale.$$" was
    mm_install_lock_live "$parent/.install.lock" &&
      die "another installer runs on $parent (remove $parent/.install.lock if none does)"
    was="$(mm_read_file "$parent/.install.lock/pid" 64 2>/dev/null | tr -cd '0-9')" || was=""
    mv "$parent/.install.lock" "$stale" 2>/dev/null && rm -rf "$stale" && mkdir "$parent/.install.lock" 2>/dev/null ||
      die "another installer runs on $parent (remove $parent/.install.lock if none does)"
    echo "mm-agent-host-install: took over the lock of an installer that is gone (pid ${was:-unknown})" >&2
    [[ -z "$AUTO_STATE" ]] || mm_auto_log "$AUTO_STATE" "took over the lock of an installer that is gone (pid ${was:-unknown})"
  fi
  LOCK="$parent/.install.lock"
  printf '%s\n' "$$" >"$LOCK/pid"
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
  # The release check fetches into src.git too (RT-6.30): not at the same
  # time. Its lock, younger than ten minutes, is a check that runs.
  if [[ -n "$STATE" && -d "$STATE/release-check.lock" &&
        -z "$(find "$STATE/release-check.lock" -maxdepth 0 -mmin +10 2>/dev/null)" ]]; then
    die "a release check runs on this host ($STATE/release-check.lock); rerun in a minute"
  fi
}

# On every exit: the half-made release ($new, when set) and the lock.
new="" LOCK=""
cleanup() {
  [[ -z "$new" ]] || rm -rf "$new"
  # The lock is this process's own (mkdir succeeded), with what it put there.
  [[ -z "$LOCK" ]] || rm -rf "$LOCK"
}
trap cleanup EXIT
# Killed by INT, TERM or HUP: exit, so the EXIT trap removes the lock as well.
trap 'exit 1' INT TERM HUP

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
  # The release this installer runs from (the end hook's, under --auto).
  [[ "$SELF" != "$RELEASES"/* ]] || keep+="${SELF##*/} "
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
  [[ -z "$FROM" && "$DOWNGRADE" -eq 0 ]] || die "--rollback takes no other option but --withdrawn-ok"
  layout
  cur="$(release_of "$DEST")" || die "$DEST is not a link to a release; nothing to roll back"
  prev="$(release_of "$PREV")" || die "no previous release to roll back to"
  [[ -f "$RELEASES/$prev/SOURCE" && -e "$RELEASES/$prev/mcp/.venv/bin/python" ]] ||
    die "the previous release $prev has no Python environment; install a release instead"
  # A rollback target can be withdrawn too (decision (5) of 2026-11-12).
  refuse_withdrawn "$(version_of "$RELEASES/$prev")" "install a newer release instead (--release v<version>)"
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
# The pinned keys come from the host only, never from the plugin or a session:
# MM_RELEASE_SIGNERS in the installer's environment (the operator's shell; a
# workdir's .envrc can set it there, so allow only one you reviewed), else the
# file release-signers a keyed run left beside releases/; git runs
# without system or global config, hook or attributes file, fetches over https
# (or from a local path for a mirror) with no credential, and checks every
# object it receives.
if [[ -n "$RELEASE" ]]; then
  # The signature check is opt-in (dembaca, 2026-11-12, decision (3)): with
  # keys it is enforced; without, the release installs with every other check,
  # trusting the repository over https and the tag object pinned in releases/.
  # Set but empty is a mistake, not opt-out: refused. Shared with the release
  # check (mm-agent-lib.sh, RT-6.30): the keys, the URL, the tools, git's clean
  # environment and the tag's checks.
  mm_release_signers "$(dirname "$DEST")" || die "$MM_REL_ERR"
  ENFORCE="$MM_REL_ENFORCE"
  case "$MM_REL_FROM" in
    env) echo "mm-agent-host-install: release keys from MM_RELEASE_SIGNERS" >&2 ;;
    file) echo "mm-agent-host-install: release keys from $MM_REL_FROM_TEXT (pinned by an earlier run; MM_RELEASE_SIGNERS is not set)" >&2 ;;
    *) echo "mm-agent-host-install: WARNING: MM_RELEASE_SIGNERS is not set and no release keys are pinned on this host ($(dirname "$DEST")/$MM_SIGNERS_FILE): the release's signature is NOT checked; set MM_RELEASE_SIGNERS to the release keys to enforce it (a run with it pins them)" >&2 ;;
  esac
  mm_release_url || die "$MM_REL_ERR"
  URL="$MM_REL_URL"
  mm_release_tools || die "$MM_REL_ERR"
  # Keys from MM_RELEASE_SIGNERS that verified the tag become the host's
  # (decision (4) of 2026-11-12): release-signers beside releases/ ($parent,
  # which layout() refused when a sandbox can write it; not the state
  # directory, which XDG_STATE_HOME could move), only `<type> <base64>` lines,
  # 0600, in one rename. mm_release_signers checked an existing file already
  # and refused other keys without --replace-signers; the same keys change
  # nothing.
  store_signers() {
    local f="$parent/$MM_SIGNERS_FILE" verb
    if [[ -n "$MM_REL_PINNED" ]]; then
      [[ "$(sort -u <<<"$MM_REL_KEYS")" != "$(sort -u <<<"$MM_REL_PINNED")" ]] || return 0
      verb="Replaced the release keys pinned in $f with those of MM_RELEASE_SIGNERS (--replace-signers)"
    else
      verb="Pinned the release keys of MM_RELEASE_SIGNERS in $f"
    fi
    write_signers "$parent" "$MM_REL_KEYS" "$verb" || true
  }
  # verify-tag's temporary files go into the installer's own lock directory,
  # which only the host sees (set by layout()).
  rgit() { MM_REL_GIT_DIR="$SRC_GIT" MM_REL_TMP="$LOCK" mm_rgit "$@"; }
  # Name, signature, then tree; any refusal before the switch leaves current as it was.
  stage_release() {
    local out old_id tag_id obj f v_py v_init
    [[ -d "$SRC_GIT" ]] ||
      GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git init -q --bare --template= "$SRC_GIT" ||
      die "could not create $SRC_GIT"
    old_id="$(rgit rev-parse -q --verify "refs/tags/$TAG" || true)"
    # No + in the refspec: a tag fetched before is never moved (git refuses to clobber it).
    if ! out="$(rgit fetch --no-tags --no-recurse-submodules --no-write-fetch-head \
          "$URL" "refs/tags/$TAG:refs/tags/$TAG" 2>&1)"; then
      if [[ -n "$old_id" && "$out" == *"would clobber existing tag"* ]]; then
        die "the tag $TAG at $URL moved since this host fetched it (it was ${old_id:0:12}): a release tag never moves; refused, $DEST is unchanged"
      fi
      die "could not fetch $TAG from $URL; $DEST is unchanged:"$'\n'"$(tail -n 5 <<<"$out" | tr -cd '[:print:]\n')"
    fi
    tag_id="$(rgit rev-parse -q --verify "refs/tags/$TAG")" || die "$TAG is not in $SRC_GIT after the fetch"
    # Annotated in both modes, so the same tags serve hosts with and without
    # keys; with keys its SSH signature by a pinned key; its own name; a commit.
    MM_REL_GIT_DIR="$SRC_GIT" MM_REL_TMP="$LOCK" mm_release_tag "$TAG" "$tag_id" ||
      die "$MM_REL_ERR; refused, $DEST is unchanged"
    SIGNER="$MM_REL_SIGNER" obj="$MM_REL_COMMIT"
    [[ "$MM_REL_FROM" != env ]] || store_signers
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
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0 GIT_NO_REPLACE_OBJECTS=1 \
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
# Between sessions only (guard rail 3): no other session of this host copy may
# run, by its release record or a live session file (this session's own record
# went before the end hook called this, and its own key is left out); a state
# directory that cannot tell counts as busy.
if [[ "$AUTO" -eq 1 ]]; then
  busy="$( { recorded_keys; unrecorded_sessions; } | grep -vxF -e "${CORRAL_SESSION_ID:-?}" | sort -u | tr '\n' ' ' || true)"
  if [[ "$STATE_OK" -eq 0 ]]; then
    auto_say "$named waits: the state directory cannot tell which sessions run" waiting
    exit 0
  fi
  if [[ -n "$busy" ]]; then
    auto_say "$named waits: other sessions of this host copy run (${busy% }); the last one to end updates it" waiting
    exit 0
  fi
fi
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
  refuse_withdrawn "$VER" "install a newer release instead"
  stage_release
  # The same version from another tag object (src.git deleted, the tag signed
  # again): a release never moves, whether it is current, previous or
  # only kept; neither "already current" nor a replacement.
  was=""
  if [[ -f "$RELEASES/$VER/SOURCE" && ! -L "$RELEASES/$VER/SOURCE" ]]; then
    was="$(sed -n 's/^TAG_OBJECT=//p' "$RELEASES/$VER/SOURCE" | head -n1 | tr -cd '0-9a-f')"
  fi
  [[ -z "$was" || "$was" == "$TAG_ID" ]] ||
    die "$TAG was installed on this host from tag object ${was:0:12}, upstream it is now ${TAG_ID:0:12}: a release never moves; refused (remove $RELEASES/$VER on purpose to accept it)"
  ORIGIN="$URL, tag $TAG"
  name="$VER"
  if [[ "$ENFORCE" -eq 1 ]]; then
    source_text="SOURCE=signed"$'\n'"TAG=$TAG"$'\n'"TAG_OBJECT=$TAG_ID"$'\n'"COMMIT=$head_sha"$'\n'"SIGNER=$SIGNER"$'\n'
  else
    # A release tag whose signature this host did not check (opt-in, RT-6.29).
    source_text="SOURCE=release"$'\n'"VERIFIED=no"$'\n'"TAG=$TAG"$'\n'"TAG_OBJECT=$TAG_ID"$'\n'"COMMIT=$head_sha"$'\n'
  fi
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

# A release dir kept for the same tag object (checked above) keeps its SOURCE,
# never marked down: one verified before stays signed in an unchecked run. A
# run that verified the signature marks one installed unchecked as signed.
mark_signed() {
  local s="$RELEASES/$1/SOURCE" tmp
  [[ -n "$RELEASE" && "$ENFORCE" -eq 1 && -f "$s" && ! -L "$s" ]] || return 0
  ! grep -qx 'SOURCE=signed' "$s" || return 0
  tmp="$RELEASES/$1/.SOURCE.$$"
  { printf '%s' "$source_text" >"$tmp" && chmod go-w "$tmp" && mv -f "$tmp" "$s"; } ||
    { rm -f "$tmp"; echo "mm-agent-host-install: WARNING: could not mark $1 as signed" >&2; }
}
cur="$(release_of "$DEST" || true)"
rel="$RELEASES/$name"
if [[ "$cur" == "$name" ]]; then
  # The same commit again: it is current, complete and in use; nothing changes.
  rm -rf "$new"
  new=""
  mark_signed "$name"
  echo "mattermost-ai-agent-bus $name is already the current release in $DEST; nothing changed"
  dispatch_line=""
  install_dispatcher "$name"
  [[ -z "$dispatch_line" ]] || echo "$dispatch_line"
  collect_garbage
  exit 0
fi
# A release from a tag is reused only for the same tag object.
if [[ -d "$rel" && ! -L "$rel" && -f "$rel/SOURCE" && -e "$rel/mcp/.venv/bin/python" ]] &&
   { [[ -z "$RELEASE" ]] || grep -qx "TAG_OBJECT=$TAG_ID" "$rel/SOURCE"; }; then
  # Installed before and complete (SOURCE comes last): switch back to it.
  rm -rf "$new"
  new=""
  mark_signed "$name"
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

[[ "$AUTO" -eq 0 ]] || auto_say "updated to $named ($have before), signed release $TAG, signed by $SIGNER" updated
if [[ -n "$RELEASE" && "$ENFORCE" -eq 1 ]]; then
  kind="signed release $TAG, signed by $SIGNER, a key pinned in $MM_REL_FROM_TEXT"
  head_line="$head_sha (tag object $TAG_ID)"
  check="Verified: the tag's signature, its name and the version in its tree."
elif [[ -n "$RELEASE" ]]; then
  kind="release $TAG, signature NOT checked: MM_RELEASE_SIGNERS is not set and no keys are pinned on this host"
  head_line="$head_sha (tag object $TAG_ID)"
  check="Checked: the tag's name, its object (pinned in releases/) and the version in its tree; not its signature."
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

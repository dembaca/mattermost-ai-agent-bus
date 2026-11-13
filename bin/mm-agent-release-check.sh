#!/usr/bin/env bash
# Which release is the newest that passes this host's check, and what it needs.
#
#   mm-agent-release-check.sh                     # check now
#   mm-agent-release-check.sh --if-due SECONDS    # only when the last try is that old
#   mm-agent-release-check.sh --state-dir DIR     # write release.env there
#
# Runs on the HOST only (the installer's --check, and the MCP server once a day
# as a bounded child, RT-6.30), never inside corral. Installs nothing and runs
# nothing from a tag. It lists the v<x.y.z> tags at MM_RELEASE_URL, fetches the
# newest (at most three, newest first, until one passes) into src.git beside
# releases/ (MM_BUS_HOST_DIR's parent) with --depth=1 and without `+`, so a tag
# fetched before never moves, and checks each as the installer does (RT-6.29):
# an annotated tag, its own name, a commit, the version in its plugin.json, and
# with keys its SSH signature by a pinned key: MM_RELEASE_SIGNERS when set,
# else the release-signers file the installer pinned beside releases/
# (mm_release_signers; the output says which). From the
# CHANGELOG.md of the first that passes it reads the entries up to its version:
# `### Breaking`, `Needs:` lines in their grammar (mm_release_needs_ok; any
# other is dropped and logged) and `Withdrawn: v<x.y.z>` lines. It writes them
# to release.env in the host state directory (0600, in one rename), and NEWEST
# with the withdrawn versions to release-withdrawn.env beside releases/, for
# the installer (RT-6.29 step 2). A tag that
# does not pass is logged once (release-rejected.env) and never shown. A check
# that fails leaves release.env as it was; release-check.at records the last
# check that succeeded (the MCP server counts the failed tries itself).
#
# Spec: ADR-011 point 3, RT-6.30 (docs/arc42/06-runtime-view.md).
set -euo pipefail

here="${0%/*}"
[[ "$here" != "$0" ]] || here=.
SELF="$(cd -P "$here/.." && pwd -P)"
# shellcheck source=mm-agent-lib.sh
source "$SELF/bin/mm-agent-lib.sh"
mm_safe_path
cd /

die() { echo "mm-agent-release-check: $*" >&2; exit 1; }
say() { echo "mm-agent-release-check: $*" >&2; }

MAX_TRIES=3 MAX_ENTRIES=60 MAX_NEEDS=40 MAX_WITHDRAWN=20 CHANGELOG_MAX=$((4 << 20))
DUE="" STATE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --if-due) DUE="${2:-}"; [[ "$DUE" =~ ^[0-9]+$ ]] || die "--if-due takes seconds"; shift 2 ;;
    --state-dir) STATE="${2:-}"; [[ "$STATE" == /* ]] || die "--state-dir takes an absolute path"; shift 2 ;;
    -h|--help) sed -n '2,/^set -euo/p' "$0" | sed '$d'; exit 0 ;;
    *) die "unknown argument" ;;
  esac
done

[[ -z "${CORRAL_SANDBOX:-}" ]] || die "runs on the host, not inside a corral session"
! mm_sandbox_can_write "$SELF" || die "$SELF can be changed from inside a corral sandbox; not run from there"

# The state directory: the host's, nowhere a sandbox writes.
STATE="${STATE:-$(mm_state_dir)}"
{ mkdir -p "$STATE" && chmod 700 "$STATE"; } 2>/dev/null || die "cannot create $STATE"
STATE="$(cd -P "$STATE" && pwd -P)"
! mm_sandbox_can_write "$STATE" || die "$STATE can be changed from inside a corral sandbox; nothing written"
[[ ! -L "$STATE/release.env" ]] || die "$STATE/release.env is a symlink; refused"

# src.git beside releases/, as the installer has it (RT-6.29 step 3).
DEST="${MM_BUS_HOST_DIR:-$HOME/.local/share/mm-agent-bus/current}"
[[ "$DEST" == /* ]] || die "MM_BUS_HOST_DIR must be an absolute path"
parent="$(dirname "$DEST")"
mkdir -p "$parent" 2>/dev/null || die "cannot create $parent"
parent="$(cd -P "$parent" && pwd -P)"
! mm_sandbox_can_write "$parent" || die "$parent can be changed from inside a corral sandbox; refused"
SRC_GIT="$parent/src.git"
[[ ! -L "$SRC_GIT" ]] || die "$SRC_GIT is a symlink; refused"

# The keys: MM_RELEASE_SIGNERS, else release-signers beside releases/, where
# the installer pinned them (RT-6.30 step 1); a bad file fails the check,
# never skips the signature.
mm_release_signers "$parent" || die "$MM_REL_ERR; release.env unchanged"
mm_release_url || die "$MM_REL_ERR"
mm_release_tools || die "$MM_REL_ERR"

# One check at a time; a lock a killed check left goes after ten minutes.
LOCK="$STATE/release-check.lock"
if ! mkdir -m 700 "$LOCK" 2>/dev/null; then
  # Taken over by one rename, so two checks never both remove it; a lock that
  # turns out fresh after the rename (another check just made it) goes back.
  stale="$LOCK.stale.$$"
  if [[ -n "$(find "$LOCK" -maxdepth 0 -mmin +10 2>/dev/null)" ]] && mv "$LOCK" "$stale" 2>/dev/null; then
    if [[ -z "$(find "$stale" -maxdepth 0 -mmin +10 2>/dev/null)" ]]; then
      mv "$stale" "$LOCK" 2>/dev/null || rm -rf "$stale"
      say "another check runs"
      exit 0
    fi
    rm -rf "$stale"
    mkdir -m 700 "$LOCK" 2>/dev/null || exit 0
  else
    say "another check runs"
    exit 0
  fi
fi
trap 'rm -rf "$LOCK"' EXIT
trap 'exit 130' INT TERM HUP

now="$(date +%s)"
if [[ -n "$DUE" ]]; then
  last="$(mm_read_file "$STATE/release-check.at" 64 2>/dev/null | tr -cd '0-9')" || last=""
  if [[ -n "$last" ]] && (( now - last < DUE )); then exit 0; fi
fi
# The registrar's release record, for the notice only (RT-6.32): what it
# names and how its keys relate to the pinned ones, in release-auto.env. Read
# and verified by this tree's own Python with MM_REGISTRAR_KEYS; nothing is
# pinned or installed here (the installer's --auto does that, between
# sessions, after reading it again). A failed read keeps what was there.
mm_registrar_release "$SELF/mcp/.venv/bin/python"
case "$MM_RR_STATUS" in
  ok) mm_auto_state "$STATE" REGISTRAR=ok "TARGET=$MM_RR_VERSION" "KEYS=$(mm_release_keystate)" "AT=$now"
      say "the registrar names ${MM_RR_VERSION:+v}${MM_RR_VERSION:-no version} (its record verified; release keys: $(mm_release_keystate))" ;;
  none | nokeys) mm_auto_state "$STATE" "REGISTRAR=$MM_RR_STATUS" TARGET= KEYS= "AT=$now" ;;
  *) mm_auto_state "$STATE" REGISTRAR=failed
     say "${MM_RR_WHY:-the release record was not read}" ;;
esac

# The installer fetches into src.git: not at the same time.
if [[ -e "$parent/.install.lock" ]]; then
  say "the installer runs; not checked"
  exit 0
fi
export MM_REL_GIT_DIR="$SRC_GIT" MM_REL_TMP="$LOCK"
[[ -d "$SRC_GIT" ]] ||
  GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null git init -q --bare --template= "$SRC_GIT" ||
  die "could not create $SRC_GIT"

# The tags, by name only; nothing of them is shown before it passed.
if ! out="$(mm_rgit ls-remote --tags --refs "$MM_REL_URL" 'refs/tags/v*' 2>&1)"; then
  die "could not list the tags at $MM_REL_URL; release.env unchanged:"$'\n'"$(tail -n 3 <<<"$out" | tr -cd '[:print:]\n')"
fi
candidates="$(sed -n 's|^[0-9a-f]*[[:space:]]*refs/tags/v\([0-9]*\.[0-9]*\.[0-9]*\)$|\1|p' <<<"$out" |
              grep -E '^[0-9]+\.[0-9]+\.[0-9]+$' | sort -t. -k1,1nr -k2,2nr -k3,3nr | head -n "$MAX_TRIES" || true)"
[[ -n "$candidates" ]] || die "no release tag at $MM_REL_URL; release.env unchanged"

# A tag that does not pass is logged once per tag object, never shown.
rejected() {
  local ver="$1" id="$2" why="$3" f="$STATE/release-rejected.env"
  if ! grep -qxF "$ver ${id:-none}" "$f" 2>/dev/null; then
    say "v$ver does not pass this host's check, not shown: $why"
    { tail -n 19 "$f" 2>/dev/null || true; printf '%s %s\n' "$ver" "${id:-none}"; } >"$f.$$" && mv -f "$f.$$" "$f"
  fi
}

newest="" tag_id="" commit="" fetch_err=""
while IFS= read -r ver; do
  tag="v$ver"
  # --depth=1: the tag, its commit and tree, no history. No +: a tag this host
  # fetched before (here or by the installer) never moves.
  if ! out="$(mm_rgit fetch --depth=1 --no-tags --no-recurse-submodules --no-write-fetch-head \
          "$MM_REL_URL" "refs/tags/$tag:refs/tags/$tag" 2>&1)"; then
    if [[ "$out" == *"would clobber existing tag"* ]]; then
      rejected "$ver" "$(mm_rgit rev-parse -q --verify "refs/tags/$tag" || true)" "the tag moved since this host fetched it"
      continue
    fi
    # Not fetched (gone upstream, a network error): the next older one.
    fetch_err="could not fetch $tag:"$'\n'"$(tail -n 3 <<<"$out" | tr -cd '[:print:]\n')"
    say "$fetch_err"
    continue
  fi
  id="$(mm_rgit rev-parse -q --verify "refs/tags/$tag")" || { rejected "$ver" "" "not fetched"; continue; }
  if ! mm_release_tag "$tag" "$id"; then
    rejected "$ver" "$id" "$(head -n1 <<<"$MM_REL_ERR")"
    continue
  fi
  pv="$(mm_rgit cat-file -p "$MM_REL_COMMIT:.claude-plugin/plugin.json" 2>/dev/null | head -c 65536 |
        sed -n 's/^[[:space:]]*"version":[[:space:]]*"\([^"]*\)".*/\1/p' | head -n1 || true)"
  if [[ "$pv" != "$ver" ]]; then
    rejected "$ver" "$id" "its tree says another version"
    continue
  fi
  newest="$ver" tag_id="$id" commit="$MM_REL_COMMIT"
  break
done <<<"$candidates"
[[ -n "$newest" ]] || die "none of the newest tags passes this host's check; release.env unchanged${fetch_err:+ (last: ${fetch_err%%$'\n'*})}"

size="$(mm_rgit cat-file -s "$commit:CHANGELOG.md" 2>/dev/null || echo 0)"
[[ "$size" =~ ^[0-9]+$ && "$size" -le "$CHANGELOG_MAX" ]] || die "v$newest has no readable CHANGELOG.md; release.env unchanged"

# The entries up to the release's own version, newest first, in their grammar.
entries="" needs="" withdrawn="" n_needs=0 n_withdrawn=0 dropped=0
flush() { [[ -z "$cur" ]] || entries+="ENTRY=$cur $brk"$'\n'; }
cur="" brk="-"
while IFS= read -r line; do
  kind="${line%% *}" line="${line#* }"
  case "$kind" in
    E) flush; cur="" brk="-"
       ! mm_version_newer "$newest" "$line" || continue
       cur="$line" ;;
    B) [[ -n "$cur" ]] && brk=breaking ;;
    N) [[ -n "$cur" ]] || continue
       if mm_release_needs_ok "$line" && (( n_needs < MAX_NEEDS )); then
         needs+="NEEDS=$cur $line"$'\n'
         n_needs=$((n_needs + 1))
       else
         dropped=$((dropped + 1))
       fi ;;
    W) [[ -n "$cur" ]] || continue
       if [[ "$line" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(,\ v[0-9]+\.[0-9]+\.[0-9]+){0,9}$ ]]; then
         for w in ${line//,/ }; do
           w="${w#v}"
           if mm_version_newer "$w" "$cur" && (( n_withdrawn < MAX_WITHDRAWN )); then
             withdrawn+="WITHDRAWN=$w"$'\n'
             n_withdrawn=$((n_withdrawn + 1))
           fi
         done
       else
         dropped=$((dropped + 1))
       fi ;;
  esac
done < <(mm_rgit cat-file -p "$commit:CHANGELOG.md" | head -c "$CHANGELOG_MAX" | tr -d '\r' |
         awk -v max="$MAX_ENTRIES" '
           /^## / { if ($0 ~ /^## \[?[0-9]+\.[0-9]+\.[0-9]+\]?[ \t]*$/) { v = $2; gsub(/\[|\]/, "", v); if (++n > max) exit; print "E " v; on = 1 } else on = 0; next }
           !on { next }
           /^### Breaking[ \t]*$/ { print "B x"; next }
           /^Needs: / { print "N " substr($0, 8); next }
           /^Withdrawn: / { print "W " substr($0, 12); next }')
flush
[[ "$dropped" -eq 0 ]] || say "v$newest: $dropped Needs:/Withdrawn: line(s) outside their grammar dropped"

if [[ "$MM_REL_ENFORCE" -eq 1 ]]; then verified=yes; else verified=no; fi
tmp="$STATE/.release.env.$$"
{
  printf '# The host'"'"'s release check (RT-6.30): written by mm-agent-release-check.sh only.\n'
  printf 'FORMAT=1\nCHECKED_AT=%s\nNEWEST=%s\nTAG_OBJECT=%s\nCOMMIT=%s\nVERIFIED=%s\n' \
    "$now" "$newest" "$tag_id" "$commit" "$verified"
  [[ -z "$MM_REL_SIGNER" ]] || printf 'SIGNER=%s\n' "$MM_REL_SIGNER"
  printf '%s%s%s' "$entries" "$needs" "$withdrawn"
} >"$tmp" && chmod 600 "$tmp" && mv -f "$tmp" "$STATE/release.env" ||
  { rm -f "$tmp"; die "could not write $STATE/release.env"; }
# The withdrawn versions for the installer, beside releases/ (decision (5)):
# where XDG_STATE_HOME cannot move them.
wtmp="$parent/.$MM_WITHDRAWN_FILE.$$"
if [[ -L "$parent/$MM_WITHDRAWN_FILE" ]] ||
   ! { printf 'NEWEST=%s\n%s' "$newest" "$withdrawn" >"$wtmp" && chmod 600 "$wtmp" &&
       mv -f "$wtmp" "$parent/$MM_WITHDRAWN_FILE"; }; then
  rm -f "$wtmp"
  say "could not write $parent/$MM_WITHDRAWN_FILE; the installer does not know the withdrawn versions"
fi
# The last check that succeeded: --if-due counts from it.
printf '%s\n' "$now" >"$STATE/release-check.at"
if [[ "$verified" == yes ]]; then
  echo "newest release v$newest, its signature checked (signed by $MM_REL_SIGNER; keys from $MM_REL_FROM_TEXT)"
else
  echo "newest release v$newest, its signature not checked (MM_RELEASE_SIGNERS is not set and no keys are pinned in $parent/$MM_SIGNERS_FILE)"
fi

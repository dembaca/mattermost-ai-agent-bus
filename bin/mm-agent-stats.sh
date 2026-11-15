#!/usr/bin/env bash
# Coordination overhead per plugin version (RT-6.9), from the STATS lines that
# every session posts to MM_STATS_CHANNEL: one per day of session time and one
# at its end (since 0.91.0; before, only at the end).
#
#   mm-agent-stats.sh report [channel]   # default: MM_STATS_CHANNEL, else agents-stats
#   mm-agent-stats.sh report --json      # the aggregated numbers as JSON
#
# Columns: sessions (a session's daily parts count once), hours on the bus,
# turns the plugin caused per hour (prompts the bus started — pushes, listener
# wake-ups — + rounds the Stop hook forced), their share of all turns (every
# prompt plus the forced rounds), bus data put into the context per turn (tool
# results + compaction handoff), characters posted per session, the fixed text
# in every turn (instructions, tool docs; before 0.55.0 the skill too) and the
# skill, read per load. Characters, not tokens: roughly 4 per token.
# Claude Code sessions since 0.32.0 add a token table from their transcripts:
# requests per hour, those the bus caused, the fixed text per request, sessions
# that loaded the skill (since 0.55.0), the bus's tokens per hour and its share
# of all, in input-token equivalents.
# A session reads the same as JSON with the MCP tool stats_report, no token.
# Since 0.36.0, per version and coding harness (the MCP client: claude-code,
# cursor, opencode, codex, …), a Turns table (turns per human prompt, mean and
# longest turn, the share of turn time the bus started on its own, turn time per
# human input) and an Asks table (asks per hour, share answered by a trusted
# human, mean and longest wait, asks open at the session's end, the share of
# time spent waiting for answers). Since 0.91.0 a Saved waiting table (answers
# on the bus while the terminal was idle, the time until the human was back
# there) and a Roles table (coordinator: the crown more than half the session).
# Missing data shows "-".
# Read-only; needs a bot token and reads at most the last 2000 posts.
set -euo pipefail

LIB="$(cd "$(dirname "$0")" && pwd)/mm-agent-lib.sh"
# shellcheck source=mm-agent-lib.sh
source "$LIB"

CMD="${1:-}" CHANNEL="" JSON=0
[[ "$CMD" == report ]] || { sed -n '2,29p' "$0"; exit 2; }
shift
for a in "$@"; do
  case "$a" in
    --json) JSON=1 ;;
    *) CHANNEL="$a" ;;
  esac
done
CHANNEL="${CHANNEL:-${MM_STATS_CHANNEL:-agents-stats}}"
CHANNEL="${CHANNEL#\~}"
[[ "$CHANNEL" =~ ^(off|none|0|false)$ ]] && { echo "stats are off (MM_STATS_CHANNEL=$CHANNEL); pass a channel" >&2; exit 2; }

CID="$(mm_work_channel_id "$CHANNEL")" || exit 1
# A non-member cannot read a channel; joining a public one is harmless.
if [[ -n "${MM_BOT_USER_ID:-}" ]]; then
  mm_api POST "/api/v4/channels/${CID}/members" "{\"user_id\":\"${MM_BOT_USER_ID}\"}" >/dev/null || true
fi

lines="[]"
for page in 0 1 2 3 4 5 6 7 8 9; do
  mm_api GET "/api/v4/channels/${CID}/posts?per_page=200&page=${page}" || break
  [[ "$MM_API_STATUS" == "200" ]] || { echo "could not read ~${CHANNEL}: HTTP ${MM_API_STATUS}" >&2; exit 1; }
  got="$(jq -c '[.posts // {} | .[] | .message | select(startswith("STATS "))
                 | ltrimstr("STATS ") | (try fromjson catch empty) | select(type == "object")]' <<<"$MM_API_BODY")"
  lines="$(jq -c --argjson a "$lines" --argjson b "$got" -n '$a + $b')"
  [[ "$(jq '.order | length' <<<"$MM_API_BODY")" -eq 200 ]] || break
done

report="$(jq -c '
  def sum(f): map(f) | add // 0;
  # Distinct sessions: the parts of one session share its random sid (since
  # 0.91.0); an older line is a session of its own.
  def sessions: map(._s) | unique | length;
  def hours: sum(.min) / 60 * 10 | round / 10;
  # Input-token equivalents: what a token type costs against an uncached input token.
  def eq(t): (t["in"] // 0) + (t.read // 0) * 0.1 + (t.w5m // 0) * 1.25
             + (t.w1h // 0) * 2 + (t.out // 0) * 5;
  def pct(a; b): if b > 0 then (a * 100 / b | round) else null end;
  # Turns and asks of the sessions of one harness (since 0.36.0); null without the
  # fields, so a harness without turn hooks shows a dash, never a zero.
  def turns_of: map(select(.turns_timed != null)) | if length == 0 then null else {
      sessions: sessions,
      turns: sum(.turns),
      human_prompts: sum(.turns - (.bus_turns // 0)),
      timed: sum(.turns_timed),
      sec: sum(.turn_sec // 0),
      max_sec: (map(.turn_max_sec // 0) | max),
      bus_turns: sum(.bus_turns // 0),
      bus_timed: sum(.bus_turns_timed // 0),
      bus_sec: sum(.bus_turn_sec // 0),
      bus_max_sec: (map(.bus_turn_max_sec // 0) | max),
      answered: sum(.asks_answered // 0)
    } | . + {
      turns_per_prompt: (if .human_prompts > 0 then (.turns / .human_prompts * 10 | round / 10) else null end),
      mean_sec: (if .timed > 0 then (.sec / .timed | round) else null end),
      bus_mean_sec: (if .bus_timed > 0 then (.bus_sec / .bus_timed | round) else null end),
      bus_share_pct: pct(.bus_sec; .sec),
      sec_per_input: (if .human_prompts + .answered > 0 then (.sec / (.human_prompts + .answered) | round) else null end)
    } end;
  def asks_of: map(select(.asks != null)) | if length == 0 then null else {
      sessions: sessions,
      hours: (sum(.min) / 60),
      asks: sum(.asks),
      answered: sum(.asks_answered // 0),
      wait_sec: sum(.ask_wait_sec // 0),
      max_wait_sec: (map(.ask_wait_max_sec // 0) | max),
      open: sum(.asks_open // 0),
      open_sec: sum(.ask_open_sec // 0),
      open_max_sec: (map(.ask_open_max_sec // 0) | max)
    } | . + {
      asks_per_h: (if .hours > 0 then (.asks / .hours * 10 | round / 10) else null end),
      answered_pct: pct(.answered; .asks),
      mean_wait_sec: (if .answered > 0 then (.wait_sec / .answered | round) else null end),
      waiting_pct: (if .hours > 0 then ((.wait_sec + .open_sec) * 100 / (.hours * 3600) | round) else null end)
    } | .hours |= (. * 10 | round / 10) end;
  # Saved waiting (since 0.91.0, harnesses with turn hooks): answers on the bus
  # while the terminal was idle, and the time until the human was back there.
  def saved_of: map(select(.asks_idle != null)) | if length == 0 then null else {
      sessions: sessions,
      answered: sum(.asks_answered // 0),
      idle: sum(.asks_idle // 0),
      saved_n: sum(.saved_n // 0),
      saved_sec: sum(.saved_sec // 0),
      saved_max_sec: (map(.saved_max_sec // 0) | max),
      saved_open_n: sum(.saved_open_n // 0),
      saved_open_sec: sum(.saved_open_sec // 0)
    } | . + {
      idle_pct: pct(.idle; .answered),
      mean_saved_sec: (if .saved_n > 0 then (.saved_sec / .saved_n | round) else null end)
    } end;
  def per_h(x; h): if h > 0 then (x / h | round) else null end;
  def tok_of: map(select(.tok)) | if length == 0 then null else {
      sessions: sessions,
      hours: (sum(.min) / 60),
      req: sum(.tok.req),
      bus_req: sum(.tok.bus_req),
      fixed: (sum(.tok.fixed) / length | round),
      skill_loaded: (map(select((.tok.skill // 0) > 0) | ._s) | unique | length),
      all: sum(eq(.tok.all)),
      bus: sum(eq(.tok.bus))
    } | . + {
      req_per_h: per_h(.req; .hours),
      bus_req_per_h: (if .hours > 0 then (.bus_req / .hours * 10 | round / 10) else null end),
      bus_per_h: per_h(.bus; .hours),
      all_per_h: per_h(.all; .hours),
      bus_share_pct: (if .all > 0 then (.bus * 1000 / .all | round / 10) else null end)
    } end;
  to_entries | map(.value + {_s: (.value.sid // "#\(.key)")})
  # A session is the coordinator when it held the crown more than half its time
  # (all its parts); unknown before 0.91.0 (no crown_sec).
  | (group_by(._s) | map({key: .[0]._s, value:
      (if all(.[]; .crown_sec == null) then "unknown"
       elif sum(.crown_sec // 0) * 2 > sum(.min) * 60 then "coordinator" else "worker" end)})
     | from_entries) as $roles
  | group_by(.v) | map(tok_of as $tok
| {
    version: .[0].v,
    sessions: sessions,
    parts: length,
    hours: hours,
    # `turns` counts every prompt, those the bus started (a push, a listener that
    # ended) included; the rounds the Stop hook forced are not prompts. Before
    # 0.36.0 (no bus_turns) the listener'"'"'s wake-ups stand in for the bus'"'"'s
    # turns, pushes uncounted.
    turns: sum(.turns),
    forced: sum((.blocks // {}) | add // 0),
    plugin_turns: sum((.bus_turns // ((.wakes // {}) | add // 0)) + ((.blocks // {}) | add // 0)),
    bus_chars: sum(((.tools // {}) | map(.[1]) | add // 0) + ((.compact // [0, 0])[1])),
    post_chars: sum((.posts // [0, 0])[1]),
    # Since 0.55.0 static is the per-turn text only, the skill a field of its own.
    static: (map(.static) | max),
    skill: (map(.skill) | max),
    tok: $tok,
    saved: saved_of,
    harness: (group_by(.rt // "other") | map({
      harness: (.[0].rt // "other"), sessions: sessions, turns: turns_of, asks: asks_of, saved: saved_of
    })),
    roles: (group_by($roles[._s]) | map({
      role: $roles[.[0]._s], sessions: sessions, hours: hours, tok: tok_of, turns: turns_of, asks: asks_of
    }))
  } | . + {
    plugin_turns_per_h: (if .hours > 0 then (.plugin_turns / .hours * 10 | round / 10) else null end),
    all_turns: (.turns + .forced)
  } | . + {
    plugin_share_pct: (if .all_turns > 0 then (.plugin_turns * 100 / .all_turns | round) else null end),
    bus_chars_per_turn: (if .all_turns > 0 then (.bus_chars / .all_turns | round) else null end),
    post_chars_per_session: (.post_chars / .sessions | round)
  }) | sort_by(.version | split(".") | map(tonumber? // 0))' <<<"$lines")"

if [[ "$JSON" -eq 1 ]]; then
  printf '%s\n' "$report"
  exit 0
fi
[[ "$(jq length <<<"$report")" -gt 0 ]] || { echo "no STATS posts in ~${CHANNEL} yet"; exit 0; }
{
  printf 'version\tsessions\thours\tplugin turns/h\tshare of turns\tbus chars/turn\tposted chars/session\tfixed chars/turn\tskill chars/load\n'
  jq -r '.[] | [.version, .sessions, .hours, (.plugin_turns_per_h // "-"),
                ((.plugin_share_pct // "-") | tostring + "%"), (.bus_chars_per_turn // "-"),
                .post_chars_per_session, (.static // "-"), (.skill // "-")] | @tsv' <<<"$report"
} | column -t -s $'\t'
# Tokens from the transcript (Claude Code sessions since 0.32.0), in input-token
# equivalents: cache read 0.1, cache write 1.25 (5 min) / 2 (1 h), output 5.
if [[ "$(jq '[.[] | select(.tok)] | length' <<<"$report")" -gt 0 ]]; then
  echo
  {
    printf 'version\tsessions\thours\trequests/h\tbus requests/h\tfixed tok/request\tskill loaded\tbus tok/h\tall tok/h\tbus share\n'
    jq -r '.[] | select(.tok) | .version as $v | .tok | [$v, .sessions, (.hours * 10 | round / 10),
                  (.req_per_h // "-"), (.bus_req_per_h // "-"), .fixed,
                  "\(.skill_loaded)/\(.sessions)", (.bus_per_h // "-"),
                  (.all_per_h // "-"), ((.bus_share_pct // "-") | tostring + "%")] | @tsv' <<<"$report"
  } | column -t -s $'\t'
fi
# Turns and asks (since 0.36.0), one row per version and harness. A turn runs
# from the prompt, or the wake-up, to the Stop that ends it; a wait on a permission
# prompt or a human is part of it. Only harnesses with turn hooks (Claude Code
# today) send turn data: the others show "-". A wait for an ask is the time to the
# first answer by a trusted human; the waiting share adds the age of asks still
# open at the end (asks to different humans at once count twice). Seconds as
# 45s, 12 min, 1:30 h.
FMT='def fmt(s): if s == null then "-" elif s < 60 then "\(s)s"
  elif (s / 60 | round) < 60 then "\(s / 60 | round) min"
  else (s / 60 | round) as $m
    | "\($m / 60 | floor):\($m % 60 | tostring | if length < 2 then "0" + . else . end) h" end;
  def dash(x): if x == null then "-" else x end;
  def pct(x): if x == null then "-" else "\(x)%" end;'
echo
{
  printf 'version\tharness\tsessions\tturns/prompt\tmean turn\tlongest turn\tbus time\twork/input\n'
  jq -r "$FMT"'.[] | .version as $v | .harness[] | .harness as $h | .turns as $t
    | if $t == null then [$v, $h, "-", "-", "-", "-", "-", "-"]
      else [$v, $h, $t.sessions, dash($t.turns_per_prompt), fmt($t.mean_sec), fmt($t.max_sec),
            pct($t.bus_share_pct), fmt($t.sec_per_input)] end | @tsv' <<<"$report"
} | column -t -s $'\t'
echo
{
  printf 'version\tharness\tsessions\tasks/h\tanswered\tmean wait\tlongest wait\topen at end\twaiting\n'
  jq -r "$FMT"'.[] | .version as $v | .harness[] | .harness as $h | .asks as $a
    | if $a == null then [$v, $h, "-", "-", "-", "-", "-", "-", "-"]
      else [$v, $h, $a.sessions, dash($a.asks_per_h), pct($a.answered_pct), fmt($a.mean_wait_sec),
            fmt(if $a.answered > 0 then $a.max_wait_sec else null end), $a.open,
            pct($a.waiting_pct)] end | @tsv' <<<"$report"
} | column -t -s $'\t'
# Saved waiting (since 0.91.0, harnesses with turn hooks): of the asks a trusted
# human answered on the bus, the share answered while the terminal was idle (no
# human prompt since the ask), and for those the time from the answer to the
# human's next prompt in the terminal: back (count, mean, longest), or still
# away when the session ended (count, their time so far: a lower bound).
if [[ "$(jq '[.[] | .harness[] | select(.saved)] | length' <<<"$report")" -gt 0 ]]; then
  echo
  {
    printf 'version\tharness\tsessions\tanswered on bus\tterminal idle\tback\tmean saved\tlongest saved\tstill away\taway at least\n'
    jq -r "$FMT"'.[] | .version as $v | .harness[] | select(.saved) | .harness as $h | .saved as $s
      | [$v, $h, $s.sessions, $s.answered, pct($s.idle_pct), $s.saved_n, fmt($s.mean_saved_sec),
         fmt(if $s.saved_n > 0 then $s.saved_max_sec else null end), $s.saved_open_n,
         fmt(if $s.saved_open_n > 0 then $s.saved_open_sec else null end)] | @tsv' <<<"$report"
  } | column -t -s $'\t'
fi
# Overhead by role (since 0.91.0): a session that held the crown more than half
# its time is the coordinator, else a worker; older sessions are unknown.
if [[ "$(jq '[.[] | .roles[] | select(.role != "unknown")] | length' <<<"$report")" -gt 0 ]]; then
  echo
  {
    printf 'version\trole\tsessions\thours\tbus tok share\tbus time\tturns/prompt\tasks/h\twaiting\n'
    jq -r "$FMT"'.[] | .version as $v | .roles[]
      | [$v, .role, .sessions, .hours, pct(.tok.bus_share_pct), pct(.turns.bus_share_pct),
         dash(.turns.turns_per_prompt), dash(.asks.asks_per_h), pct(.asks.waiting_pct)] | @tsv' <<<"$report"
  } | column -t -s $'\t'
fi

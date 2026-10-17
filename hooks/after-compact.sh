#!/usr/bin/env bash
# SessionStart hook (matcher "compact"): the conversation was just compacted.
#
# Compaction keeps a summary and drops the details. The agent's own sign-offs
# on the bus (IDLE/BYE handoffs, RT-6.2 step 2) say what is done, what is open,
# the branch or PR and the next step, so hand them back right away: the newest
# few own posts in the work channel, plus where its HELLO thread is.
# Spec: RT-6.7 in docs/arc42/06-runtime-view.md.
#
# Reads only the agent's own posts. Fails open: no bus, no token, no jq, an API
# error — exit 0 without output, and the session goes on as without the hook.
set -uo pipefail

cat >/dev/null 2>&1 || true   # drain the hook payload; we do not read it

# shellcheck source=../bin/mm-agent-lib.sh
source "$(cd "$(dirname "$0")/../bin" && pwd)/mm-agent-lib.sh"
mm_load_hook_session || true

command -v jq >/dev/null 2>&1 || exit 0
[[ -n "${MM_BOT_TOKEN:-${MATTERMOST_TOKEN:-}}" ]] || exit 0

if [[ -z "${MM_BOT_USER_ID:-}" ]]; then
  mm_api GET "/api/v4/users/me" && [[ "$MM_API_STATUS" == "200" ]] || exit 0
  MM_BOT_USER_ID="$(jq -r '.id' <<<"$MM_API_BODY")"
fi
cid="$(mm_work_channel_id 2>/dev/null)" || exit 0
mm_api GET "/api/v4/channels/${cid}/posts?per_page=200" && [[ "$MM_API_STATUS" == "200" ]] || exit 0

# Own posts, newest first: the last three, each cut to 600 characters.
context="$(jq -r --arg me "$MM_BOT_USER_ID" '
  [.order[] as $i | .posts[$i] | select(.user_id == $me and (.type // "") == "")] as $own
  | if ($own | length) == 0 then empty else
      ($own[:3] | map("- [\(.create_at / 1000 | floor | strflocaltime("%Y-%m-%d %H:%M"))] "
                      + (.message | gsub("\n"; " ") | .[:600])) | join("\n")) as $posts
      | ([$own[] | select((.root_id // "") == "" and (.message | startswith("HELLO")))][0].id // null) as $hello
      | "Your conversation was just compacted. Your own latest posts on the Mattermost agent bus "
        + "(work channel), newest first, are your handoff state:\n" + $posts
        + (if $hello then "\nYour HELLO thread: root \($hello); read it with get_thread for more." else "" end)
        + "\nBefore you continue, check the bus with wait_for_events(timeout_sec=1)."
    end' <<<"$MM_API_BODY" 2>/dev/null)" || exit 0
[[ -n "$context" ]] || exit 0

jq -nc --arg c "$context" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
exit 0

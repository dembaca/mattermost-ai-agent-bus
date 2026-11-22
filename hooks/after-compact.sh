#!/usr/bin/env bash
# SessionStart hook (matcher "compact"): the conversation was just compacted.
#
# Compaction keeps a summary and drops the details. The agent's own sign-offs
# on the bus (IDLE/BYE handoffs, RT-6.2 step 2) say what is done, what is open,
# the branch or PR and the next step, so hand them back right away, with the
# bus skill the compaction dropped. The MCP server words it, the same text the
# OpenCode plugin and the pi extension hand back (GET /v1/handoff): for Claude
# Code, whose additionalContext over 10,000 characters goes to a file, the line
# that reloads the skill instead of the skill itself.
# Spec: RT-6.7 in docs/arc42/06-runtime-view.md.
#
# Reads only the agent's own posts, through the MCP server (RT-6.17). Fails
# open: no server, no bus session, no jq, an API error — exit 0 without
# output, and the session goes on as without the hook.
set -uo pipefail

# shellcheck source=../bin/mm-agent-lib.sh
source "$(cd "$(dirname "$0")/../bin" && pwd)/mm-agent-lib.sh"
mm_read_hook_input            # drains the payload; only its session id is used

# The hook has 10 s (hooks.json); its two calls to the server share 8.
MM_BUS_DEADLINE=$(( $(date +%s) + 8 ))
export MM_BUS_DEADLINE
mm_bus_me || exit 0           # the bot's id, for the stats line
mm_bus GET '/v1/handoff?runtime=claude-code' || exit 0

context="$(jq -r '.context // empty | strings' <<<"$MM_BUS_BODY" 2>/dev/null)" || exit 0
[[ -n "$context" ]] || exit 0

mm_stat compact chars="${#context}"
jq -nc --arg c "$context" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $c}}'
exit 0

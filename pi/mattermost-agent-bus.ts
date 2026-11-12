// pi extension: the Mattermost agent bus in pi sessions (RT-6.27 in
// docs/arc42/06-runtime-view.md; install: adapters/pi.md; ADR-009 phase 3).
// Written against pi-coding-agent 1.3.0: its docs/extensions.md and the
// declarations of `ExtensionAPI` and its events (dist/core/extensions/types.d.ts).
//
// Thin glue, as the OpenCode plugin (RT-6.20): the session's MCP server decides
// and words everything; this file turns pi's events into calls to the server's
// hook API (RT-6.17) and the server's answers into session input:
//   session_start       the MCP server (pi.registerMcpServer) and the long poll
//   resources_discover  the skill
//   input               a human's prompt (any source but "extension") starts a
//                       turn: the counter, drain rounds and backoff afresh
//   agent_start         presence working (not awaited)
//   agent_settled       the final idle signal, not agent_end (which pi may
//                       follow with a retry, a compaction or queued input):
//                       GET /v1/turn-end → drain or sign-off as a user message
//                       that starts the next run, else presence "on the bus"
//                       (the server's gates hold) or idle; aborted (Escape):
//                       idle, no checks, no wake-up until the human's next
//                       prompt (RT-6.9)
//   turn_end, agent_settled
//                       the context use (ctx.getContextUsage) → POST /v1/usage
//                       for the status (ADR-008), not awaited
//   session_compact     GET /v1/handoff → context that starts no run (RT-6.7)
//   one long poll       GET /v1/wake?wait=50 (ADR-009) from session_start to
//                       session_shutdown: a wake → pi.sendUserMessage, at once
//                       when idle, deliverAs "followUp" while a human's run
//                       goes (after its tools, never in a tool call); none
//                       while a run this extension started goes, nor while
//                       pi compacts (it refuses a prompt then)
//   session_shutdown    ends the poll (idempotent)
// It holds no token and no secret, writes no file, and fails open: without a
// server or a bus session every call comes back empty and nothing happens. It
// finds the server as the OpenCode plugin does: the helpers between the
// keep-in-step markers are that plugin's (opencode/plugin/mattermost-agent-bus.ts),
// copied, because pi loads a symlinked extension from the link's directory,
// where no relative import of the checkout resolves; pi/test/ compares the two.

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent"
import { createHash } from "node:crypto"
import { existsSync, readFileSync, realpathSync } from "node:fs"
import { homedir } from "node:os"
import { basename, dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

export const SERVER = "mattermost-agent-bus"
export const SKILL = "mattermost-ai-agent-bus"
const QUICK_MS = 3000
// /v1/turn-end reads several channels (the Stop hook's 8 s).
const SLOW_MS = 8000
// The long poll of /v1/wake: the server holds it WAIT_SEC (at most 55).
export const WAIT_SEC = 50
// After a failed or empty-handed poll, and the base of the pause after "replaced".
export const RETRY_SEC = 15
// The same unread counter, stall or reaction again after the turn it woke:
// wait 1, 2, 4 … min, at most 30.
export const WAKE_BACKOFF_MS = 60_000
export const WAKE_BACKOFF_MAX_MS = 30 * 60_000
// The long poll's read timeout is a margin above its wait (RT-6.17 step 7).
const READ_MARGIN_MS = 10_000
const REPLACED_MAX_MS = 60_000
// Nothing to ask for (aborted, a run of ours): asked again when something
// changes, at the latest after this.
const IDLE_PAUSE_MS = 60_000
// A message of ours whose run never came (pi refused it, which it reports as an
// extension error, not to the caller): poll again after this.
const OURS_MS = 60_000
// A compaction whose end never came (another extension cancelled it): poll
// again after this.
const COMPACT_MAX_MS = 10 * 60_000
// pi's per-request timeout for the bus tools (s; its default is 60):
// wait_for_events waits 60 s by default and may be asked for more.
export const MCP_TIMEOUT_SEC = 180
// The context use is reported when it moved a point, else at most once a
// minute, as the status line helper does (RT-6.10 step 3).
export const USAGE_EVERY_MS = 60_000

// The tests' seams: the environment, where this file lies, the poll's timing.
export type Deps = { env?: Env; here?: string; waitSec?: number; retrySec?: number }

type Env = Record<string, string | undefined>
type Json = Record<string, any>

// keep-in-step: from here to the end marker, the same text in
// opencode/plugin/mattermost-agent-bus.ts and pi/mattermost-agent-bus.ts
// (pi/test/ compares them; RT-6.20 step 2, RT-6.27 step 2).
function sha16(text: string): string {
  return createHash("sha1").update(text).digest("hex").slice(0, 16)
}

function real(dir: string): string | null {
  try {
    return realpathSync(dir)
  } catch {
    return null
  }
}

// The endpoint file of the workspace's MCP server (an OpenCode location, a pi
// session's directory), whose MM_WORKSPACE names that real directory (RT-6.17
// step 2; the name as mm_bus_file in bin/mm-agent-lib.sh writes it). No session id, no other
// variable, no walk-up: inherited from a Claude Code shell, or a parent
// directory's file, they may name another session's server.
export function busFile(env: Env, directory: string, prefix = "endpoint"): string | null {
  const state = join(env.XDG_STATE_HOME || join(env.HOME || homedir(), ".local", "state"), "mm-agent-bus")
  const dir = real(directory)
  const f = dir && join(state, `${prefix}-${sha16(dir)}.env`)
  return f && existsSync(f) ? f : null
}

function parseEnvFile(text: string): Record<string, string> {
  const out: Record<string, string> = {}
  for (const line of text.split("\n")) {
    const i = line.indexOf("=")
    if (i > 0) out[line.slice(0, i)] = line.slice(i + 1)
  }
  return out
}

// One call to the hook API; null for no server, no bus session or any failure.
// missing: called when no endpoint file is there.
export function hookApi(env: Env, directory: string, missing: () => void = () => {}) {
  // signal: ends the call when it aborts (an unload, a shutdown), as the timeout does.
  return async (method: "GET" | "POST", path: string, body?: Json, ms = QUICK_MS, signal?: AbortSignal): Promise<Json | null> => {
    let endpoint = env.MM_BUS_ENDPOINT || ""
    let key = env.MM_BUS_KEY || ""
    if (!endpoint || !key) {
      const f = busFile(env, directory)
      if (!f) {
        missing()
        return null
      }
      try {
        const vars = parseEnvFile(readFileSync(f, "utf8"))
        endpoint = vars.MM_BUS_ENDPOINT || ""
        key = vars.MM_BUS_KEY || ""
      } catch {
        return null
      }
      if (!endpoint || !key) return null
    }
    try {
      const res = await fetch(endpoint.replace(/\/$/, "") + path, {
        method,
        headers: { Authorization: `Bearer ${key}`, ...(body ? { "Content-Type": "application/json" } : {}) },
        body: body ? JSON.stringify(body) : undefined,
        signal: signal ? AbortSignal.any([AbortSignal.timeout(ms), signal]) : AbortSignal.timeout(ms),
      })
      if (res.status !== 200) return null
      return (await res.json()) as Json
    } catch {
      return null
    }
  }
}
// keep-in-step: end

// The long poll's wait in seconds, 10 to 55.
export function waitSec(sec: number = WAIT_SEC): number {
  return Math.min(55, Math.max(10, Number.isFinite(sec) && sec > 0 ? Math.floor(sec) : WAIT_SEC))
}

// The least pause after a failed poll: a server that is gone must not be
// asked in a tight loop. `timing` is the tests' to lower.
export const timing = { minRetrySec: 5 }

export function retryMs(sec: number = RETRY_SEC): number {
  return Math.max(timing.minRetrySec, Number.isFinite(sec) && sec > 0 ? sec : RETRY_SEC) * 1000
}

export function wakeBackoffMs(repeats: number): number {
  return Math.min(WAKE_BACKOFF_MS * 2 ** repeats, WAKE_BACKOFF_MAX_MS)
}

// The bus checkout: MM_AGENT_BUS_ROOT, or the checkout this file really lies
// in (pi/; a symlink into ~/.pi/agent/extensions/ resolves).
export function busRoot(env: Env, here: string): string | null {
  for (const dir of [env.MM_AGENT_BUS_ROOT, dirname(dirname(real(here) || here))]) {
    if (dir && existsSync(join(dir, "mcp", "pyproject.toml"))) return dir
  }
  return null
}

const shq = (s: string) => `'${s.replace(/'/g, `'\\''`)}'`

// The registration secret for the server alone: pi runs an env value that
// starts with "!" as a command for that server's environment only, so the
// secret never enters pi's environment or the agent's shell. From
// MM_REG_SECRET_GOPASS (the gopass path, as the corral hooks read it), else
// MM_REG_SECRET_CMD (any command that prints it). Only the command goes into
// the config, never the secret. None under corral (MM_BUS_MCP_ENDPOINT): the
// host hooks registered the bot, the server is a relay (RT-6.18).
export function secretCommand(env: Env): string | null {
  if (env.MM_BUS_MCP_ENDPOINT) return null
  const path = env.MM_REG_SECRET_GOPASS?.trim()
  if (path) return `!gopass show -o ${shq(path)}`
  const cmd = env.MM_REG_SECRET_CMD?.trim()
  return cmd ? `!${cmd}` : null
}

// The MCP server as pi.registerMcpServer takes it (an mcp.json entry). Its
// environment is pi's plus these: a Claude Code session's id and project,
// inherited when pi was started from its shell, would key the server's files
// on that session: blanked, and the workspace named, where the extension
// looks (RT-6.27 step 1, as RT-6.20 step 2). No channel push: Claude Code's.
// Its tools are declared to the model ("direct"), not left to codemode.
export function mcpConfig(root: string, workspace: string, env: Env = {}) {
  const secret = secretCommand(env)
  return {
    command: "uv",
    args: ["run", "--locked", "--project", join(root, "mcp"), "mattermost-ai-agent-bus-mcp"],
    env: { CLAUDE_CODE_SESSION_ID: "", CLAUDE_PROJECT_DIR: "", MM_CHANNEL_PUSH: "0", MM_WORKSPACE: workspace,
           ...(secret ? { MM_REG_SECRET: secret } : {}) },
    cwd: workspace,
    exposure: "direct" as const,
    timeout: MCP_TIMEOUT_SEC,
    description: "Mattermost agent bus: post, reply, wait for events, ask a human (skill mattermost-ai-agent-bus)",
  }
}

type Turn = {
  running: boolean // a run goes (agent_start until agent_settled)
  working: boolean // the presence the extension set last is "working"
  drains: number
  signoffAsked: boolean
  byBus: boolean
  aborted: boolean // until the human's next prompt: no checks, no wake-up
  listening: boolean // the server's gates held at the last turn end
  ours: number // epoch ms: a message of ours is out, its run not settled (0: none)
  idleAt: number // epoch ms: the last turn end, the long poll's `since`
  human: number // the human's prompts so far: a check they overtook is dropped
  wakeKey: string // the last wake-up's counter (reason:count), "" for new posts
  wakeRepeats: number
  wakeAfter: number // epoch ms: no wake-up for the same counter before
  usageCtx: number // the context % reported last (-1: none)
  usageAt: number
  compacting: number // epoch ms: pi compacts since (0: not)
}

export function create(pi: ExtensionAPI, deps: Deps = {}) {
  const env = deps.env ?? (process.env as Env)
  const root = busRoot(env, deps.here ?? fileURLToPath(import.meta.url))
  const WAIT = waitSec(deps.waitSec)
  const RETRY = retryMs(deps.retrySec)
  const t: Turn = { running: false, working: false, drains: 0, signoffAsked: false, byBus: false, aborted: false,
    listening: false, ours: 0, idleAt: Date.now(), human: 0, wakeKey: "", wakeRepeats: 0, wakeAfter: 0,
    usageCtx: -1, usageAt: 0, compacting: 0 }
  let ctx: ExtensionContext | null = null
  let bus: ReturnType<typeof hookApi> = async () => null
  let label = ""
  let ac: AbortController | null = null
  let closed = true

  const stat = (k: string, fields: Json = {}) => bus("POST", "/v1/stat", { k, ...fields })
  const presence = (state: "working" | "idle" | "bus") => bus("POST", "/v1/presence", { state, label })
  const notify = (text: string) => {
    try {
      if (text && ctx?.hasUI) ctx.ui.notify(text, "info")
    } catch {
      // no UI: nothing to show
    }
  }

  // A user message of ours: at once when idle, else after the run's tools
  // (followUp), never into a tool call. pi reports a refusal as an extension
  // error, not here: OURS_MS bounds the wait for its run.
  const deliver = (text: string) => {
    t.ours = Date.now()
    if (!ctx || ctx.isIdle()) pi.sendUserMessage(text)
    else pi.sendUserMessage(text, { deliverAs: "followUp" })
  }
  const compacting = () => t.compacting > 0 && Date.now() - t.compacting < COMPACT_MAX_MS

  // A pause the loop ends early when something changes (nudge).
  let poke: (() => void) | null = null
  const pause = (ms: number) =>
    new Promise<void>((resolve) => {
      const done = () => {
        clearTimeout(timer)
        if (poke === done) poke = null
        resolve()
      }
      const timer = setTimeout(done, ms)
      ;(timer as any).unref?.()
      poke = done
    })
  const nudge = () => poke?.()

  // ADR-008: the context use for the status, when it moved or once a minute.
  const usage = async () => {
    let u: any
    try {
      u = ctx?.getContextUsage()
    } catch {
      return
    }
    const pct = Number(u?.percent)
    if (u?.percent == null || !Number.isFinite(pct)) return
    const ctxPct = Math.min(100, Math.max(0, Math.round(pct)))
    if (Math.abs(ctxPct - t.usageCtx) < 1 && Date.now() - t.usageAt < USAGE_EVERY_MS) return
    t.usageCtx = ctxPct
    t.usageAt = Date.now()
    const size = Number(u.contextWindow)
    await bus("POST", "/v1/usage", { ctx: ctxPct, ...(Number.isInteger(size) && size > 0 ? { ctx_size: size } : {}) })
  }

  // The Stop hook's checks, asked of the server (RT-6.27 step 4; RT-6.2 steps 1–2).
  const turnEnded = async (aborted: boolean) => {
    t.running = false
    t.ours = 0
    if (aborted || t.aborted) {
      // The human stopped the run: no checks, no turn end (RT-6.9), no wake-up
      // until their next prompt.
      t.aborted = true
      if (t.working) {
        t.working = false
        await presence("idle")
      }
      await usage()
      return
    }
    t.idleAt = Date.now()
    const human = t.human
    const q = `drains=${t.drains}&signoff_asked=${+t.signoffAsked}&by_bus=${+t.byBus}&push=1`
    const r = await bus("GET", `/v1/turn-end?${q}`, undefined, SLOW_MS)
    // The human typed meanwhile, or a wake-up went out: that run ends with its own check.
    if (t.human !== human || t.running || t.ours) return
    if (r && (r.action === "drain" || r.action === "signoff")) {
      if (r.action === "drain") t.drains += 1
      else t.signoffAsked = true
      await stat("block", { kind: r.action })
      notify(String(r.banner || ""))
      deliver(String(r.reason))
      return
    }
    if (r) await stat("turn_end")
    // A window the server opened (RT-6.2 step 3) says its gates hold: reachable
    // between turns, for as long as the session lives (ADR-009). A failed check
    // changes nothing about that.
    if (r) t.listening = (Number(r.listen_until) || 0) * 1000 > Date.now()
    t.working = false
    await (t.listening ? presence("bus") : presence("idle"))
    await usage()
    nudge()
  }

  // The listener (RT-6.27 step 5, ADR-009): one long poll of /v1/wake from
  // session_start to session_shutdown, renewed when it ends. The server
  // decides, by its gates; it only looks.
  const listen = async (signal: AbortSignal) => {
    let replaced = 0
    while (!closed && !signal.aborted) {
      // Aborted by the human, a message of ours whose run has not settled, or
      // a compaction (pi refuses a prompt meanwhile).
      if (t.aborted || compacting() || (t.ours && Date.now() - t.ours < OURS_MS)) {
        await pause(t.aborted ? IDLE_PAUSE_MS : OURS_MS)
        continue
      }
      const asked = Date.now()
      const since = t.idleAt
      // `until` (a day ahead) is for a server before 0.71.8, as the OpenCode plugin sends it.
      const until = Math.floor(asked / 1000) + 86400
      const r = await bus("GET", `/v1/wake?since=${since}&until=${until}&wait=${WAIT}`, undefined,
                          WAIT * 1000 + READ_MARGIN_MS, signal)
      if (closed || signal.aborted) break
      if (!r) {
        await pause(RETRY) // no server, 503 (not registered yet), a failure
        continue
      }
      if (r.reason === "replaced") {
        // Another poller of the session took over: back off, or two replace each other.
        replaced += 1
        await pause(Math.min(RETRY * 2 ** (replaced - 1), REPLACED_MAX_MS))
        continue
      }
      replaced = 0
      if (!r.wake) {
        t.wakeKey = ""
        // An empty answer long before the wait ran out: a server that does not hold it.
        if (Date.now() - asked < (WAIT - 5) * 1000) await pause(RETRY)
        continue
      }
      // A poll that was out when one of these began: its answer is dropped.
      if (t.aborted || t.ours || compacting()) continue
      // New posts are counted since the last turn end; the counters, a stall
      // and reactions are not: the same one again backs off.
      const key = r.reason === "addressed" || r.reason === "digest" ? "" : `${r.reason}:${r.count}`
      const now = Date.now()
      if (key && key === t.wakeKey) {
        if (now < t.wakeAfter) {
          await pause(RETRY)
          continue
        }
        t.wakeRepeats += 1
      } else {
        t.wakeRepeats = 0
      }
      t.wakeKey = key
      t.wakeAfter = now + wakeBackoffMs(t.wakeRepeats)
      const idle = !t.running
      await stat("wake", { reason: String(r.reason || "") })
      deliver(String(r.text))
      if (idle) {
        // The bus's turn keeps the drain rounds: only the human's next prompt
        // starts them afresh.
        t.byBus = true
        await stat("turn", { by: "bus" })
      }
    }
  }

  pi.on("session_start", async (_event, c) => {
    ctx = c
    const dir = real(c.cwd) || c.cwd
    label = basename(dir).slice(0, 64) // the presence label's limit (RT-6.17)
    bus = hookApi(env, dir)
    if (root) {
      try {
        pi.registerMcpServer(SERVER, mcpConfig(root, dir, env))
      } catch (e) {
        notify(`${SERVER}: MCP server not registered: ${e instanceof Error ? e.message : String(e)}`)
      }
    }
    ac?.abort()
    ac = new AbortController()
    closed = false
    void listen(ac.signal)
  })

  pi.on("resources_discover", () => (root ? { skillPaths: [join(root, "skills", SKILL)] } : undefined))

  // A human's prompt (RT-6.27 step 3): pi's own input from an extension, ours
  // included, is not one.
  pi.on("input", async (event, c) => {
    ctx = c
    if (event.source === "extension") return
    t.human += 1
    Object.assign(t, { aborted: false, drains: 0, signoffAsked: false, byBus: false, wakeKey: "", wakeRepeats: 0,
                       wakeAfter: 0 })
    await stat("turn", { by: "human" })
    nudge()
  })

  // Not awaited, here and at turn_end: pi waits for its handlers, and a run
  // must not wait for the bus.
  pi.on("agent_start", (_event, c) => {
    ctx = c
    t.running = true
    if (!t.working) {
      t.working = true
      void presence("working").catch(() => {})
    }
  })

  pi.on("turn_end", (_event, c) => {
    ctx = c
    void usage().catch(() => {})
  })

  // Not awaited: pi waits for its settle handlers, and the human must not.
  pi.on("agent_settled", (event, c) => {
    ctx = c
    void turnEnded(Boolean(event.aborted)).catch(() => {})
  })

  // A compaction: no wake-up meanwhile (RT-6.27 step 5).
  pi.on("session_before_compact", (_event, c) => {
    ctx = c
    t.compacting = Date.now()
  })
  pi.on("session_compact_failed", (_event, c) => {
    ctx = c
    t.compacting = 0
    nudge()
  })

  // After a compaction: the own latest posts as context that starts no run (RT-6.7).
  pi.on("session_compact", async (_event, c) => {
    ctx = c
    t.compacting = 0
    nudge()
    const r = await bus("GET", "/v1/handoff")
    const text = String(r?.context || "")
    if (!text) return
    await stat("compact", { chars: text.length })
    try {
      pi.sendMessage({ customType: SERVER, content: text, display: true }, { triggerTurn: false })
    } catch {
      // the next turn goes without it
    }
  })

  pi.on("session_shutdown", () => {
    closed = true
    ac?.abort() // ends a long poll in flight
    nudge()
  })

  return { state: t }
}

export default function mattermostAgentBus(pi: ExtensionAPI) {
  create(pi)
}

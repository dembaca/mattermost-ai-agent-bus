// OpenCode plugin: the Mattermost agent bus in OpenCode sessions (RT-6.20 in
// docs/arc42/06-runtime-view.md; install: adapters/opencode.md). Plugin API V1
// (OpenCode 1.x).
//
// Thin glue: the session's MCP server decides and words everything; this file
// turns OpenCode's events into calls to the server's hook API (RT-6.17) and
// the server's answers into prompts:
//   config            the MCP server (unless configured) and the skill path
//   chat.message      a turn starts: presence working (RT-6.2, RT-6.3)
//   session.idle      a turn ends: GET /v1/turn-end → drain or sign-off as a new
//                     prompt, else presence listening for the listen window the
//                     server opens, or idle (the Stop hook, RT-6.2)
//   a timer           while no top-level turn runs, inside that window: GET
//                     /v1/wake → a prompt that tells the agent to collect with
//                     wait_for_events (the listener, RT-6.2 step 4); the same
//                     counter again after the turn it woke backs off
//   session.compacted GET /v1/handoff → the own latest posts as context, no
//                     reply (RT-6.7)
// It holds no token and no secret, writes no file, and fails open: without a
// server or a bus session every call comes back empty and nothing happens.
// Only top-level sessions count: a subagent's session (parentID) is left alone.

import type { Plugin } from "@opencode-ai/plugin"
import { createHash } from "node:crypto"
import { existsSync, readFileSync, realpathSync } from "node:fs"
import { homedir } from "node:os"
import { basename, dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

export const SERVER = "mattermost-agent-bus"
// Marks the prompts this plugin sends, so chat.message tells them from the human's.
export const MARK = "mattermost-agent-bus"
const QUICK_MS = 3000
// /v1/turn-end and /v1/wake read several channels (the Stop hook's 8 s).
const SLOW_MS = 8000
const WAKE_RETRY_MS = 60_000
// The same unread counter, stall or reaction again after the turn it woke: a
// counter wait_for_events cannot clear. Wait 1, 2, 4 … min, at most 30.
export const WAKE_BACKOFF_MS = 60_000
export const WAKE_BACKOFF_MAX_MS = 30 * 60_000
const MIN_POLL_SEC = 5

type Env = Record<string, string | undefined>
type Json = Record<string, any>
type Kind = "drain" | "signoff" | "wake" | "context"

export type Options = {
  // The bus checkout (with mcp/ and skills/); default: where this file really lies.
  root?: string
  // false: add no MCP server to the config (the user configures one).
  mcp?: boolean
  // The MCP server's command instead of `uv run … mattermost-ai-agent-bus-mcp`,
  // e.g. one that fetches MM_REG_SECRET itself (adapters/opencode.md).
  command?: string[]
  environment?: Record<string, string>
  // How often an idle session asks whether the bus has something (s).
  pollSec?: number
}

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

// The endpoint file of this OpenCode instance's MCP server, which the config
// hook keys on MM_WORKSPACE = OpenCode's real directory (RT-6.20 step 2;
// the name as mm_bus_file in bin/mm-agent-lib.sh writes it). No session id, no
// other variable, no walk-up: inherited from a Claude Code shell, or a parent
// directory's file, they may name another session's server.
export function busFile(env: Env, directory: string, prefix = "endpoint"): string | null {
  const state = join(env.XDG_STATE_HOME || join(env.HOME || homedir(), ".local", "state"), "mm-agent-bus")
  const dir = real(directory)
  const f = dir && join(state, `${prefix}-${sha16(dir)}.env`)
  return f && existsSync(f) ? f : null
}

// How often an idle session asks: the option, MM_LISTEN_POLL_SEC or 15 s, at least 5 s.
export function pollMs(options: Options, env: Env): number {
  const sec = Number(options.pollSec ?? env.MM_LISTEN_POLL_SEC ?? 15)
  return Math.max(MIN_POLL_SEC, Number.isFinite(sec) && sec > 0 ? sec : 15) * 1000
}

export function wakeBackoffMs(repeats: number): number {
  return Math.min(WAKE_BACKOFF_MS * 2 ** repeats, WAKE_BACKOFF_MAX_MS)
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
  return async (method: "GET" | "POST", path: string, body?: Json, ms = QUICK_MS): Promise<Json | null> => {
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
        signal: AbortSignal.timeout(ms),
      })
      if (res.status !== 200) return null
      return (await res.json()) as Json
    } catch {
      return null
    }
  }
}

// The bus checkout: the option, MM_AGENT_BUS_ROOT, or the checkout this file
// really lies in (opencode/plugin/) (a symlink into ~/.config/opencode/plugins/ resolves).
export function busRoot(options: Options, env: Env, here: string): string | null {
  for (const dir of [options.root, env.MM_AGENT_BUS_ROOT, dirname(dirname(dirname(real(here) || here)))]) {
    if (dir && existsSync(join(dir, "mcp", "pyproject.toml"))) return dir
  }
  return null
}

// The config hook: the MCP server and the skill, unless the user has them.
// true: the user configured the server without MM_WORKSPACE, so its endpoint
// file may lie where the plugin does not look.
export function addToConfig(cfg: Json, options: Options, root: string | null, directory: string): boolean {
  const mine = (entry: any) =>
    Array.isArray(entry?.command) && entry.command.some((a: unknown) => String(a).includes("mattermost-ai-agent-bus-mcp"))
  cfg.mcp ??= {}
  const own = cfg.mcp[SERVER] ?? Object.values(cfg.mcp).find(mine)
  if (options.mcp !== false && !own) {
    const command =
      options.command ??
      (root ? ["uv", "run", "--locked", "--project", join(root, "mcp"), "mattermost-ai-agent-bus-mcp"] : null)
    // The secret is never written here: the server inherits OpenCode's
    // environment, or the user's command fetches it (adapters/opencode.md).
    // A Claude Code session's id and project, inherited when OpenCode was
    // started from its shell, would key the server's files on that session:
    // blanked, and the workspace named, where busFile looks (RT-6.20 step 2).
    const environment = { CLAUDE_CODE_SESSION_ID: "", CLAUDE_PROJECT_DIR: "", MM_WORKSPACE: real(directory) || directory,
                          ...(options.environment ?? {}) }
    if (command) cfg.mcp[SERVER] = { type: "local", command, environment }
  }
  const skills = root ? join(root, "skills") : null
  if (skills && existsSync(skills)) {
    cfg.skills ??= {}
    cfg.skills.paths ??= []
    if (!cfg.skills.paths.includes(skills)) cfg.skills.paths.push(skills)
  }
  return Boolean(own && !own.environment?.MM_WORKSPACE)
}

type Turn = {
  busy: boolean
  idleAt: number
  drains: number
  signoffAsked: boolean
  byBus: boolean
  aborted: boolean // until the next message: idles change nothing, no wake-up
  inFlight: boolean
  listenSince: number // the listen window, epoch s (0: none)
  listenUntil: number
  wakeKey: string // the last wake-up's counter (reason:count), "" for new posts
  wakeRepeats: number
  wakeAfter: number // epoch ms: no wake-up for the same counter before
}

export const MattermostAgentBus: Plugin = async (input, rawOptions) => {
  const options = (rawOptions ?? {}) as Options
  const { client, directory } = input
  const env = process.env as Env
  let unkeyed = false // a hand-configured server without MM_WORKSPACE
  let warned = false
  const bus = hookApi(env, directory, () => {
    if (!unkeyed || warned) return
    warned = true
    void toast(`Mattermost agent bus: no MCP server found for ${directory}; set MM_WORKSPACE in its environment (adapters/opencode.md)`, "warning")
  })
  const label = basename(directory).slice(0, 64) // the presence label's limit (RT-6.17)
  const turns = new Map<string, Turn>() // top-level sessions only
  const parents = new Map<string, boolean>() // session id → top-level
  let last = "" // the top-level session that had a message or went idle last: the one a wake starts
  let wakeFailedAt = 0
  let ticking = false

  const turn = (id: string): Turn => {
    let t = turns.get(id)
    if (!t) {
      t = { busy: false, idleAt: Date.now(), drains: 0, signoffAsked: false, byBus: false, aborted: false, inFlight: false,
            listenSince: 0, listenUntil: 0, wakeKey: "", wakeRepeats: 0, wakeAfter: 0 }
      turns.set(id, t)
    }
    return t
  }
  const anyBusy = () => [...turns.values()].some((t) => t.busy || t.inFlight)

  const topLevel = async (id: string): Promise<boolean> => {
    if (parents.has(id)) return parents.get(id)!
    try {
      const res: any = await client.session.get({ path: { id } })
      const info = res?.data ?? res
      if (!info?.id) return false
      parents.set(id, !info.parentID)
      return !info.parentID
    } catch {
      return false
    }
  }

  const stat = (k: string, fields: Json = {}) => bus("POST", "/v1/stat", { k, ...fields })
  const presence = (state: "working" | "idle") => bus("POST", "/v1/presence", { state, label })
  const listening = (until: number) => bus("POST", "/v1/presence", { state: "listening", until })
  async function toast(message: string, variant = "info") {
    try {
      await (client as any).tui?.showToast?.({ body: { message, variant } })
    } catch {
      // no TUI (serve, run)
    }
  }

  const prompt = async (id: string, text: string, kind: Kind): Promise<boolean> => {
    const parts = [{ type: "text" as const, text, metadata: { [MARK]: kind } }]
    const s = client.session as any
    try {
      // The SDK answers {data, error} rather than throwing.
      let res: any
      if (kind === "context") {
        res = await s.prompt({ path: { id }, body: { noReply: true, parts } })
      } else if (typeof s.promptAsync === "function") {
        res = await s.promptAsync({ path: { id }, body: { parts } })
      } else {
        void s.prompt({ path: { id }, body: { parts } }).catch(() => {})
      }
      return !res?.error
    } catch {
      return false
    }
  }

  // The Stop hook's checks, asked of the server (RT-6.20; RT-6.2 steps 1–2).
  const turnEnded = async (id: string) => {
    if (!(await topLevel(id))) return
    const t = turn(id)
    if (t.inFlight) return
    if (t.aborted) {
      // The human interrupted: no checks, no turn end (RT-6.9), no wake-up
      // until their next message. OpenCode reports such an idle twice.
      if (t.busy) {
        t.busy = false
        await presence("idle")
      }
      return
    }
    t.busy = false
    t.idleAt = Date.now()
    last = id
    t.inFlight = true
    try {
      const q = `drains=${t.drains}&signoff_asked=${+t.signoffAsked}&by_bus=${+t.byBus}&push=1&listen_since=${t.listenSince}`
      const r = await bus("GET", `/v1/turn-end?${q}`, undefined, SLOW_MS)
      if (r && (r.action === "drain" || r.action === "signoff")) {
        if (r.action === "drain") t.drains += 1
        else t.signoffAsked = true
        t.busy = true
        await stat("block", { kind: r.action })
        await toast(String(r.banner || ""))
        if (await prompt(id, String(r.reason), r.action)) return
        t.busy = false
      }
      if (r) await stat("turn_end")
      // The listen window the server opened (RT-6.2 step 3): wake-ups only inside it.
      const until = Number(r?.listen_until) || 0
      if (until * 1000 > Date.now()) {
        t.listenSince = Number(r!.listen_since) || 0
        t.listenUntil = until
        await listening(until)
      } else {
        t.listenUntil = 0
        await presence("idle")
      }
    } finally {
      t.inFlight = false
    }
  }

  // The listener's checks while the session is idle (RT-6.20; RT-6.2 step 4).
  const tick = async () => {
    // A top-level turn at work: its human is there, wake no other session now.
    if (ticking || !last || Date.now() - wakeFailedAt < WAKE_RETRY_MS || anyBusy()) return
    const t = turns.get(last)
    if (!t || t.aborted || Date.now() >= t.listenUntil * 1000) return
    ticking = true
    const id = last
    try {
      const r = await bus("GET", `/v1/wake?since=${t.idleAt}&until=${t.listenUntil}`, undefined, SLOW_MS)
      if (!r) return
      if (!r.wake) {
        t.wakeKey = ""
        return
      }
      if (anyBusy() || t.aborted || last !== id) return
      // New posts are counted since the session went idle; the counters,
      // a stall and reactions are not: the same one again backs off.
      const key = r.reason === "addressed" || r.reason === "digest" ? "" : `${r.reason}:${r.count}`
      const now = Date.now()
      if (key && key === t.wakeKey) {
        if (now < t.wakeAfter) return
        t.wakeRepeats += 1
      } else {
        t.wakeRepeats = 0
      }
      t.wakeKey = key
      t.wakeAfter = now + wakeBackoffMs(t.wakeRepeats)
      t.busy = true
      await stat("wake", { reason: String(r.reason || "") })
      if (!(await prompt(id, String(r.text), "wake"))) {
        t.busy = false
        wakeFailedAt = Date.now()
      }
    } finally {
      ticking = false
    }
  }
  const timer = setInterval(() => void tick(), pollMs(options, env))
  ;(timer as any).unref?.()

  const here = fileURLToPath(import.meta.url)
  const root = busRoot(options, env, here)

  return {
    config: async (cfg) => {
      unkeyed = addToConfig(cfg as Json, options, root, directory)
    },

    "chat.message": async (inp, out) => {
      const marked = (out.parts as any[]).find((p) => p?.type === "text" && p?.metadata?.[MARK])
      const kind: Kind | undefined = marked?.metadata?.[MARK]
      if (kind === "context") return // no turn follows
      void (async () => {
        if (!(await topLevel(inp.sessionID))) return
        const t = turn(inp.sessionID)
        t.busy = true
        t.aborted = false
        last = inp.sessionID
        if (kind === "drain" || kind === "signoff") {
          // A round inside the same turn, as a blocked stop is.
        } else if (kind === "wake") {
          // The bus's turn keeps the drain rounds and the listen window: only
          // the human's next message starts them afresh.
          t.byBus = true
          await stat("turn", { by: "bus" })
        } else {
          Object.assign(t, { drains: 0, signoffAsked: false, byBus: false, listenSince: 0, listenUntil: 0,
                             wakeKey: "", wakeRepeats: 0, wakeAfter: 0 })
          await stat("turn", { by: "human" })
        }
        await presence("working")
      })()
    },

    event: async ({ event }) => {
      const e = event as any
      const p = e.properties ?? {}
      switch (e.type) {
        case "session.created":
        case "session.updated":
          if (p.info?.id) parents.set(p.info.id, !p.info.parentID)
          break
        case "session.deleted":
          if (p.info?.id) {
            turns.delete(p.info.id)
            if (last === p.info.id) last = ""
          }
          break
        case "session.status":
          if (p.status?.type === "busy" && turns.has(p.sessionID)) turn(p.sessionID).busy = true
          break
        case "session.error":
          if (turns.has(p.sessionID) && p.error?.name === "MessageAbortedError") turn(p.sessionID).aborted = true
          break
        case "session.idle":
          await turnEnded(p.sessionID)
          break
        case "session.compacted": {
          if (!(await topLevel(p.sessionID))) break
          const r = await bus("GET", "/v1/handoff")
          const text = String(r?.context || "")
          if (text) {
            await stat("compact", { chars: text.length })
            await prompt(p.sessionID, text, "context")
          }
          break
        }
      }
    },

    dispose: async () => {
      clearInterval(timer)
    },
  }
}

export default { id: SERVER, server: MattermostAgentBus }

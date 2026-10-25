// OpenCode plugin: the Mattermost agent bus in OpenCode sessions (RT-6.20 in
// docs/arc42/06-runtime-view.md; install: adapters/opencode.md). Plugin API V2
// (OpenCode 2.x); OpenCode 1.x (plugin API V1) is not supported.
//
// Thin glue: the session's MCP server decides and words everything; this file
// turns OpenCode's hooks and events into calls to the server's hook API
// (RT-6.17) and the server's answers into session input:
//   mcp.transform      the MCP server (unless configured)
//   skill.transform    the skill
//   hook "prompt"      a human's prompt starts a turn: presence working (RT-6.2, RT-6.3)
//   session.execution.succeeded / .failed
//                      a turn ends: GET /v1/turn-end → drain or sign-off as
//                      synthetic input that starts the next round, else
//                      presence listening for the listen window the server
//                      opens, or idle (the Stop hook, RT-6.2)
//   session.execution.interrupted
//                      the human stopped the turn: presence idle, no checks (RT-6.9)
//   a timer            while no top-level turn runs, inside that window: GET
//                      /v1/wake → synthetic input that tells the agent to collect
//                      with wait_for_events (the listener, RT-6.2 step 4); the
//                      same counter again after the turn it woke backs off
//   session.compaction.ended
//                      GET /v1/handoff → the own latest posts as synthetic
//                      context that starts no turn (RT-6.7); a compaction
//                      between turns (/compact) runs as an execution of its
//                      own, whose end is no turn end
// It holds no token and no secret, writes no file, and fails open: without a
// server or a bus session every call comes back empty and nothing happens.
// Only top-level sessions of its own location count: a subagent's session
// (parentID) is left alone. OpenCode 2 runs one plugin instance per location
// in its long-running service and unloads a location after an hour without
// use: the turn state is kept in the plugin's storage, so a reload goes on.

import type { Plugin } from "@opencode/plugin"
import { createHash } from "node:crypto"
import { existsSync, readFileSync, realpathSync } from "node:fs"
import { homedir } from "node:os"
import { basename, dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

export const SERVER = "mattermost-agent-bus"
export const SKILL = "mattermost-ai-agent-bus"
// Marks the input this plugin sends, so the prompt hook tells it from the human's.
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
const RESUBSCRIBE_MS = 1000
const KEEP_MS = 24 * 3600_000 // turn state older than a day is not restored

type Env = Record<string, string | undefined>
type Json = Record<string, any>
type Kind = "drain" | "signoff" | "wake" | "context"

export type Options = {
  // The bus checkout (with mcp/ and skills/); default: where this file really lies.
  root?: string
  // false: add no MCP server (the user configures one).
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

// The endpoint file of this location's MCP server, which mcp.transform keys
// on MM_WORKSPACE = the location's real directory (RT-6.20 step 2; the name
// as mm_bus_file in bin/mm-agent-lib.sh writes it). No session id, no other
// variable, no walk-up: inherited from a Claude Code shell, or a parent
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
// really lies in (opencode/plugin/) (a symlink into .opencode/plugins/ resolves).
export function busRoot(options: Options, env: Env, here: string): string | null {
  for (const dir of [options.root, env.MM_AGENT_BUS_ROOT, dirname(dirname(dirname(real(here) || here)))]) {
    if (dir && existsSync(join(dir, "mcp", "pyproject.toml"))) return dir
  }
  return null
}

// The MCP transform: the server, unless the user has it (by name, or another
// local entry that runs mattermost-ai-agent-bus-mcp). true: the user's server
// has no MM_WORKSPACE, so its endpoint file may lie where the plugin does not look.
export function addMcp(editor: any, options: Options, root: string | null, directory: string): boolean {
  const runs = (cfg: any) =>
    Array.isArray(cfg?.command) && cfg.command.some((a: unknown) => String(a).includes("mattermost-ai-agent-bus-mcp"))
  const own = editor.get(SERVER) ?? editor.list().find(([, cfg]: [string, any]) => runs(cfg))?.[1]
  if (options.mcp !== false && !own) {
    const command =
      options.command ??
      (root ? ["uv", "run", "--locked", "--project", join(root, "mcp"), "mattermost-ai-agent-bus-mcp"] : null)
    // The secret is never written here: the server inherits the OpenCode
    // service's environment, or the user's command fetches it
    // (adapters/opencode.md). A Claude Code session's id and project,
    // inherited when the service was started from its shell, would key the
    // server's files on that session: blanked, and the workspace named, where
    // busFile looks (RT-6.20 step 2). No channel push: that is Claude Code's.
    const dir = real(directory) || directory
    const environment = { CLAUDE_CODE_SESSION_ID: "", CLAUDE_PROJECT_DIR: "", MM_CHANNEL_PUSH: "0", MM_WORKSPACE: dir,
                          ...(options.environment ?? {}) }
    if (command) editor.set(SERVER, { type: "local", command, environment, cwd: dir })
  }
  return Boolean(own && !own.environment?.MM_WORKSPACE)
}

// The skill as OpenCode reads a SKILL.md: name and description from the
// front matter, the rest as content; path next to coordinator.md and
// self-registration.md, which the skill names.
export function skillInfo(root: string | null): Json | null {
  const path = root ? join(root, "skills", SKILL, "SKILL.md") : ""
  let text: string
  try {
    text = readFileSync(path, "utf8")
  } catch {
    return null
  }
  const m = text.match(/^---\r?\n([\s\S]*?)\r?\n---\r?\n?/)
  const front = m ? m[1].split(/\r?\n/) : []
  const field = (name: string): string => {
    const i = front.findIndex((l) => l.startsWith(`${name}:`))
    if (i < 0) return ""
    const value = front[i].slice(name.length + 1).trim()
    if (!/^[>|][-+]?$/.test(value)) return value.replace(/^["']|["']$/g, "")
    const block: string[] = []
    for (const l of front.slice(i + 1)) {
      if (!/^\s+\S/.test(l)) break
      block.push(l.trim())
    }
    return block.join(" ")
  }
  const description = field("description")
  return { id: SKILL, name: field("name") || SKILL, ...(description ? { description } : {}), path,
           content: m ? text.slice(m[0].length) : text }
}

type Turn = {
  busy: boolean
  idleAt: number
  drains: number
  signoffAsked: boolean
  byBus: boolean
  aborted: boolean // until the human's next prompt: no checks, no wake-up
  inFlight: boolean
  listenSince: number // the listen window, epoch s (0: none)
  listenUntil: number
  wakeKey: string // the last wake-up's counter (reason:count), "" for new posts
  wakeRepeats: number
  wakeAfter: number // epoch ms: no wake-up for the same counter before
  sent: string // the text of the input the plugin sent last
  turn: boolean // a turn started by a prompt or the plugin's input runs
  compactOnly: boolean // the running execution only compacts (/compact between turns)
}

const fresh = (): Turn => ({ busy: false, idleAt: Date.now(), drains: 0, signoffAsked: false, byBus: false, aborted: false,
  inFlight: false, listenSince: 0, listenUntil: 0, wakeKey: "", wakeRepeats: 0, wakeAfter: 0, sent: "", turn: false, compactOnly: false })

export async function setup(ctx: Plugin.Context) {
  const options = (ctx.options ?? {}) as Options
  const directory = String(ctx.location.directory)
  const realDir = real(directory) || directory
  const env = process.env as Env
  const storeKey = `${SERVER}/${sha16(realDir)}`
  let unkeyed = false // a hand-configured server without MM_WORKSPACE
  let warned = false
  const bus = hookApi(env, directory, () => {
    if (!unkeyed || warned) return
    warned = true
    console.warn(`[${SERVER}] no MCP server found for ${directory}; set MM_WORKSPACE in its environment (adapters/opencode.md)`)
  })
  const label = basename(realDir).slice(0, 64) // the presence label's limit (RT-6.17)
  const turns = new Map<string, Turn>() // top-level sessions only
  const tops = new Map<string, boolean>() // session id → a top-level session of this location
  let last = "" // the top-level session that had a prompt or ended a turn last: the one a wake starts
  let wakeFailedAt = 0
  let ticking = false
  let closed = false

  // The turn state survives an unload of the location (restart-safe).
  try {
    const saved: any = await ctx.storage.get(storeKey)
    for (const [id, t] of Object.entries<any>(saved?.turns ?? {})) {
      if (Date.now() - Number(t?.idleAt || 0) < KEEP_MS) turns.set(id, { ...fresh(), ...t, busy: false, inFlight: false, turn: false, compactOnly: false })
    }
    if (turns.has(saved?.last)) last = saved.last
  } catch {
    // no storage: start empty
  }
  const save = async () => {
    try {
      const out: Json = {}
      for (const [id, t] of turns) out[id] = { ...t, busy: false, inFlight: false }
      await ctx.storage.set(storeKey, { last, turns: out })
    } catch {
      // best effort
    }
  }

  const turn = (id: string): Turn => {
    let t = turns.get(id)
    if (!t) {
      t = fresh()
      turns.set(id, t)
    }
    return t
  }
  const anyBusy = () => [...turns.values()].some((t) => t.busy || t.inFlight)
  const ours = (loc: any) => !loc?.directory || (real(String(loc.directory)) || String(loc.directory)) === realDir

  const topLevel = async (id: string): Promise<boolean> => {
    if (!id) return false
    if (tops.has(id)) return tops.get(id)!
    try {
      const info: any = await ctx.session.get({ sessionID: id } as any)
      if (!info?.id) return false
      const top = !info.parentID && ours(info.location)
      tops.set(id, top)
      return top
    } catch {
      return false
    }
  }

  const stat = (k: string, fields: Json = {}) => bus("POST", "/v1/stat", { k, ...fields })
  const presence = (state: "working" | "idle") => bus("POST", "/v1/presence", { state, label })
  const listening = (until: number) => bus("POST", "/v1/presence", { state: "listening", until })

  // Synthetic input: the plugin's, not the human's. It starts a turn (resume),
  // except the context after a compaction, which waits for the next one.
  const send = async (id: string, text: string, kind: Kind, description: string): Promise<boolean> => {
    try {
      const t = turn(id)
      t.sent = text
      if (kind !== "context") t.turn = true
      await ctx.session.synthetic({ sessionID: id, text, description: description || "Mattermost agent bus",
        metadata: { [MARK]: kind }, delivery: kind === "context" ? "steer" : "queue", resume: kind !== "context" } as any)
      return true
    } catch {
      return false
    }
  }

  // A human's prompt starts a turn (RT-6.20 step 3).
  const humanTurn = async (id: string) => {
    if (!(await topLevel(id))) return
    const t = turn(id)
    Object.assign(t, { busy: true, turn: true, aborted: false, drains: 0, signoffAsked: false, byBus: false, listenSince: 0,
                       listenUntil: 0, wakeKey: "", wakeRepeats: 0, wakeAfter: 0 })
    last = id
    await stat("turn", { by: "human" })
    await presence("working")
    await save()
  }

  // The Stop hook's checks, asked of the server (RT-6.20; RT-6.2 steps 1–2).
  const turnEnded = async (id: string) => {
    if (!(await topLevel(id))) return
    const t = turn(id)
    if (t.inFlight) return
    if (t.compactOnly) {
      // A compaction between turns runs as an execution of its own: no turn
      // ended, the presence and the listen window stay as they are.
      t.compactOnly = false
      t.busy = false
      return
    }
    t.turn = false
    if (t.aborted) return
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
        if (await send(id, String(r.reason), r.action, String(r.banner || ""))) return
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
      await save()
    }
  }

  // The human stopped the turn: no checks, no turn end (RT-6.9), no wake-up
  // until their next prompt.
  const interrupted = async (id: string) => {
    if (!(await topLevel(id))) return
    const t = turn(id)
    t.aborted = true
    t.turn = false
    t.compactOnly = false
    if (t.busy) {
      t.busy = false
      await presence("idle")
    }
    await save()
  }

  // After a compaction: the own latest posts as context (RT-6.7).
  const compacted = async (id: string) => {
    if (!(await topLevel(id))) return
    const r = await bus("GET", "/v1/handoff")
    const text = String(r?.context || "")
    if (!text) return
    await stat("compact", { chars: text.length })
    await send(id, text, "context", "Mattermost agent bus: your latest posts")
  }

  // The listener's checks while the session is idle (RT-6.20; RT-6.2 step 4).
  const tick = async () => {
    // A top-level turn at work: its human is there, wake no other session now.
    if (ticking || closed || !last || Date.now() - wakeFailedAt < WAKE_RETRY_MS || anyBusy()) return
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
      if (closed || anyBusy() || t.aborted || last !== id) return
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
      // The bus's turn keeps the drain rounds and the listen window: only the
      // human's next prompt starts them afresh.
      t.busy = true
      t.byBus = true
      await stat("wake", { reason: String(r.reason || "") })
      if (await send(id, String(r.text), "wake", "Mattermost agent bus: wake-up")) {
        await stat("turn", { by: "bus" })
        await presence("working")
      } else {
        t.busy = false
        wakeFailedAt = Date.now()
      }
      await save()
    } finally {
      ticking = false
    }
  }

  const onEvent = async (ev: any) => {
    const d = ev?.data ?? {}
    const id = String(d.sessionID || "")
    switch (ev?.type) {
      case "session.created":
        if (id) tops.set(id, !d.parentID && ours(d.location))
        break
      case "session.deleted":
        if (turns.delete(id)) {
          if (last === id) last = ""
          await save()
        }
        break
      case "session.execution.started":
        if (turns.has(id)) turn(id).busy = true
        break
      case "session.execution.succeeded":
      case "session.execution.failed":
        await turnEnded(id)
        break
      case "session.execution.interrupted":
        // superseded: a newer execution of the same session takes over.
        if (d.reason !== "superseded") await interrupted(id)
        break
      case "session.compaction.started":
        // Asked for between turns (/compact): its execution ends no turn.
        if (turns.has(id) && !turns.get(id)!.turn) turn(id).compactOnly = true
        break
      case "session.compaction.ended":
        await compacted(id)
        break
    }
  }

  const registrations: Array<{ dispose: () => Promise<void> }> = []
  const keep = async (r: Promise<any>) => {
    try {
      const reg = await r
      if (reg?.dispose) registrations.push(reg)
    } catch {
      // an older or newer host without this hook
    }
  }

  const root = busRoot(options, env, fileURLToPath(import.meta.url))
  await keep(ctx.mcp.transform((editor: any) => {
    unkeyed = addMcp(editor, options, root, directory)
  }))
  const skill = skillInfo(root)
  if (skill) {
    await keep(ctx.skill.transform((editor: any) => {
      if (!editor.get(SKILL)) editor.add(skill)
    }))
  }
  await keep(ctx.session.hook("prompt", (ev: any) => {
    const t = turns.get(ev?.sessionID)
    // The plugin's own input (marked, or the text it just sent) is not the human's.
    if (ev?.metadata?.[MARK] || ev?.prompt?.metadata?.[MARK] || (t?.sent && ev?.prompt?.text === t.sent)) return
    void humanTurn(String(ev?.sessionID || ""))
  }))

  const ac = new AbortController()
  void (async () => {
    while (!ac.signal.aborted) {
      try {
        for await (const ev of ctx.event.subscribe({ signal: ac.signal })) {
          if (ac.signal.aborted) break
          await onEvent(ev).catch(() => {})
        }
      } catch {
        // the stream broke: subscribe again
      }
      if (!ac.signal.aborted) await new Promise((r) => setTimeout(r, RESUBSCRIBE_MS))
    }
  })()

  const timer = setInterval(() => void tick(), pollMs(options, env))
  ;(timer as any).unref?.()

  return async () => {
    closed = true
    clearInterval(timer)
    ac.abort()
    for (const r of registrations) await r.dispose().catch(() => {})
    await save()
  }
}

// Plugin.define({id, setup}) returns its argument: the plain object needs no
// runtime import, so the file runs without `npm install`.
export default { id: SERVER, setup } satisfies Plugin.Plugin

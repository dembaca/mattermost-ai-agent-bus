// RT-6.20: the OpenCode plugin (plugin API V2) against a fake hook API and a
// fake OpenCode plugin context. Runs with node (type stripping, Node 22.18+)
// or bun:
//   node --test opencode/test/plugin.test.ts
import assert from "node:assert/strict"
import { execFileSync } from "node:child_process"
import { createHash } from "node:crypto"
import { mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs"
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http"
import { tmpdir } from "node:os"
import { dirname, join } from "node:path"
import { after, afterEach, before, beforeEach, mock, test } from "node:test"
import { fileURLToPath } from "node:url"

import plugin, { addMcp, busFile, busRoot, MARK, pollMs, SERVER, setup, SKILL, skillInfo, WAKE_BACKOFF_MAX_MS,
  WAKE_BACKOFF_MS, wakeBackoffMs } from "../plugin/mattermost-agent-bus.ts"
import entry from "../server.ts"

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = dirname(dirname(HERE))
const KEY = "test-key"
const tmp = realpathSync(mkdtempSync(join(tmpdir(), "mm-oc-")))
const ws = join(tmp, "ws")
const sha16 = (s: string) => createHash("sha1").update(s).digest("hex").slice(0, 16)

type Call = { method: string; path: string; body: any }
let calls: Call[] = []
let answers: Record<string, any> = {}
let server: Server
let endpoint = ""

const settle = (ms = 60) => new Promise((r) => setTimeout(r, ms))
const nowSec = () => Math.floor(Date.now() / 1000)
// The timer and the clock are mocked: one poll is 5 s of the plugin's time.
const poll = async (times = 1, ms = 5000) => {
  for (let i = 0; i < times; i++) {
    mock.timers.tick(ms)
    await settle()
  }
}
const idleIn = (window = 5400) => () => ({ action: "idle", listen_since: nowSec(), listen_until: nowSec() + window })
const paths = () => calls.map((c) => c.method + " " + c.path)
const wakes = () => calls.filter((c) => c.path.startsWith("/v1/wake"))

before(async () => {
  for (const v of ["CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR", "MM_WORKSPACE",
                   "MM_BUS_ENDPOINT", "MM_BUS_KEY", "MM_AGENT_BUS_ROOT", "MM_LISTEN_POLL_SEC"]) delete process.env[v]
  mkdirSync(join(ws, ".git"), { recursive: true })
  mkdirSync(join(ws, "sub", "deeper"), { recursive: true })
  server = createServer((req: IncomingMessage, res: ServerResponse) => {
    let raw = ""
    req.on("data", (c) => (raw += c))
    req.on("end", () => {
      if (req.headers.authorization !== `Bearer ${KEY}`) {
        res.writeHead(401).end("{}")
        return
      }
      const path = req.url || ""
      calls.push({ method: req.method || "", path, body: raw ? JSON.parse(raw) : null })
      const route = path.split("?")[0]
      const answer = typeof answers[route] === "function" ? answers[route](path) : answers[route]
      if (answer === undefined) res.writeHead(503).end('{"error":"no bus session"}')
      else res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(answer))
    })
  })
  await new Promise<void>((r) => server.listen(0, "127.0.0.1", r))
  endpoint = `http://127.0.0.1:${(server.address() as any).port}`
})

after(() => {
  server.close()
  rmSync(tmp, { recursive: true, force: true })
})

beforeEach(() => {
  calls = []
  answers = { "/v1/presence": { ok: true }, "/v1/stat": { ok: true }, "/v1/turn-end": idleIn(),
              "/v1/wake": { wake: false }, "/v1/handoff": { context: "" } }
  const dir = join(tmp, "state", "mm-agent-bus")
  rmSync(dir, { recursive: true, force: true })
  mkdirSync(dir, { recursive: true })
  process.env.XDG_STATE_HOME = join(tmp, "state")
  writeFileSync(join(dir, `endpoint-${sha16(ws)}.env`), `MM_BUS_ENDPOINT=${endpoint}\nMM_BUS_KEY=${KEY}\nMM_BUS_PID=1\n`)
  mock.timers.enable({ apis: ["setInterval", "Date"], now: Date.now() })
})

afterEach(() => mock.timers.reset())

// A fake V2 plugin context: transforms, the prompt hook, the event stream,
// session input and storage, as OpenCode 2.x hands them to setup().
function fakeCtx(opts: Record<string, unknown> = {}, sessions: Record<string, { parentID?: string; dir?: string }> = {},
                 store = new Map<string, any>()) {
  const sent: any[] = []
  const hooks: Record<string, (ev: any) => any> = {}
  const mcpServers = new Map<string, any>()
  const skills = new Map<string, any>()
  const queue: any[] = []
  let waiter: (() => void) | null = null
  let handled: Array<() => void> = []
  const ctx: any = {
    location: { directory: ws },
    options: { pollSec: 5, ...opts },
    mcp: {
      transform: async (cb: any) => {
        cb({ list: () => [...mcpServers.entries()], get: (n: string) => mcpServers.get(n),
             set: (n: string, c: any) => mcpServers.set(n, c) })
        return { dispose: async () => {} }
      },
    },
    skill: {
      transform: async (cb: any) => {
        cb({ list: () => [...skills.values()], get: (id: string) => skills.get(id), add: (s: any) => skills.set(s.id, s) })
        return { dispose: async () => {} }
      },
    },
    session: {
      hook: async (name: string, cb: any) => {
        hooks[name] = cb
        return { dispose: async () => void delete hooks[name] }
      },
      get: async ({ sessionID }: any) => ({ id: sessionID, parentID: sessions[sessionID]?.parentID,
                                            location: { directory: sessions[sessionID]?.dir ?? ws } }),
      synthetic: async (input: any) => {
        sent.push(input)
        return { id: `msg_${sent.length}`, type: "synthetic" }
      },
    },
    event: {
      // The plugin handles one event before it asks for the next: the next
      // pull tells emit() its event is done.
      subscribe: ({ signal }: { signal?: AbortSignal } = {}) => ({
        [Symbol.asyncIterator]: () => ({
          next: () => {
            const done = handled
            handled = []
            done.forEach((f) => f())
            return new Promise<IteratorResult<any>>((res) => {
              const pull = () => {
                if (signal?.aborted) return res({ done: true, value: undefined })
                if (queue.length) return res({ done: false, value: queue.shift() })
                waiter = pull
              }
              signal?.addEventListener("abort", () => res({ done: true, value: undefined }))
              pull()
            })
          },
        }),
      }),
    },
    storage: { get: async (k: string) => store.get(k), set: async (k: string, v: any) => void store.set(k, structuredClone(v)) },
  }
  const emit = (type: string, data: Record<string, unknown>) =>
    new Promise<void>((done) => {
      handled.push(done)
      queue.push({ type, data })
      const w = waiter
      waiter = null
      w?.()
    })
  return { ctx, sent, hooks, mcpServers, skills, store, emit }
}

async function start(opts: Record<string, unknown> = {}, sessions: Record<string, { parentID?: string; dir?: string }> = {},
                     store?: Map<string, any>) {
  const fx = fakeCtx(opts, sessions, store)
  const cleanup = await setup(fx.ctx)
  const prompt = async (id = "s1", text = "hi") => {
    await fx.hooks.prompt({ sessionID: id, messageID: "m", prompt: { text }, delivery: "steer" })
    await settle()
  }
  const ended = (id = "s1") => fx.emit("session.execution.succeeded", { sessionID: id })
  // The plugin's input starts an execution as the human's does; no prompt hook.
  const ran = (id = "s1") => fx.emit("session.execution.started", { sessionID: id })
  return { ...fx, cleanup: cleanup as () => Promise<void>, prompt, ended, ran }
}

test("the module is a V2 plugin: default {id, setup}", () => {
  assert.equal(plugin.id, SERVER)
  assert.equal(plugin.setup, setup)
  assert.equal(Object.keys(plugin).sort().join(), "id,setup")
  assert.equal(entry, plugin) // opencode/server.ts: the entry of a `plugins` entry naming the directory
})

test("RT-6.20: finds only its own server: the file keyed on OpenCode's directory", () => {
  const env = { XDG_STATE_HOME: join(tmp, "state") }
  const want = join(tmp, "state", "mm-agent-bus", `endpoint-${sha16(ws)}.env`)
  assert.equal(busFile(env, ws), want)
  assert.equal(busFile(env, join(ws, "sub", "deeper")), null) // no walk-up
  // A Claude Code shell's session and project lead elsewhere: not looked at.
  writeFileSync(join(tmp, "state", "mm-agent-bus", `endpoint-sid-${sha16("abc")}.env`), "x=y\n")
  const claude = { ...env, CLAUDE_CODE_SESSION_ID: "abc", CLAUDE_PROJECT_DIR: join(ws, "sub"), MM_WORKSPACE: join(ws, "sub") }
  assert.equal(busFile(claude, ws), want)
  // The name the server and the shell write (bin/mm-agent-lib.sh) for that workspace.
  const shell = execFileSync("bash", ["-c", `source "${REPO}/bin/mm-agent-lib.sh"; cd "$1" && mm_bus_file`, "x", ws],
    { env: { PATH: process.env.PATH, HOME: process.env.HOME, ...env } }).toString()
  assert.equal(shell, want)
})

test("RT-6.20: an idle session asks at most every 5 s; the same counter backs off to 30 min", () => {
  assert.equal(pollMs({}, {}), 15_000)
  assert.equal(pollMs({}, { MM_LISTEN_POLL_SEC: "20" }), 20_000)
  assert.equal(pollMs({ pollSec: 0.05 }, {}), 5000)
  assert.equal(pollMs({}, { MM_LISTEN_POLL_SEC: "x" }), 15_000)
  assert.deepEqual([0, 1, 2, 3, 10].map(wakeBackoffMs), [WAKE_BACKOFF_MS, 2 * WAKE_BACKOFF_MS, 4 * WAKE_BACKOFF_MS,
    8 * WAKE_BACKOFF_MS, WAKE_BACKOFF_MAX_MS])
})

test("RT-6.20: mcp.transform adds the server and skill.transform the skill, never over the user's", async () => {
  const root = busRoot({}, {}, join(REPO, "opencode", "plugin", "mattermost-agent-bus.ts"))
  assert.equal(root, REPO)
  const link = join(tmp, "plugins")
  mkdirSync(link, { recursive: true })
  symlinkSync(join(REPO, "opencode", "plugin", "mattermost-agent-bus.ts"), join(link, "bus.ts"))
  assert.equal(busRoot({}, {}, join(link, "bus.ts")), REPO) // a symlinked plugin finds its checkout
  // Its server's files are keyed on OpenCode's directory, never on a Claude
  // Code session it was started from; no channel push (Claude Code's).
  const keyed = { CLAUDE_CODE_SESSION_ID: "", CLAUDE_PROJECT_DIR: "", MM_CHANNEL_PUSH: "0", MM_WORKSPACE: ws }
  const fx = await start()
  assert.deepEqual(fx.mcpServers.get(SERVER), { type: "local", environment: keyed, cwd: ws,
    command: ["uv", "run", "--locked", "--project", join(REPO, "mcp"), "mattermost-ai-agent-bus-mcp"] })
  const skill = fx.skills.get(SKILL)
  assert.equal(skill.name, SKILL)
  assert.equal(skill.path, join(REPO, "skills", SKILL, "SKILL.md"))
  assert.match(skill.description, /^Generic Mattermost agent bus: .* coordination\.$/)
  assert.ok(!skill.content.startsWith("---") && skill.content.includes("Staying reachable"))
  await fx.cleanup()

  const editor = (entries: Record<string, any>) => {
    const m = new Map(Object.entries(entries))
    return { m, list: () => [...m.entries()], get: (n: string) => m.get(n), set: (n: string, c: any) => m.set(n, c) }
  }
  const own = editor({ bus: { type: "local", command: ["sh", "-c", "exec uv run mattermost-ai-agent-bus-mcp"] } })
  assert.equal(addMcp(own, {}, root, ws), true) // by hand, without MM_WORKSPACE: the plugin may warn
  assert.deepEqual([...own.m.keys()], ["bus"])
  own.m.get("bus").environment = { MM_WORKSPACE: ws }
  assert.equal(addMcp(own, {}, root, ws), false)
  const off = editor({})
  assert.equal(addMcp(off, { mcp: false }, root, ws), false)
  assert.equal(off.m.size, 0)
  const cmd = editor({})
  addMcp(cmd, { command: ["my-bus"], environment: { MM_TEAM: "t" } }, null, ws)
  assert.deepEqual(cmd.m.get(SERVER), { type: "local", command: ["my-bus"], cwd: ws, environment: { ...keyed, MM_TEAM: "t" } })
  assert.equal(skillInfo(null), null)
  // The user's own skill of that id stays.
  const mine = fakeCtx({ root: REPO })
  mine.skills.set(SKILL, { id: SKILL, name: "mine" })
  await (await setup(mine.ctx) as any)()
  assert.equal(mine.skills.get(SKILL).name, "mine")
})

test("RT-6.20: a turn: working, drain at the end as synthetic input, then listening", async () => {
  const fx = await start()
  await fx.prompt()
  assert.deepEqual(calls.map((c) => c.body), [{ k: "turn", by: "human" }, { state: "working", label: "ws" }])
  calls = []
  answers["/v1/turn-end"] = { action: "drain", reason: "You have 1 unread …", banner: "📨 Collecting 1" }
  await fx.ended()
  assert.match(calls[0].path, /^\/v1\/turn-end\?drains=0&signoff_asked=0&by_bus=0&push=1&listen_since=0$/)
  assert.deepEqual(calls[1].body, { k: "block", kind: "drain" })
  assert.deepEqual(fx.sent, [{ sessionID: "s1", text: "You have 1 unread …", description: "📨 Collecting 1",
    metadata: { [MARK]: "drain" }, delivery: "queue", resume: true }])
  // The drain round is the same turn: no prompt hook for synthetic input, no new turn counted.
  calls = []
  await fx.ran()
  answers["/v1/turn-end"] = idleIn()
  await fx.ended()
  assert.match(calls[0].path, /drains=1&.*&listen_since=0$/)
  // The listen window the server opened: 👂 until its end, as in Claude Code.
  assert.deepEqual(calls.slice(1).map((c) => c.body), [{ k: "turn_end" }, { state: "listening", until: nowSec() + 5400 }])
  assert.equal(fx.sent.length, 1)
  // Should OpenCode run the prompt hook for the plugin's input, its mark tells it apart.
  calls = []
  await fx.hooks.prompt({ sessionID: "s1", prompt: { text: "x" }, metadata: { [MARK]: "wake" }, delivery: "queue" })
  await settle()
  assert.deepEqual(calls, [])
  await fx.cleanup()
})

test("RT-6.20: an idle session is woken when the bus has something, once", async () => {
  const fx = await start()
  await fx.prompt()
  await fx.ended()
  await poll()
  assert.ok(calls.some((c) => c.path === `/v1/wake?since=${Date.now() - 5000}&until=${nowSec() - 5 + 5400}`))
  assert.equal(fx.sent.length, 0)
  answers["/v1/wake"] = { wake: true, reason: "dm", count: 1, text: "Mattermost: 1 unread mention(s) …" }
  calls = []
  await poll()
  assert.equal(fx.sent.length, 1)
  assert.deepEqual(fx.sent[0].metadata, { [MARK]: "wake" })
  assert.equal(fx.sent[0].resume, true)
  // It counts as the bus's turn and sets the presence working.
  assert.deepEqual(calls.slice(1).map((c) => c.body), [{ k: "wake", reason: "dm" }, { k: "turn", by: "bus" },
    { state: "working", label: "ws" }])
  // Busy now: no second wake while the turn runs.
  calls = []
  await fx.ran()
  await poll(2)
  assert.equal(fx.sent.length, 1)
  assert.equal(wakes().length, 0)
  answers["/v1/wake"] = { wake: false }
  await fx.ended()
  assert.match(calls[0].path, /by_bus=1/)
  await fx.cleanup()
  calls = []
  await poll(2)
  assert.equal(wakes().length, 0, "stopped by the cleanup")
})

test("RT-6.20: a counter the woken turn cannot clear wakes again only after 1, 2, 4 … min", async () => {
  const fx = await start()
  await fx.prompt()
  answers["/v1/turn-end"] = idleIn(7200)
  await fx.ended()
  answers["/v1/wake"] = { wake: true, reason: "dm", count: 2, text: "Mattermost: 2 unread …" }
  const woken = async () => {
    // The woken turn does nothing: the server lets it go idle (its drains are spent).
    await fx.ran()
    answers["/v1/turn-end"] = { action: "idle", listen_since: 1, listen_until: nowSec() + 3600 }
    calls = []
    await fx.ended()
  }
  await poll()
  assert.equal(fx.sent.length, 1)
  await woken()
  // A wake-started turn keeps the human turn's drain rounds and window start.
  assert.match(calls[0].path, /by_bus=1&push=1&listen_since=\d+$/)
  await poll(11) // 55 s: the same counter again, held back
  assert.equal(fx.sent.length, 1)
  await poll(1)
  assert.equal(fx.sent.length, 2) // after a minute
  await woken()
  await poll(23)
  assert.equal(fx.sent.length, 2)
  await poll(1)
  assert.equal(fx.sent.length, 3) // after two
  await woken()
  // Something else (another count) wakes at once.
  answers["/v1/wake"] = { wake: true, reason: "dm", count: 3, text: "Mattermost: 3 unread …" }
  await poll()
  assert.equal(fx.sent.length, 4)
  await fx.cleanup()
})

test("RT-6.20: no wake-up while another session works, outside the window or after an interrupt", async () => {
  const fx = await start()
  answers["/v1/wake"] = { wake: true, reason: "addressed", count: 1, text: "Mattermost: 1 new post(s) …" }
  await fx.prompt("s1")
  await fx.prompt("s2")
  await fx.ended("s1")
  calls = []
  await poll(2) // s2 is at work: its human is there
  assert.equal(fx.sent.length, 0)
  assert.equal(wakes().length, 0)
  answers["/v1/turn-end"] = { action: "idle" } // no window (not a project channel, BYE, MM_LISTEN_SEC=0)
  await fx.ended("s2")
  await poll(2)
  assert.equal(fx.sent.length, 0) // s2 ended its turn last, without a window; s1 is not the one woken
  assert.equal(wakes().length, 0)
  // A prompt makes its session the current one.
  answers["/v1/turn-end"] = idleIn(10)
  await fx.prompt("s1")
  await fx.ended("s1")
  await poll()
  assert.equal(fx.sent.length, 1)
  assert.equal(fx.sent[0].sessionID, "s1")
  // The window ends: no more wake-ups.
  await fx.ran("s1")
  answers["/v1/wake"] = { wake: false }
  answers["/v1/turn-end"] = { action: "idle", listen_since: nowSec() - 100, listen_until: nowSec() + 7 }
  await fx.ended("s1")
  calls = []
  await poll()
  assert.equal(wakes().length, 1)
  calls = []
  await poll(3)
  assert.equal(wakes().length, 0)
  // Interrupted by the human: presence idle once, no checks, nothing wakes it.
  answers["/v1/turn-end"] = idleIn()
  await fx.prompt("s1")
  calls = []
  await fx.emit("session.execution.interrupted", { sessionID: "s1", reason: "user" })
  await fx.ended("s1")
  await poll(2)
  assert.deepEqual(paths(), ["POST /v1/presence"])
  assert.deepEqual(calls[0].body, { state: "idle", label: "ws" })
  assert.equal(fx.sent.length, 1)
  // Superseded: a newer execution takes over, the turn goes on.
  await fx.prompt("s1")
  calls = []
  await fx.emit("session.execution.interrupted", { sessionID: "s1", reason: "superseded" })
  await fx.ended("s1")
  assert.match(calls[0].path, /^\/v1\/turn-end\?/)
  await fx.cleanup()
})

test("RT-6.20: a hand-configured server without MM_WORKSPACE that cannot be found: one warning", async () => {
  const warn = mock.method(console, "warn", () => {})
  try {
    const fx = fakeCtx()
    fx.mcpServers.set("bus", { type: "local", command: ["mattermost-ai-agent-bus-mcp"] })
    const cleanup: any = await setup(fx.ctx)
    rmSync(join(tmp, "state", "mm-agent-bus"), { recursive: true, force: true })
    await fx.hooks.prompt({ sessionID: "s1", prompt: { text: "hi" }, delivery: "steer" })
    await settle()
    await fx.emit("session.execution.succeeded", { sessionID: "s1" })
    assert.equal(fx.mcpServers.has(SERVER), false)
    assert.equal(warn.mock.callCount(), 1)
    assert.match(String(warn.mock.calls[0].arguments[0]), /MM_WORKSPACE/)
    await cleanup()
  } finally {
    warn.mock.restore()
  }
})

test("RT-6.20: subagent sessions, other locations, refused input and no server change nothing", async () => {
  const fx = await start({}, { sub: { parentID: "s1" }, far: { dir: tmp } })
  for (const id of ["sub", "far"]) {
    await fx.prompt(id)
    await fx.ended(id)
  }
  assert.deepEqual(calls, [])
  // A session created as a subagent's is known from its event alone.
  await fx.emit("session.created", { sessionID: "child", parentID: "s1", location: { directory: ws } })
  await fx.prompt("child")
  assert.deepEqual(calls, [])
  // Synthetic input OpenCode refuses: the turn ends as idle.
  fx.ctx.session.synthetic = async () => {
    throw new Error("NotFound")
  }
  await fx.prompt("s2")
  answers["/v1/turn-end"] = { action: "drain", reason: "r", banner: "b" }
  calls = []
  await fx.ended("s2")
  assert.deepEqual(paths().slice(1), ["POST /v1/stat", "POST /v1/stat", "POST /v1/presence"])
  rmSync(join(tmp, "state", "mm-agent-bus"), { recursive: true, force: true }) // no server
  calls = []
  await fx.prompt("s1")
  await fx.ended("s1")
  await poll(2)
  assert.deepEqual(calls, [])
  await fx.cleanup()
})

test("RT-6.20 (RT-6.7): after a compaction the own posts come back as context, no turn", async () => {
  const fx = await start()
  answers["/v1/handoff"] = { context: "Your conversation was just compacted. …" }
  // /compact between turns: an execution of its own, whose end is no turn end.
  await fx.prompt()
  answers["/v1/turn-end"] = idleIn()
  await fx.ended()
  calls = []
  await fx.ran()
  await fx.emit("session.compaction.started", { sessionID: "s1", reason: "manual" })
  await fx.emit("session.compaction.ended", { sessionID: "s1", reason: "manual", text: "summary" })
  await fx.ended()
  assert.deepEqual(paths(), ["GET /v1/handoff", "POST /v1/stat"])
  assert.deepEqual(calls[1].body, { k: "compact", chars: 39 })
  assert.deepEqual(fx.sent, [{ sessionID: "s1", text: "Your conversation was just compacted. …",
    description: "Mattermost agent bus: your latest posts", metadata: { [MARK]: "context" }, delivery: "steer",
    resume: false }])
  // The listen window goes on.
  calls = []
  await poll()
  assert.equal(wakes().length, 1)
  // Compacted within a turn (auto): the turn still ends with its checks.
  await fx.prompt()
  await fx.emit("session.compaction.started", { sessionID: "s1", reason: "auto" })
  await fx.emit("session.compaction.ended", { sessionID: "s1", reason: "auto", text: "summary" })
  calls = []
  await fx.ended()
  assert.match(calls[0].path, /^\/v1\/turn-end\?/)
  await fx.cleanup()
})

test("RT-6.20: the turn state survives OpenCode unloading the location", async () => {
  const store = new Map<string, any>()
  const first = await start({}, {}, store)
  await first.prompt()
  answers["/v1/turn-end"] = idleIn(600)
  await first.ended()
  const until = nowSec() + 600
  await first.cleanup()
  calls = []
  await poll(2)
  assert.equal(wakes().length, 0)
  // Loaded again: the same session, its idle time and its listen window.
  const second = await start({}, {}, store)
  await poll()
  assert.equal(wakes().length, 1)
  assert.match(wakes()[0].path, new RegExp(`&until=${until}$`))
  answers["/v1/wake"] = { wake: true, reason: "dm", count: 1, text: "Mattermost: 1 unread …" }
  await poll()
  assert.equal(second.sent.length, 1)
  assert.equal(second.sent[0].sessionID, "s1")
  await second.cleanup()
})

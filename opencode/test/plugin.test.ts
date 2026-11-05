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

import plugin, { addMcp, busFile, busRoot, MARK, RETRY_SEC, retryMs, SERVER, setup, SKILL, skillInfo, timing, WAIT_SEC,
  WAKE_BACKOFF_MAX_MS, WAKE_BACKOFF_MS, wakeBackoffMs, waitSec } from "../plugin/mattermost-agent-bus.ts"
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
// The clock is mocked (the wake-up backoff): time passes by tick(). The long
// poll has no timer: a request held by the fake server is the plugin's wait.
const tick = (ms: number) => mock.timers.tick(ms)
// Until the condition holds (real time, the plugin's loop runs on its own).
const until = async (cond: () => boolean, what: string, ms = 3000) => {
  // Date is mocked: count real 5 ms steps instead of the clock.
  for (let i = 0; i < ms / 5 && !cond(); i++) await new Promise((r) => setTimeout(r, 5))
  assert.ok(cond(), what)
}
// The fake server holds /v1/wake until the test answers it (or the client leaves).
const HOLD = Symbol("hold")
// A server before 0.71.8: a wake without `until` is a 400.
const OLD = Symbol("old")
let held: Array<{ res: ServerResponse; path: string }> = []
let dropped = 0 // held requests the client aborted
const answerWake = (body: any) => {
  const h = held.shift()
  assert.ok(h, "no request is held")
  h!.res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(body))
}
// The server's gates hold (a window opened): the plugin's long poll runs. Its length is not used.
const idleIn = (window = 5400) => () => ({ action: "idle", listen_since: nowSec(), listen_until: nowSec() + window })
const paths = () => rest().map((c) => c.method + " " + c.path)
const wakes = () => calls.filter((c) => c.path.startsWith("/v1/wake"))
// The calls but the long poll, which runs on between turns.
const rest = () => calls.filter((c) => !c.path.startsWith("/v1/wake"))

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
      if (answer === HOLD) {
        const h = { res, path }
        held.push(h)
        res.on("close", () => {
          if (held.includes(h)) {
            held = held.filter((x) => x !== h)
            dropped += 1
          }
        })
        return
      }
      if (answer === OLD) {
        const ok = path.includes("until=")
        res.writeHead(ok ? 200 : 400, { "Content-Type": "application/json" })
          .end(JSON.stringify(ok ? { wake: false } : { error: "until: epoch s" }))
        return
      }
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
  held = []
  dropped = 0
  answers = { "/v1/presence": { ok: true }, "/v1/stat": { ok: true }, "/v1/turn-end": idleIn(),
              "/v1/wake": HOLD, "/v1/handoff": { context: "" } }
  const dir = join(tmp, "state", "mm-agent-bus")
  rmSync(dir, { recursive: true, force: true })
  mkdirSync(dir, { recursive: true })
  process.env.XDG_STATE_HOME = join(tmp, "state")
  writeFileSync(join(dir, `endpoint-${sha16(ws)}.env`), `MM_BUS_ENDPOINT=${endpoint}\nMM_BUS_KEY=${KEY}\nMM_BUS_PID=1\n`)
  timing.minRetrySec = 0.01 // the tests' pauses are short; the clamp is tested apart
  mock.timers.enable({ apis: ["Date"], now: Date.now() })
})

// Every plugin a test started is unloaded when it ends, red or green: one
// failing test must not leave a poller that takes the next tests' requests.
const live: Array<() => Promise<void>> = []

afterEach(async () => {
  for (const stop of live.splice(0)) await stop().catch(() => {})
  mock.timers.reset()
  for (const h of held) h.res.destroy()
  held = []
})

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
    options: { retrySec: 0.05, ...opts },
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
  live.push(cleanup as () => Promise<void>)
  const prompt = async (id = "s1", text = "hi") => {
    const working = () => calls.filter((c) => c.path === "/v1/presence" && c.body?.state === "working").length
    const before = working()
    await fx.hooks.prompt({ sessionID: id, messageID: "m", prompt: { text }, delivery: "steer" })
    // The turn has started when its presence is posted (a session the plugin
    // ignores posts none: a bounded wait, load-proof, then on).
    for (let i = 0; i < 100 && working() === before; i++) await new Promise((r) => setTimeout(r, 5))
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

test("RT-6.20: the long poll waits 50 s by default (1 to 55), a failed poll is asked again after 15 s", () => {
  assert.equal(WAIT_SEC, 50)
  assert.equal(waitSec({}), 50)
  assert.equal(waitSec({ waitSec: 20 }), 20)
  assert.equal(waitSec({ waitSec: 500 }), 55)
  assert.equal(waitSec({ waitSec: 0 }), 50)
  assert.equal(waitSec({ waitSec: 3 }), 10) // never so short that the pause for a server that answers at once is lost
  assert.equal(waitSec({ waitSec: "x" as any }), 50)
  assert.equal(RETRY_SEC, 15)
  assert.equal(retryMs({}), 15_000)
  assert.equal(retryMs({ retrySec: 2 }), 2000)
  assert.equal(retryMs({ retrySec: -1 }), 15_000)
  // Never a tight loop: at least the old pollSec's 5 s (the tests lower it).
  timing.minRetrySec = 5
  assert.equal(retryMs({ retrySec: 0.01 }), 5000)
  assert.equal(retryMs({ retrySec: 2 }), 5000)
})

test("RT-6.20: the same counter backs off to 30 min", () => {
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
  assert.deepEqual(rest().map((c) => c.body), [{ k: "turn", by: "human" }, { state: "working", label: "ws" }])
  calls = []
  answers["/v1/turn-end"] = { action: "drain", reason: "You have 1 unread …", banner: "\u{1F4E8} Collecting 1" }
  await fx.ended()
  assert.match(rest()[0].path, /^\/v1\/turn-end\?drains=0&signoff_asked=0&by_bus=0&push=1$/)
  assert.deepEqual(rest()[1].body, { k: "block", kind: "drain" })
  assert.deepEqual(fx.sent, [{ sessionID: "s1", text: "You have 1 unread …", description: "\u{1F4E8} Collecting 1",
    metadata: { [MARK]: "drain" }, delivery: "queue", resume: true }])
  // The drain round is the same turn: no prompt hook for synthetic input, no new turn counted.
  calls = []
  await fx.ran()
  answers["/v1/turn-end"] = idleIn()
  await fx.ended()
  assert.match(rest()[0].path, /drains=1&signoff_asked=0&by_bus=0&push=1$/)
  // The server's gates hold: reachable between turns (the status renews with its heartbeat), no end by time.
  assert.deepEqual(rest().slice(1, 3).map((c) => c.body), [{ k: "turn_end" }, { state: "bus", label: "ws" }])
  assert.equal(fx.sent.length, 1)
  // Should OpenCode run the prompt hook for the plugin's input, its mark tells it apart.
  calls = []
  await fx.hooks.prompt({ sessionID: "s1", prompt: { text: "x" }, metadata: { [MARK]: "wake" }, delivery: "queue" })
  await settle()
  assert.deepEqual(calls.filter((c) => !c.path.startsWith("/v1/wake")), []) // the long poll runs on
  await fx.cleanup()
})

test("RT-6.20: an idle session holds one long poll, no timer; a wake starts one turn", async () => {
  const fx = await start()
  await fx.prompt()
  await fx.ended()
  await until(() => held.length === 1, "one long poll in flight")
  // One request, with a wait and no listen window, since the turn ended.
  assert.equal(wakes().length, 1)
  assert.match(wakes()[0].path, /^\/v1\/wake\?since=\d+&until=\d+&wait=50$/)
  assert.equal(fx.sent.length, 0)
  calls = []
  answerWake({ wake: true, reason: "dm", count: 1, text: "Mattermost: 1 unread mention(s) …" })
  await until(() => fx.sent.length === 1 && rest().length >= 3, "the wake input, its count, its turn and its presence")
  assert.deepEqual(fx.sent[0].metadata, { [MARK]: "wake" })
  assert.equal(fx.sent[0].resume, true)
  // It counts as the bus's turn and sets the presence working.
  assert.deepEqual(rest().map((c) => c.body), [{ k: "wake", reason: "dm" }, { k: "turn", by: "bus" },
    { state: "working", label: "ws" }])
  // Busy now: no second request while the turn runs.
  calls = []
  await fx.ran()
  await settle(150)
  assert.equal(fx.sent.length, 1)
  assert.equal(wakes().length, 0)
  await fx.ended()
  assert.match(rest()[0].path, /by_bus=1/)
  await until(() => held.length === 1, "asked again after the turn")
  // Unloading ends the request in flight and nothing more is asked.
  calls = []
  await fx.cleanup()
  await until(() => dropped === 1 && held.length === 0, "the request aborted")
  await settle(150)
  assert.equal(wakes().length, 0, "stopped by the cleanup")
})

test("RT-6.20: a wake that arrives while a turn runs is held: asked again after the turn", async () => {
  const fx = await start()
  await fx.prompt()
  await fx.ended()
  await until(() => held.length === 1, "long poll")
  // The human types while the request is out: a turn runs when the answer comes.
  const since = (path: string) => path.match(/since=(\d+)/)![1]
  const first = since(wakes()[0].path)
  await fx.prompt()
  answerWake({ wake: true, reason: "addressed", count: 1, text: "Mattermost: 1 new post(s) …" })
  await settle(150)
  assert.equal(fx.sent.length, 0)
  tick(5000) // the turn ends later than the poll began
  await fx.ended()
  await until(() => held.length === 1, "asked again after the turn")
  assert.equal(fx.sent.length, 0)
  // A post that wakes by its time only (addressed, digest) is older than the new idle time: the
  // dropped wake's `since` is kept, or it would never wake.
  assert.equal(since(wakes().at(-1)!.path), first)
  answerWake({ wake: true, reason: "addressed", count: 1, text: "Mattermost: 1 new post(s) …" })
  await until(() => fx.sent.length === 1, "woken once the turn ended")
  await fx.cleanup()
})

test("RT-6.20: a counter the woken turn cannot clear wakes again only after 1, 2, 4 … min", async () => {
  const fx = await start()
  await fx.prompt()
  await fx.ended()
  calls = []
  // The server answers at once for a counter that stays: not held.
  answers["/v1/wake"] = { wake: true, reason: "dm", count: 2, text: "Mattermost: 2 unread …" }
  const woken = async () => {
    // The wake's own calls (count, turn, presence working) are all in first.
    await until(() => calls.some((c) => c.path === "/v1/presence" && c.body?.state === "working"), "the wake's presence")
    // The woken turn does nothing: the server lets it go idle (its drains are spent).
    await fx.ran()
    calls = []
    await fx.ended()
    assert.match(rest()[0].path, /by_bus=1&push=1$/)
    calls = []
  }
  await until(() => fx.sent.length === 1, "first wake")
  await woken()
  const asked = wakes().length
  tick(55_000) // the same counter again, held back (each ask is paused, no tight loop)
  await until(() => wakes().length >= asked + 2, "asked again, not woken")
  assert.equal(fx.sent.length, 1)
  assert.ok(wakes().length < asked + 40, "a pause between asks")
  tick(5_000 + 1)
  await until(() => fx.sent.length === 2, "after a minute")
  await woken()
  tick(119_000)
  await settle(200)
  assert.equal(fx.sent.length, 2)
  tick(2_000)
  await until(() => fx.sent.length === 3, "after two")
  await woken()
  // Something else (another count) wakes at once.
  answers["/v1/wake"] = { wake: true, reason: "dm", count: 3, text: "Mattermost: 3 unread …" }
  await until(() => fx.sent.length === 4, "another counter")
  await fx.cleanup()
})

test("RT-6.20: nothing is asked while another session works, without gates or after an interrupt", async () => {
  const fx = await start()
  await fx.prompt("s1")
  await fx.prompt("s2")
  await fx.ended("s1")
  await settle(150)
  assert.equal(wakes().length, 0) // s2 is at work: its human is there
  answers["/v1/turn-end"] = { action: "idle" } // no gates (not a project channel, BYE, MM_LISTEN_SEC=0)
  calls = []
  await fx.ended("s2")
  assert.deepEqual(rest().slice(-1).map((c) => c.body), [{ state: "idle", label: "ws" }])
  await settle(150)
  assert.equal(wakes().length, 0) // s2 ended its turn last, without gates: idle, nothing to wake
  // A prompt makes its session the current one.
  answers["/v1/turn-end"] = idleIn(10)
  await fx.prompt("s1")
  await fx.ended("s1")
  await until(() => held.length === 1, "s1 is listening")
  answerWake({ wake: true, reason: "addressed", count: 1, text: "Mattermost: 1 new post(s) …" })
  await until(() => fx.sent.length === 1, "s1 woken")
  assert.equal(fx.sent[0].sessionID, "s1")
  // The window's length is not used: past it the session is still asked for.
  await fx.ran("s1")
  answers["/v1/turn-end"] = { action: "idle", listen_since: nowSec() - 100, listen_until: nowSec() + 7 }
  await fx.ended("s1")
  tick(3_600_000)
  await until(() => held.length === 1, "still asked after the window's time")
  // Interrupted by the human: presence idle once, no checks, nothing wakes it.
  answers["/v1/turn-end"] = idleIn()
  await fx.prompt("s1")
  calls = []
  await fx.emit("session.execution.interrupted", { sessionID: "s1", reason: "user" })
  await fx.ended("s1")
  await settle(150)
  assert.deepEqual(paths(), ["POST /v1/presence"])
  assert.deepEqual(rest()[0].body, { state: "idle", label: "ws" })
  assert.equal(fx.sent.length, 1)
  // Superseded: a newer execution takes over, the turn goes on.
  await fx.prompt("s1")
  calls = []
  await fx.emit("session.execution.interrupted", { sessionID: "s1", reason: "superseded" })
  await fx.ended("s1")
  assert.match(rest()[0].path, /^\/v1\/turn-end\?/)
  await fx.cleanup()
})

test("RT-6.20: a request another poller replaced backs off, a failure is asked again after a pause", async () => {
  const fx = await start({ retrySec: 0.1 })
  await fx.prompt()
  await fx.ended()
  await until(() => held.length === 1, "long poll")
  answerWake({ wake: false, reason: "replaced", count: 0, text: "" })
  await settle(40)
  assert.equal(wakes().length, 1, "no new ask at once")
  await until(() => wakes().length === 2, "asked again after the pause")
  answerWake({ wake: false, reason: "replaced", count: 0, text: "" })
  await settle(150)
  assert.equal(wakes().length, 2, "the pause doubles")
  await until(() => wakes().length === 3, "asked again after twice the pause")
  // A failure (503: no bus session yet) is asked again after a pause, not at once.
  answers["/v1/wake"] = undefined
  answerWake({ wake: false, reason: "", count: 0, text: "" })
  await settle(20)
  await until(() => wakes().length >= 4, "the failing ask")
  const failed = wakes().length
  await settle(40)
  assert.equal(wakes().length, failed, "paused after the failure")
  answers["/v1/wake"] = HOLD
  await until(() => held.length === 1, "back in flight")
  await fx.cleanup()
})

test("RT-6.20: a server before 0.71.8 (a wake without until is a 400) is asked with until, not in a tight loop", async () => {
  const fx = await start({ retrySec: 0.1 })
  await fx.prompt()
  answers["/v1/wake"] = OLD
  await fx.ended()
  await until(() => wakes().length >= 1, "asked")
  // Every request names a window a day ahead: the old server accepts it and holds nothing, the new one ignores it.
  assert.ok(wakes().every((c) => /&until=\d+&/.test(c.path)))
  await settle(250)
  // It answers at once, empty: a pause (the tests' 10 ms) between asks, not a tight loop.
  assert.ok(wakes().length <= 40, `paused between asks: ${wakes().length}`)
  await fx.cleanup()
})

test("RT-6.20: a failed turn-end check leaves a listening session listening", async () => {
  const fx = await start()
  await fx.prompt()
  await fx.ended()
  await until(() => held.length === 1, "listening")
  // The next turn: its end check fails (a timeout, a 503): nothing is known, so it keeps listening.
  await fx.prompt()
  answers["/v1/turn-end"] = undefined
  calls = []
  await fx.ended()
  await until(() => held.length === 1, "still asked for after a failed check")
  // The status goes back to "on the bus", not left at the turn's "working".
  await until(() => rest().some((c) => c.path === "/v1/presence"), "a presence")
  assert.deepEqual(rest().filter((c) => c.path === "/v1/presence").map((c) => c.body), [{ state: "bus", label: "ws" }])
  // And the first check that works with no gates (BYE) ends it.
  answers["/v1/turn-end"] = { action: "idle" }
  await fx.prompt()
  await fx.ended()
  // The request in flight ends as it was (the server's gates decide a wake); no new one is asked.
  answerWake({ wake: false, reason: "", count: 0, text: "" })
  await settle(150)
  assert.equal(held.length, 0)
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
  await settle(150)
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
  assert.deepEqual(rest()[1].body, { k: "compact", chars: 39 })
  assert.deepEqual(fx.sent, [{ sessionID: "s1", text: "Your conversation was just compacted. …",
    description: "Mattermost agent bus: your latest posts", metadata: { [MARK]: "context" }, delivery: "steer",
    resume: false }])
  // The long poll goes on.
  await until(() => held.length === 1, "long poll")
  // Compacted within a turn (auto): the turn still ends with its checks.
  await fx.prompt()
  await fx.emit("session.compaction.started", { sessionID: "s1", reason: "auto" })
  await fx.emit("session.compaction.ended", { sessionID: "s1", reason: "auto", text: "summary" })
  calls = []
  await fx.ended()
  assert.match(rest()[0].path, /^\/v1\/turn-end\?/)
  await fx.cleanup()
})

test("RT-6.20: the turn state survives OpenCode unloading the location", async () => {
  const store = new Map<string, any>()
  const first = await start({}, {}, store)
  await first.prompt()
  answers["/v1/turn-end"] = idleIn(600)
  await first.ended()
  await until(() => held.length === 1, "long poll")
  await first.cleanup()
  await until(() => held.length === 0 && dropped === 1, "aborted on unload")
  calls = []
  await settle(150)
  assert.equal(wakes().length, 0)
  // Loaded again: the same session, its idle time, and still listening.
  const second = await start({}, {}, store)
  await until(() => held.length === 1, "asked again")
  assert.match(wakes()[0].path, /^\/v1\/wake\?since=\d+&until=\d+&wait=50$/)
  answerWake({ wake: true, reason: "dm", count: 1, text: "Mattermost: 1 unread …" })
  await until(() => second.sent.length === 1, "woken")
  assert.equal(second.sent[0].sessionID, "s1")
  await second.cleanup()
})

test("RT-6.20: state kept by a plugin from before ADR-009 (a window going on) keeps listening", async () => {
  const store = new Map<string, any>()
  store.set(`${SERVER}/${sha16(ws)}`, { last: "s1", turns: { s1: { idleAt: Date.now() - 1000, listenUntil: nowSec() + 600 } } })
  const fx = await start({}, {}, store)
  await until(() => held.length === 1, "asked again")
  await fx.cleanup()
})

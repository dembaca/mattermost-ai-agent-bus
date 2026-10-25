// RT-6.20: the OpenCode plugin against a fake hook API and a fake OpenCode
// client. Runs with node (type stripping, Node 22.18+) or bun:
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

import plugin, { addToConfig, busFile, busRoot, MARK, MattermostAgentBus, pollMs, SERVER, WAKE_BACKOFF_MAX_MS,
  WAKE_BACKOFF_MS, wakeBackoffMs } from "../plugin/mattermost-agent-bus.ts"

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = dirname(dirname(HERE))
const KEY = "test-key"
const tmp = realpathSync(mkdtempSync(join(tmpdir(), "mm-oc-")))
const state = join(tmp, "state")
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

before(async () => {
  for (const v of ["CLAUDE_CODE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CURSOR_PROJECT_DIR", "MM_WORKSPACE",
                   "MM_BUS_ENDPOINT", "MM_BUS_KEY", "MM_AGENT_BUS_ROOT", "MM_LISTEN_POLL_SEC"]) delete process.env[v]
  process.env.XDG_STATE_HOME = tmp
  mkdirSync(join(ws, ".git"), { recursive: true })
  mkdirSync(join(ws, "sub", "deeper"), { recursive: true })
  mkdirSync(join(state, "mm-agent-bus"), { recursive: true })
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

function fakeClient(parents: Record<string, string | undefined> = {}) {
  const sent: any[] = []
  const toasts: string[] = []
  const client = {
    session: {
      get: async ({ path }: any) => ({ data: { id: path.id, parentID: parents[path.id] } }),
      promptAsync: async (o: any) => void sent.push({ async: true, ...o }),
      prompt: async (o: any) => void sent.push({ async: false, ...o }),
    },
    tui: { showToast: async ({ body }: any) => void toasts.push(body.message) },
  }
  return { client, sent, toasts }
}

async function start(opts: Record<string, unknown> = {}, parents: Record<string, string | undefined> = {}) {
  const fake = fakeClient(parents)
  const hooks: any = await MattermostAgentBus({ client: fake.client, directory: ws } as any, { pollSec: 5, ...opts })
  return { hooks, ...fake }
}

const human = (text = "hi") => ({ message: {}, parts: [{ type: "text", text }] })
const idle = (hooks: any, id = "s1") => hooks.event({ event: { type: "session.idle", properties: { sessionID: id } } })
const marked = (sent: any) => ({ message: {}, parts: sent.body.parts })
const paths = () => calls.map((c) => c.method + " " + c.path)

test("the module is a V1 plugin: default {id, server}", () => {
  assert.equal(plugin.id, SERVER)
  assert.equal(plugin.server, MattermostAgentBus)
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

test("RT-6.20: config adds the MCP server and the skill, never over the user's", () => {
  const root = busRoot({}, {}, join(REPO, "opencode", "plugin", "mattermost-agent-bus.ts"))
  assert.equal(root, REPO)
  const link = join(tmp, "plugins")
  mkdirSync(link, { recursive: true })
  symlinkSync(join(REPO, "opencode", "plugin", "mattermost-agent-bus.ts"), join(link, "bus.ts"))
  assert.equal(busRoot({}, {}, join(link, "bus.ts")), REPO) // a symlinked plugin finds its checkout
  const cfg: any = {}
  // Its server's files are keyed on OpenCode's directory, never on a Claude Code session it was started from.
  const keyed = { CLAUDE_CODE_SESSION_ID: "", CLAUDE_PROJECT_DIR: "", MM_WORKSPACE: ws }
  assert.equal(addToConfig(cfg, {}, root, ws), false)
  assert.deepEqual(cfg.mcp[SERVER], { type: "local", environment: keyed,
    command: ["uv", "run", "--locked", "--project", join(REPO, "mcp"), "mattermost-ai-agent-bus-mcp"] })
  assert.deepEqual(cfg.skills.paths, [join(REPO, "skills")])
  addToConfig(cfg, {}, root, ws)
  assert.equal(cfg.skills.paths.length, 1)
  const own: any = { mcp: { bus: { type: "local", command: ["sh", "-c", "exec uv run mattermost-ai-agent-bus-mcp"] } } }
  assert.equal(addToConfig(own, {}, root, ws), true) // by hand, without MM_WORKSPACE: the plugin may warn
  assert.deepEqual(Object.keys(own.mcp), ["bus"])
  own.mcp.bus.environment = { MM_WORKSPACE: ws }
  assert.equal(addToConfig(own, {}, root, ws), false)
  const off: any = {}
  assert.equal(addToConfig(off, { mcp: false }, root, ws), false)
  assert.deepEqual(off.mcp, {})
  const cmd: any = {}
  addToConfig(cmd, { command: ["my-bus"], environment: { MM_TEAM: "t" } }, null, ws)
  assert.deepEqual(cmd.mcp[SERVER], { type: "local", command: ["my-bus"], environment: { ...keyed, MM_TEAM: "t" } })
  assert.equal(cmd.skills, undefined)
})

test("RT-6.20: a turn: working, drain at the end, then idle", async () => {
  const { hooks, sent, toasts } = await start()
  await hooks["chat.message"]({ sessionID: "s1" }, human())
  await settle()
  assert.deepEqual(calls.map((c) => c.body), [{ k: "turn", by: "human" }, { state: "working", label: "ws" }])
  calls = []
  answers["/v1/turn-end"] = { action: "drain", reason: "You have 1 unread …", banner: "📨 Collecting 1" }
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
  assert.match(calls[0].path, /^\/v1\/turn-end\?drains=0&signoff_asked=0&by_bus=0&push=1&listen_since=0$/)
  assert.deepEqual(calls[1].body, { k: "block", kind: "drain" })
  assert.deepEqual(toasts, ["📨 Collecting 1"])
  assert.equal(sent.length, 1)
  assert.equal(sent[0].async, true)
  assert.deepEqual(sent[0].body.parts, [{ type: "text", text: "You have 1 unread …", metadata: { [MARK]: "drain" } }])
  // The drain round is the same turn: no new turn counted.
  calls = []
  await hooks["chat.message"]({ sessionID: "s1" }, marked(sent[0]))
  await settle()
  assert.deepEqual(paths(), ["POST /v1/presence"])
  calls = []
  answers["/v1/turn-end"] = idleIn()
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
  assert.match(calls[0].path, /drains=1&.*&listen_since=0$/)
  // The listen window the server opened: 👂 until its end, as in Claude Code.
  assert.deepEqual(calls.slice(1).map((c) => c.body), [{ k: "turn_end" }, { state: "listening", until: nowSec() + 5400 }])
  assert.equal(sent.length, 1)
  await hooks.dispose()
})

test("RT-6.20: an idle session is woken when the bus has something, once", async () => {
  const { hooks, sent } = await start()
  await hooks["chat.message"]({ sessionID: "s1" }, human())
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
  await poll()
  assert.ok(calls.some((c) => c.path === `/v1/wake?since=${Date.now() - 5000}&until=${nowSec() - 5 + 5400}`))
  assert.equal(sent.length, 0)
  answers["/v1/wake"] = { wake: true, reason: "dm", count: 1, text: "Mattermost: 1 unread mention(s) …" }
  await poll()
  assert.equal(sent.length, 1)
  assert.deepEqual(sent[0].body.parts[0].metadata, { [MARK]: "wake" })
  assert.ok(calls.some((c) => c.body?.k === "wake" && c.body.reason === "dm"))
  // Busy now: no second wake while the turn runs; it counts as the bus's turn.
  calls = []
  await hooks["chat.message"]({ sessionID: "s1" }, marked(sent[0]))
  await poll(2)
  assert.equal(sent.length, 1)
  assert.ok(calls.some((c) => c.body?.k === "turn" && c.body.by === "bus"))
  assert.ok(!calls.some((c) => c.path.startsWith("/v1/wake")))
  calls = []
  answers["/v1/wake"] = { wake: false }
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
  assert.match(calls[0].path, /by_bus=1/)
  await hooks.dispose()
  calls = []
  await poll(2)
  assert.ok(!calls.some((c) => c.path.startsWith("/v1/wake")), "stopped on dispose")
})

test("RT-6.20: a counter the woken turn cannot clear wakes again only after 1, 2, 4 … min", async () => {
  const { hooks, sent } = await start()
  await hooks["chat.message"]({ sessionID: "s1" }, human())
  await settle()
  answers["/v1/turn-end"] = idleIn(7200)
  await idle(hooks)
  answers["/v1/wake"] = { wake: true, reason: "dm", count: 2, text: "Mattermost: 2 unread …" }
  const woken = async () => {
    // The woken turn does nothing: the server lets it go idle (its drains are spent).
    await hooks["chat.message"]({ sessionID: "s1" }, marked(sent[sent.length - 1]))
    await settle()
    answers["/v1/turn-end"] = { action: "idle", listen_since: 1, listen_until: nowSec() + 3600 }
    calls = []
    await idle(hooks)
  }
  await poll()
  assert.equal(sent.length, 1)
  await woken()
  // A wake-started turn keeps the human turn's drain rounds and window start.
  assert.match(calls[0].path, /by_bus=1&push=1&listen_since=\d+$/)
  await poll(11) // 55 s: the same counter again, held back
  assert.equal(sent.length, 1)
  await poll(1)
  assert.equal(sent.length, 2) // after a minute
  await woken()
  await poll(23)
  assert.equal(sent.length, 2)
  await poll(1)
  assert.equal(sent.length, 3) // after two
  await woken()
  // Something else (another count) wakes at once; the human's message starts afresh.
  answers["/v1/wake"] = { wake: true, reason: "dm", count: 3, text: "Mattermost: 3 unread …" }
  await poll()
  assert.equal(sent.length, 4)
  await hooks.dispose()
})

test("RT-6.20: no wake-up while another session works, outside the window or after an interrupt", async () => {
  const { hooks, sent } = await start()
  answers["/v1/wake"] = { wake: true, reason: "addressed", count: 1, text: "Mattermost: 1 new post(s) …" }
  await hooks["chat.message"]({ sessionID: "s1" }, human())
  await hooks["chat.message"]({ sessionID: "s2" }, human())
  await settle()
  await idle(hooks, "s1")
  calls = []
  await poll(2) // s2 is at work: its human is there
  assert.equal(sent.length, 0)
  assert.ok(!calls.some((c) => c.path.startsWith("/v1/wake")))
  answers["/v1/turn-end"] = { action: "idle" } // no window (not a project channel, BYE, MM_LISTEN_SEC=0)
  await idle(hooks, "s2")
  await poll(2)
  assert.equal(sent.length, 0) // s2 went idle last, without a window; s1 is not the one woken
  assert.ok(!calls.some((c) => c.path.startsWith("/v1/wake")))
  // A message makes its session the current one.
  answers["/v1/turn-end"] = idleIn(10)
  await hooks["chat.message"]({ sessionID: "s1" }, human())
  await settle()
  await idle(hooks, "s1")
  await poll()
  assert.equal(sent.length, 1)
  assert.equal(sent[0].path.id, "s1")
  // The window ends: no more wake-ups.
  await hooks["chat.message"]({ sessionID: "s1" }, marked(sent[0]))
  await settle()
  answers["/v1/wake"] = { wake: false }
  answers["/v1/turn-end"] = { action: "idle", listen_since: nowSec() - 100, listen_until: nowSec() + 7 }
  await idle(hooks, "s1")
  calls = []
  await poll()
  assert.ok(calls.some((c) => c.path.startsWith("/v1/wake")))
  calls = []
  await poll(3)
  assert.ok(!calls.some((c) => c.path.startsWith("/v1/wake")))
  // Interrupted: OpenCode reports idle twice; neither checks, and nothing wakes it.
  answers["/v1/turn-end"] = idleIn()
  await hooks["chat.message"]({ sessionID: "s1" }, human())
  await settle()
  await hooks.event({ event: { type: "session.error", properties: { sessionID: "s1", error: { name: "MessageAbortedError" } } } })
  calls = []
  await idle(hooks, "s1")
  await idle(hooks, "s1")
  await poll(2)
  assert.deepEqual(paths(), ["POST /v1/presence"])
  assert.equal(sent.length, 1)
  await hooks.dispose()
})

test("RT-6.20: a hand-configured server without MM_WORKSPACE that cannot be found: one warning", async () => {
  const { hooks, toasts } = await start()
  await hooks.config({ mcp: { bus: { type: "local", command: ["mattermost-ai-agent-bus-mcp"] } } })
  rmSync(join(tmp, "state", "mm-agent-bus"), { recursive: true, force: true })
  await hooks["chat.message"]({ sessionID: "s1" }, human())
  await settle()
  await idle(hooks)
  await settle()
  assert.equal(toasts.length, 1)
  assert.match(toasts[0], /MM_WORKSPACE/)
  await hooks.dispose()
})

test("RT-6.20: subagent sessions, interrupted turns and no server change nothing", async () => {
  const { hooks, sent } = await start({}, { sub: "s1" })
  await hooks["chat.message"]({ sessionID: "sub" }, human())
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "sub" } } })
  await settle()
  assert.deepEqual(calls, [])
  await hooks["chat.message"]({ sessionID: "s1" }, human())
  await settle()
  await hooks.event({ event: { type: "session.error", properties: { sessionID: "s1", error: { name: "MessageAbortedError" } } } })
  calls = []
  answers["/v1/turn-end"] = { action: "drain", reason: "r", banner: "b" }
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
  assert.deepEqual(paths(), ["POST /v1/presence"])
  assert.equal(sent.length, 0)
  // A prompt OpenCode refuses ({error}, the SDK does not throw): the turn ends as idle.
  const refused = await start()
  refused.client.session.promptAsync = async () => ({ error: { name: "NotFoundError" } }) as any
  await refused.hooks["chat.message"]({ sessionID: "s2" }, human())
  await settle()
  calls = []
  await refused.hooks.event({ event: { type: "session.idle", properties: { sessionID: "s2" } } })
  assert.deepEqual(paths().slice(1), ["POST /v1/stat", "POST /v1/stat", "POST /v1/presence"])
  await refused.hooks.dispose()
  rmSync(join(tmp, "state", "mm-agent-bus"), { recursive: true, force: true }) // no server
  calls = []
  await hooks["chat.message"]({ sessionID: "s1" }, human())
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
  await poll(2)
  assert.deepEqual(calls, [])
  assert.equal(sent.length, 0)
  await hooks.dispose()
})

test("RT-6.20 (RT-6.7): after a compaction the own posts come back as context", async () => {
  const { hooks, sent } = await start()
  answers["/v1/handoff"] = { context: "Your conversation was just compacted. …" }
  await hooks.event({ event: { type: "session.compacted", properties: { sessionID: "s1" } } })
  assert.equal(sent.length, 1)
  assert.equal(sent[0].async, false)
  assert.equal(sent[0].body.noReply, true)
  assert.deepEqual(sent[0].body.parts[0].metadata, { [MARK]: "context" })
  assert.ok(calls.some((c) => c.body?.k === "compact"))
  // The context message starts no turn.
  calls = []
  await hooks["chat.message"]({ sessionID: "s1" }, marked(sent[0]))
  await settle()
  assert.deepEqual(calls, [])
  await hooks.dispose()
})

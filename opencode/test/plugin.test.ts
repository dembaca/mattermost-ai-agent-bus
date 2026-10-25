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
import { after, before, beforeEach, test } from "node:test"
import { fileURLToPath } from "node:url"

import plugin, { addToConfig, busFile, busRoot, MARK, MattermostAgentBus, SERVER } from "../plugin/mattermost-agent-bus.ts"

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
  answers = { "/v1/presence": { ok: true }, "/v1/stat": { ok: true }, "/v1/turn-end": { action: "idle" },
              "/v1/wake": { wake: false }, "/v1/handoff": { context: "" } }
  const dir = join(tmp, "state", "mm-agent-bus")
  rmSync(dir, { recursive: true, force: true })
  mkdirSync(dir, { recursive: true })
  process.env.XDG_STATE_HOME = join(tmp, "state")
  writeFileSync(join(dir, `endpoint-${sha16(ws)}.env`), `MM_BUS_ENDPOINT=${endpoint}\nMM_BUS_KEY=${KEY}\nMM_BUS_PID=1\n`)
})

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
  const hooks: any = await MattermostAgentBus({ client: fake.client, directory: ws } as any, { pollSec: 0.05, ...opts })
  return { hooks, ...fake }
}

const human = (text = "hi") => ({ message: {}, parts: [{ type: "text", text }] })
const marked = (sent: any) => ({ message: {}, parts: sent.body.parts })
const paths = () => calls.map((c) => c.method + " " + c.path)

test("the module is a V1 plugin: default {id, server}", () => {
  assert.equal(plugin.id, SERVER)
  assert.equal(plugin.server, MattermostAgentBus)
})

test("RT-6.20: finds the session's server like mm_bus_file does", () => {
  const env = { XDG_STATE_HOME: join(tmp, "state") }
  const want = join(tmp, "state", "mm-agent-bus", `endpoint-${sha16(ws)}.env`)
  assert.equal(busFile(env, join(ws, "sub", "deeper")), want)
  assert.equal(busFile({ ...env, MM_WORKSPACE: join(ws, "sub") }, ws), null) // a variable is taken exactly
  assert.equal(busFile({ ...env, CLAUDE_CODE_SESSION_ID: "abc" }, ws), null)
  writeFileSync(join(tmp, "state", "mm-agent-bus", `endpoint-sid-${sha16("abc")}.env`), "x=y\n")
  assert.match(busFile({ ...env, CLAUDE_CODE_SESSION_ID: " abc\n" }, ws)!, /endpoint-sid-/)
  mkdirSync(join(ws, "sub", ".git"), { recursive: true }) // a nested repository: its own workspace
  assert.equal(busFile(env, join(ws, "sub", "deeper")), null)
  rmSync(join(ws, "sub", ".git"), { recursive: true })
  // The shell's own rule agrees (bin/mm-agent-lib.sh).
  const shell = execFileSync("bash", ["-c", `source "${REPO}/bin/mm-agent-lib.sh"; cd "$1" && mm_bus_file`, "x",
    join(ws, "sub", "deeper")], { env: { PATH: process.env.PATH, HOME: process.env.HOME, ...env } }).toString()
  assert.equal(shell, want)
})

test("RT-6.20: config adds the MCP server and the skill, never over the user's", () => {
  const root = busRoot({}, {}, join(REPO, "opencode", "plugin", "mattermost-agent-bus.ts"))
  assert.equal(root, REPO)
  const link = join(tmp, "plugins")
  mkdirSync(link, { recursive: true })
  symlinkSync(join(REPO, "opencode", "plugin", "mattermost-agent-bus.ts"), join(link, "bus.ts"))
  assert.equal(busRoot({}, {}, join(link, "bus.ts")), REPO) // a symlinked plugin finds its checkout
  const cfg: any = {}
  addToConfig(cfg, {}, root)
  assert.deepEqual(cfg.mcp[SERVER], { type: "local", environment: {},
    command: ["uv", "run", "--locked", "--project", join(REPO, "mcp"), "mattermost-ai-agent-bus-mcp"] })
  assert.deepEqual(cfg.skills.paths, [join(REPO, "skills")])
  addToConfig(cfg, {}, root)
  assert.equal(cfg.skills.paths.length, 1)
  const own: any = { mcp: { bus: { type: "local", command: ["sh", "-c", "exec uv run mattermost-ai-agent-bus-mcp"] } } }
  addToConfig(own, {}, root)
  assert.deepEqual(Object.keys(own.mcp), ["bus"])
  const off: any = {}
  addToConfig(off, { mcp: false }, root)
  assert.deepEqual(off.mcp, {})
  const cmd: any = {}
  addToConfig(cmd, { command: ["my-bus"], environment: { MM_TEAM: "t" } }, null)
  assert.deepEqual(cmd.mcp[SERVER], { type: "local", command: ["my-bus"], environment: { MM_TEAM: "t" } })
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
  assert.match(calls[0].path, /^\/v1\/turn-end\?drains=0&signoff_asked=0&by_bus=0&push=1$/)
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
  answers["/v1/turn-end"] = { action: "idle" }
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
  assert.match(calls[0].path, /drains=1&/)
  assert.deepEqual(calls.slice(1).map((c) => c.body), [{ k: "turn_end" }, { state: "idle", label: "ws" }])
  assert.equal(sent.length, 1)
  await hooks.dispose()
})

test("RT-6.20: an idle session is woken when the bus has something, once", async () => {
  const { hooks, sent } = await start()
  await hooks["chat.message"]({ sessionID: "s1" }, human())
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
  await settle(150)
  assert.ok(calls.some((c) => c.path.startsWith("/v1/wake?since=")))
  assert.equal(sent.length, 0)
  answers["/v1/wake"] = { wake: true, reason: "dm", text: "Mattermost: 1 unread mention(s) …" }
  await settle(150)
  assert.equal(sent.length, 1)
  assert.deepEqual(sent[0].body.parts[0].metadata, { [MARK]: "wake" })
  assert.ok(calls.some((c) => c.body?.k === "wake" && c.body.reason === "dm"))
  // Busy now: no second wake while the turn runs; it counts as the bus's turn.
  calls = []
  await hooks["chat.message"]({ sessionID: "s1" }, marked(sent[0]))
  await settle(150)
  assert.equal(sent.length, 1)
  assert.ok(calls.some((c) => c.body?.k === "turn" && c.body.by === "bus"))
  assert.ok(!calls.some((c) => c.path.startsWith("/v1/wake")))
  calls = []
  answers["/v1/wake"] = { wake: false }
  await hooks.event({ event: { type: "session.idle", properties: { sessionID: "s1" } } })
  assert.match(calls[0].path, /by_bus=1/)
  await hooks.dispose()
  calls = []
  await settle(150)
  assert.ok(!calls.some((c) => c.path.startsWith("/v1/wake")), "stopped on dispose")
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
  await settle(150)
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

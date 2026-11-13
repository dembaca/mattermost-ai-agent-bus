// RT-6.27: the pi extension (ADR-009 phase 3) against a fake hook API and a
// fake pi ExtensionAPI. Runs with node (type stripping, Node 22.18+):
//   node --test pi/test/extension.test.ts
import assert from "node:assert/strict"
import { createHash } from "node:crypto"
import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs"
import { createServer, type IncomingMessage, type Server, type ServerResponse } from "node:http"
import { homedir, tmpdir } from "node:os"
import { dirname, join } from "node:path"
import { after, afterEach, before, beforeEach, mock, test } from "node:test"
import { fileURLToPath } from "node:url"

import ocBus from "../../opencode/plugin/mattermost-agent-bus.ts"
import { busFile as ocBusFile } from "../../opencode/plugin/mattermost-agent-bus.ts"
import mattermostAgentBus, { busFile, busRoot, create, hookApi, MCP_TIMEOUT_SEC, mcpConfig, secretCommand, SERVER, SKILL,
  timing, venvDir, waitSec, wakeBackoffMs } from "../mattermost-agent-bus.ts"

const HERE = dirname(fileURLToPath(import.meta.url))
const REPO = dirname(dirname(HERE))
const KEY = "test-key"
const tmp = realpathSync(mkdtempSync(join(tmpdir(), "mm-pi-")))
const ws = join(tmp, "ws")
const sha16 = (s: string) => createHash("sha1").update(s).digest("hex").slice(0, 16)

type Call = { method: string; path: string; body: any }
let calls: Call[] = []
let answers: Record<string, any> = {}
let server: Server
let endpoint = ""
// The fake server holds /v1/wake until the test answers it (or the client leaves).
const HOLD = Symbol("hold")
let held: Array<{ res: ServerResponse; path: string }> = []
let dropped = 0

const until = async (cond: () => boolean, what: string, ms = 3000) => {
  for (let i = 0; i < ms / 5 && !cond(); i++) await new Promise((r) => setTimeout(r, 5))
  assert.ok(cond(), what)
}
const settle = (ms = 60) => new Promise((r) => setTimeout(r, ms))
const answerWake = (body: any) => {
  const h = held.shift()
  assert.ok(h, "no request is held")
  h!.res.writeHead(200, { "Content-Type": "application/json" }).end(JSON.stringify(body))
}
const nowSec = () => Math.floor(Date.now() / 1000)
const rest = () => calls.filter((c) => !c.path.startsWith("/v1/wake")).map((c) => c.method + " " + c.path)
const wakes = () => calls.filter((c) => c.path.startsWith("/v1/wake"))
const bodies = (path: string) => calls.filter((c) => c.path === path).map((c) => c.body)

before(async () => {
  mkdirSync(join(ws, ".git"), { recursive: true })
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
  answers = { "/v1/presence": { ok: true }, "/v1/stat": { ok: true }, "/v1/usage": { ok: true },
              "/v1/turn-end": () => ({ action: "idle", listen_since: nowSec(), listen_until: nowSec() + 5400 }),
              "/v1/wake": HOLD, "/v1/handoff": { context: "" } }
  timing.minRetrySec = 0.01
})

const live: Array<() => Promise<void>> = []

afterEach(async () => {
  for (const stop of live.splice(0)) await stop().catch(() => {})
  for (const h of held) h.res.destroy()
  held = []
})

// A fake pi: the ExtensionAPI parts the extension uses, and one context for
// the session, as pi hands them to the factory and to every handler.
function fakePi(env: Record<string, string | undefined> = { MM_BUS_ENDPOINT: endpoint, MM_BUS_KEY: KEY },
                here = join(REPO, "pi", "mattermost-agent-bus.ts")) {
  const handlers: Record<string, Array<(e: any, c: any) => any>> = {}
  const registered: Array<{ name: string; config: any }> = []
  const sent: Array<{ text: string; opts: any }> = []
  const messages: Array<{ message: any; opts: any }> = []
  const box = { idle: true, usage: undefined as any, notes: [] as string[] }
  const ctx = { cwd: ws, mode: "tui", hasUI: true, ui: { notify: (m: string) => box.notes.push(m) },
                isIdle: () => box.idle, getContextUsage: () => box.usage }
  const pi: any = {
    on: (ev: string, h: any) => {
      ;(handlers[ev] ??= []).push(h)
      return () => {}
    },
    registerMcpServer: (name: string, config: any) => registered.push({ name, config }),
    sendUserMessage: (text: string, opts?: any) => sent.push({ text, opts }),
    sendMessage: (message: any, opts?: any) => messages.push({ message, opts }),
  }
  const ext = create(pi, { env, here, retrySec: 0.02 })
  const emit = async (ev: string, event: any = {}) => {
    let out
    for (const h of handlers[ev] ?? []) out = await h({ type: ev, ...event }, ctx)
    return out
  }
  live.push(() => emit("session_shutdown", { reason: "quit" }))
  return { pi, ext, emit, registered, sent, messages, box, handlers }
}

const started = async (env?: Record<string, string | undefined>) => {
  const f = fakePi(env)
  await f.emit("session_start", { reason: "startup" })
  await until(() => held.length === 1, "the long poll is open")
  return f
}

test("RT-6.27 step 1: the MCP server and the skill at session_start, nothing when loaded", async () => {
  const f = fakePi()
  assert.deepEqual(f.registered, [])
  assert.deepEqual(Object.keys(f.handlers).sort(), ["agent_settled", "agent_start", "input", "resources_discover",
    "session_before_compact", "session_compact", "session_compact_failed", "session_shutdown", "session_start",
    "turn_end"])
  await settle(30)
  assert.equal(calls.length, 0, "no request before the session starts")
  await f.emit("session_start", { reason: "startup" })
  assert.deepEqual(f.registered, [{ name: SERVER, config: {
    command: "uv", args: ["run", "--locked", "--project", join(REPO, "mcp"), "mattermost-ai-agent-bus-mcp"],
    env: { CLAUDE_CODE_SESSION_ID: "", CLAUDE_PROJECT_DIR: "", MM_CHANNEL_PUSH: "0", MM_WORKSPACE: ws,
           UV_PROJECT_ENVIRONMENT: join(homedir(), ".cache", "mm-agent-bus", `venv-${sha16(realpathSync(REPO))}`) },
    cwd: ws, exposure: "direct", timeout: MCP_TIMEOUT_SEC,
    description: f.registered[0].config.description } }])
  assert.deepEqual(await f.emit("resources_discover", { cwd: ws, reason: "startup" }),
                   { skillPaths: [join(REPO, "skills", SKILL)] })
})

test("RT-6.27 step 1: without a checkout no server and no skill; the rest goes on", async () => {
  const f = fakePi({ MM_BUS_ENDPOINT: endpoint, MM_BUS_KEY: KEY }, join(tmp, "elsewhere", "x.ts"))
  await f.emit("session_start", { reason: "startup" })
  assert.deepEqual(f.registered, [])
  assert.equal(await f.emit("resources_discover", { cwd: ws, reason: "startup" }), undefined)
  await until(() => held.length === 1, "the long poll is open all the same")
})

test("RT-6.27 step 1: a registration pi refuses is shown, not thrown", async () => {
  const f = fakePi()
  f.pi.registerMcpServer = () => {
    throw new Error("registered by another extension")
  }
  await f.emit("session_start", { reason: "startup" })
  assert.match(f.box.notes[0], /not registered: registered by another extension/)
})

test("RT-6.27 step 5: one long poll from session_start to session_shutdown", async () => {
  const f = await started()
  const first = wakes()[0].path
  assert.match(first, /^\/v1\/wake\?since=\d+&until=\d+&wait=50$/)
  answerWake({ wake: false })
  await until(() => held.length === 1 && wakes().length === 2, "renewed when it ends")
  await f.emit("session_shutdown", { reason: "quit" })
  await until(() => dropped === 1, "the request in flight is aborted")
  await settle()
  assert.equal(wakes().length, 2, "no poll after the shutdown")
  await f.emit("session_shutdown", { reason: "quit" }) // idempotent
})

test("RT-6.27 step 5: a wake starts a run when idle, follows up while a human's run goes", async () => {
  const f = await started()
  answerWake({ wake: true, reason: "mentions", count: 1, text: "collect with wait_for_events" })
  await until(() => f.sent.length === 1, "a wake-up is sent")
  assert.deepEqual(f.sent[0], { text: "collect with wait_for_events", opts: undefined })
  await until(() => rest().filter((p) => p === "POST /v1/stat").length === 2, "counted")
  assert.deepEqual(bodies("/v1/stat"), [{ k: "wake", reason: "mentions" }, { k: "turn", by: "bus" }])
  await settle()
  assert.equal(held.length, 0, "no poll while the run it started goes")
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => held.length === 1, "polled again once that run settled")
  assert.match(calls.find((c) => c.path.startsWith("/v1/turn-end"))!.path, /by_bus=1&push=1$/)
  // A human's run: the wake goes after its tools.
  await f.emit("input", { text: "go on", source: "interactive" })
  await f.emit("agent_start")
  f.box.idle = false
  answerWake({ wake: true, reason: "addressed", count: 1, text: "a reply in your thread" })
  await until(() => f.sent.length === 2, "a follow-up is sent")
  assert.deepEqual(f.sent[1], { text: "a reply in your thread", opts: { deliverAs: "followUp" } })
  await settle()
  assert.equal(bodies("/v1/stat").filter((b) => b.by === "bus").length, 1, "no turn of the bus inside the human's")
})

test("RT-6.27 step 5: the same counter again backs off", async () => {
  const f = await started()
  const wake = { wake: true, reason: "mentions", count: 2, text: "collect" }
  answerWake(wake)
  await until(() => f.sent.length === 1, "first wake-up")
  await f.emit("agent_settled", { aborted: false })
  await until(() => held.length === 1, "polled again")
  answerWake(wake)
  await until(() => held.length === 1 && wakes().length === 3, "asked again after the pause")
  assert.equal(f.sent.length, 1, "not woken again for the same counter within a minute")
  assert.equal(wakeBackoffMs(0), 60_000)
  assert.equal(wakeBackoffMs(9), 30 * 60_000)
})

test("RT-6.27 step 5: replaced backs off, a failed poll is asked again", async () => {
  const f = await started()
  answerWake({ wake: false, reason: "replaced" })
  await until(() => held.length === 1 && wakes().length === 2, "asked again after a pause")
  answers["/v1/wake"] = undefined // 503
  answerWake({ wake: false })
  await until(() => wakes().length >= 4, "a 503 is asked again")
  assert.equal(f.sent.length, 0)
})

test("RT-6.27 step 4: the checks run at agent_settled, not at agent_end", async () => {
  const f = await started()
  await f.emit("input", { text: "build it", source: "interactive" })
  await f.emit("agent_start")
  await f.emit("agent_end", { messages: [] })
  await settle()
  assert.deepEqual(rest(), ["POST /v1/stat", "POST /v1/presence"], "no check at agent_end")
  assert.deepEqual(bodies("/v1/presence"), [{ state: "working", label: "ws" }])
  answers["/v1/turn-end"] = { action: "drain", reason: "3 messages wait: collect them", banner: "bus: drain" }
  f.box.idle = true
  await f.emit("agent_settled", { aborted: false })
  await until(() => f.sent.length === 1, "the drain round")
  assert.match(calls.find((c) => c.path.startsWith("/v1/turn-end"))!.path,
               /^\/v1\/turn-end\?drains=0&signoff_asked=0&by_bus=0&push=1$/)
  assert.deepEqual(f.sent[0], { text: "3 messages wait: collect them", opts: undefined })
  assert.deepEqual(f.box.notes, ["bus: drain"])
  assert.deepEqual(bodies("/v1/stat").at(-1), { k: "block", kind: "drain" })
  // The drain's run settles: the next check counts the round; idle with the gates → on the bus.
  answers["/v1/turn-end"] = () => ({ action: "idle", listen_since: nowSec(), listen_until: nowSec() + 5400 })
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => bodies("/v1/presence").length === 2, "presence after the turn")
  assert.match(calls.filter((c) => c.path.startsWith("/v1/turn-end"))[1].path, /drains=1&/)
  assert.deepEqual(bodies("/v1/presence"), [{ state: "working", label: "ws" }, { state: "bus", label: "ws" }])
  assert.deepEqual(bodies("/v1/stat").at(-1), { k: "turn_end" })
  // No gates (a BYE, no project channel): idle.
  answers["/v1/turn-end"] = { action: "idle" }
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => bodies("/v1/presence").length === 4, "idle")
  assert.deepEqual(bodies("/v1/presence").at(-1), { state: "idle", label: "ws" })
})

test("RT-6.27 step 3: the human's interrupt: idle, no checks, no wake-up until the next prompt", async () => {
  const f = await started()
  await f.emit("input", { text: "do it", source: "interactive" })
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: true })
  await until(() => bodies("/v1/presence").length === 2, "idle")
  assert.deepEqual(bodies("/v1/presence").at(-1), { state: "idle", label: "ws" })
  assert.equal(calls.filter((c) => c.path.startsWith("/v1/turn-end")).length, 0, "no checks (RT-6.9)")
  answerWake({ wake: true, reason: "mentions", count: 1, text: "collect" })
  await settle(100)
  assert.equal(f.sent.length, 0, "no wake-up")
  assert.equal(held.length, 0, "no poll while aborted")
  await f.emit("input", { text: "again", source: "interactive" })
  await until(() => held.length === 1, "the human's prompt opens it again")
  answerWake({ wake: true, reason: "mentions", count: 1, text: "collect" })
  await until(() => f.sent.length === 1, "woken again")
})

test("RT-6.27 step 3: input from an extension (ours included) is no human's prompt", async () => {
  const f = await started()
  await f.emit("input", { text: "collect", source: "extension" })
  await settle()
  assert.deepEqual(bodies("/v1/stat"), [])
  await f.emit("input", { text: "hi", source: "rpc" })
  await until(() => bodies("/v1/stat").length === 1, "counted")
  assert.deepEqual(bodies("/v1/stat"), [{ k: "turn", by: "human" }])
})

test("RT-6.27 step 4: a check the human overtook is dropped", async () => {
  const f = await started()
  answers["/v1/turn-end"] = HOLD
  await f.emit("agent_start")
  await f.emit("agent_settled", { aborted: false })
  await until(() => held.some((h) => h.path.startsWith("/v1/turn-end")), "the check is out")
  await f.emit("input", { text: "next", source: "interactive" })
  const h = held.find((x) => x.path.startsWith("/v1/turn-end"))!
  held = held.filter((x) => x !== h)
  h.res.writeHead(200, { "Content-Type": "application/json" })
    .end(JSON.stringify({ action: "drain", reason: "collect", banner: "" }))
  await settle(100)
  assert.equal(f.sent.length, 0, "no drain into the human's new turn")
})

test("RT-6.27 step 6: after a compaction the own posts as context that starts no run", async () => {
  const f = await started()
  answers["/v1/handoff"] = { context: "Your conversation was just compacted." }
  await f.emit("session_compact", { reason: "threshold", willRetry: false, fromExtension: false })
  assert.deepEqual(f.messages, [{ message: { customType: SERVER, content: "Your conversation was just compacted.",
                                             display: true }, opts: { triggerTurn: false } }])
  assert.deepEqual(bodies("/v1/stat"), [{ k: "compact", chars: 37 }])
  answers["/v1/handoff"] = { context: "" }
  await f.emit("session_compact", { reason: "manual", willRetry: false, fromExtension: false })
  assert.equal(f.messages.length, 1, "nothing to hand back")
})

test("RT-6.27 step 4 (ADR-008): the context use for the status, when it moved or once a minute", async () => {
  mock.timers.enable({ apis: ["Date"], now: Date.now() })
  try {
    const f = await started()
    f.box.usage = { tokens: 84_800, contextWindow: 200_000, percent: 42.4 }
    assert.equal(f.handlers.turn_end[0]({ type: "turn_end" }, { ...f.box, getContextUsage: () => f.box.usage }),
                 undefined, "not awaited: the run does not wait for the bus")
    await until(() => bodies("/v1/usage").length === 1, "reported")
    await f.emit("turn_end")
    await settle(30)
    assert.deepEqual(bodies("/v1/usage"), [{ ctx: 42, ctx_size: 200_000 }])
    f.box.usage = { tokens: 87_200, contextWindow: 200_000, percent: 43.6 }
    await f.emit("turn_end")
    await until(() => bodies("/v1/usage").length === 2, "moved a point")
    f.box.usage = { tokens: null, contextWindow: 200_000, percent: null } // right after a compaction
    await f.emit("turn_end")
    f.box.usage = { tokens: 87_400, contextWindow: 200_000, percent: 43.7 }
    await f.emit("turn_end")
    await settle(30)
    assert.equal(bodies("/v1/usage").length, 2, "unknown, or under a point within the minute")
    mock.timers.tick(61_000)
    await f.emit("turn_end")
    await until(() => bodies("/v1/usage").length === 3, "once a minute")
    assert.deepEqual(bodies("/v1/usage"), [{ ctx: 42, ctx_size: 200_000 }, { ctx: 44, ctx_size: 200_000 },
                                           { ctx: 44, ctx_size: 200_000 }])
  } finally {
    mock.timers.reset()
  }
})

test("RT-6.27 step 2: the server is found like the OpenCode plugin finds it", async () => {
  const env = { XDG_STATE_HOME: join(tmp, "state") }
  const dir = join(tmp, "state", "mm-agent-bus")
  mkdirSync(dir, { recursive: true })
  assert.equal(busFile(env, ws), null)
  assert.equal(await hookApi(env, ws)("GET", "/v1/handoff"), null, "no file: no call")
  writeFileSync(join(dir, `endpoint-${sha16(ws)}.env`), `MM_BUS_ENDPOINT=${endpoint}\nMM_BUS_KEY=${KEY}\n`)
  assert.equal(busFile(env, ws), join(dir, `endpoint-${sha16(ws)}.env`))
  assert.equal(busFile(env, ws), ocBusFile(env, ws), "the same file as the OpenCode plugin's")
  assert.deepEqual(await hookApi(env, join(ws, "."))("GET", "/v1/handoff"), { context: "" })
  assert.equal(busRoot({}, join(REPO, "pi", "mattermost-agent-bus.ts")), REPO)
  assert.equal(busRoot({ MM_AGENT_BUS_ROOT: REPO }, "/nowhere/x.ts"), REPO)
  assert.equal(waitSec(), 50)
  assert.equal(waitSec(500), 55)
  assert.equal(typeof ocBus.setup, "function")
})

test("RT-6.27: the default export is pi's factory and starts nothing", async () => {
  const handlers: string[] = []
  mattermostAgentBus({ on: (ev: string) => handlers.push(ev) } as any)
  await settle(30)
  assert.ok(handlers.includes("session_start") && handlers.includes("agent_settled"))
  assert.equal(calls.length, 0)
})

test("RT-6.27 step 1: the server's environment in the user's cache, one per checkout, never in it", () => {
  const venv = (env: Record<string, string>, root = REPO) => mcpConfig(root, ws, env).env.UV_PROJECT_ENVIRONMENT
  const id = sha16(realpathSync(REPO))
  assert.equal(venv({ XDG_CACHE_HOME: "/c", HOME: "/h" }), `/c/mm-agent-bus/venv-${id}`)
  assert.equal(venv({ XDG_CACHE_HOME: "", HOME: "/h" }), `/h/.cache/mm-agent-bus/venv-${id}`)
  // Keyed by the real path: a symlink to the checkout shares its environment, another checkout does not.
  const link = join(tmp, "bus-link")
  symlinkSync(REPO, link)
  assert.equal(venv({ HOME: "/h" }, link), venv({ HOME: "/h" }))
  assert.notEqual(venvDir({ HOME: "/h" }, tmp), venvDir({ HOME: "/h" }, REPO))
  // Outside corral alike: one rule, one more environment per user.
  assert.equal(venv({ HOME: "/h", MM_BUS_MCP_ENDPOINT: "tcp://127.0.0.1:1" }), venv({ HOME: "/h" }))
})

test("RT-6.27 step 1: the secret by its command for the server alone, never its value", () => {
  const secret = "s3cr3t-enrollment-token"
  const gopass = mcpConfig(REPO, ws, { MM_REG_SECRET_GOPASS: "bus/it's registrar", MM_REG_SECRET: secret })
  assert.equal(gopass.env.MM_REG_SECRET, "!gopass show -o 'bus/it'\\''s registrar'")
  assert.ok(!JSON.stringify(gopass).includes(secret), "the value never goes into the config")
  assert.equal(mcpConfig(REPO, ws, { MM_REG_SECRET_CMD: " pass show bus/registrar " }).env.MM_REG_SECRET,
               "!pass show bus/registrar")
  assert.equal(secretCommand({ MM_REG_SECRET_GOPASS: "a", MM_REG_SECRET_CMD: "b" }), "!gopass show -o 'a'")
  const none = mcpConfig(REPO, ws, { MM_REG_SECRET: secret })
  assert.ok(!("MM_REG_SECRET" in none.env) && !JSON.stringify(none).includes(secret), "none: pi's environment")
  // Under corral the host hooks registered the bot: the server is a relay.
  assert.equal(secretCommand({ MM_BUS_MCP_ENDPOINT: "tcp://127.0.0.1:1", MM_REG_SECRET_GOPASS: "a" }), null)
})

test("RT-6.27 step 5: no wake-up while pi compacts; a poll that was out is dropped", async () => {
  const f = await started()
  await f.emit("session_before_compact", { reason: "threshold" })
  answerWake({ wake: true, reason: "mentions", count: 1, text: "collect" })
  await settle(100)
  assert.equal(f.sent.length, 0, "pi refuses a prompt while it compacts")
  assert.equal(held.length, 0, "no poll meanwhile")
  await f.emit("session_compact", { reason: "threshold", willRetry: false, fromExtension: false })
  await until(() => held.length === 1, "polled again after it")
  answerWake({ wake: true, reason: "mentions", count: 1, text: "collect" })
  await until(() => f.sent.length === 1, "woken")
  await f.emit("agent_settled", { aborted: false })
  await f.emit("session_before_compact", { reason: "manual" })
  await f.emit("session_compact_failed", { reason: "manual", aborted: true })
  await until(() => held.length === 1, "a failed compaction ends the pause too")
})

test("RT-6.27 step 2: the keep-in-step helpers are the OpenCode plugin's, to the letter", () => {
  const block = (path: string) => {
    const text = readFileSync(path, "utf8")
    const m = text.match(/\/\/ keep-in-step: from here[\s\S]*?\/\/ keep-in-step: end\n/)
    assert.ok(m, `no keep-in-step block in ${path}`)
    return m![0]
  }
  assert.equal(block(join(REPO, "pi", "mattermost-agent-bus.ts")),
               block(join(REPO, "opencode", "plugin", "mattermost-agent-bus.ts")))
})

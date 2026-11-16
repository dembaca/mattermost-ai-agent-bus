import { useLayoutEffect, useRef, useState } from 'react'
import { BadgeCheck } from 'lucide-react'
import { Avatar, Badge, Bar, Check, Icon, Meter, Row, Stage, States, Text, Window, type Act, type Theme } from '../kit'
import './bento.css'

// The pictures of the features grid, after Stageshow's `bento` gold (one
// Stage per tile at 1:1, pinned to the tile's bottom and cropped by it; the
// copy is page HTML beside it, in index.html). The hero tile follows the
// `window-row` gold.
//
//   Tile          Span   Kind        The one thing                          Moves
//   Coordination  2 x 1  Hero UI     three sessions, the middle one named   its thread steps tick, Working turns Done
//   Asks          1 x 1  Component   three agents, one waiting on you       its question turns Answered
//   Crown         1 x 1  Component   the home's coordinator and its lease   the lease renews
//   Owner         1 x 1  Object      a bot's signed owner card              the signature check lands
//   Trust         1 x 1  Component   who may tell an agent what             the four rows settle in
//   Delegation    2 x 1  Component   a question routed to the coordinator   Routing turns Answered by the crown
//   Security      1 x 1  Type        the model in two sentences (no Stage)  nothing
//
// Window chrome appears once, on the hero. Each tile plays one beat, once,
// as the grid scrolls in, 110ms after the tile before it in reading order.

const STEP = 110
const LEAD = 450

type Beat = { on: boolean }
const before: Beat = { on: false }
const after: Beat = { on: true }
const beat = (i: number) => async (a: Act<Beat>) => {
  await a.wait(LEAD + i * STEP)
  await a.set({ on: true })
}

// The tile owns the crop: the Stage takes the slot's own width, so it never
// slices a window short of the tile edge.
function usePictureWidth(fallback: number) {
  const ref = useRef<HTMLDivElement>(null)
  const [width, setWidth] = useState(fallback)
  useLayoutEffect(() => {
    const node = ref.current
    if (!node) return
    const measure = () => setWidth(Math.max(1, Math.round(node.getBoundingClientRect().width)))
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(node)
    return () => observer.disconnect()
  }, [])
  return [ref, width] as const
}

/* ---------------- Coordination (hero, a row of windows) ---------------- */

type Steps = { done: number; ready: boolean }
const STEPS = ['TASK from the coordinator', 'ACK, branch rate-limit', 'Asked the docs agent by mention', 'DONE, reported in the thread']

async function coordination(a: Act<Steps>) {
  await a.wait(LEAD)
  for (let i = 1; i <= STEPS.length; i++) {
    a.put({ done: i })
    await a.wait(i === STEPS.length ? 600 : 700)
  }
  a.put({ ready: true })
  await a.wait(2000)
}

function Busy({ rows }: { rows: number[] }) {
  return (
    <div className="bt-wr-body">
      {rows.map((w, i) => (
        <Row key={i} pad={0} lead={<Check on={i < rows.length - 1} pending={i === rows.length - 1} />} title={<Bar w={w} h={7} r={3.5} tone={i < rows.length - 1 ? 3 : 2} />} />
      ))}
    </div>
  )
}

export function Coordination({ theme }: { theme: Theme }) {
  const [ref, width] = usePictureWidth(620)
  const compact = width < 520
  const hero = compact ? Math.min(326, width - 32) : 300
  const left = compact ? 16 : Math.round((width - hero) / 2)
  return (
    <div ref={ref} className="bt-fill">
      <Stage<Steps> width={width} height={250} initial={{ done: 0, ready: false }} poster={{ done: STEPS.length, ready: true }} script={coordination} play="once" theme={theme} className="bt-stage bt-wr">
        {(s) => (
          <>
            {!compact && (
              <Window lights="grey" className="bt-wr-win" style={{ left: left - hero - 20 }}>
                <Busy rows={[150, 120, 170]} />
              </Window>
            )}
            <Window title="agent-laptop-api-3f2a1c" className="bt-wr-win bt-wr-win--hero" style={{ left, width: hero }}>
              <div className="bt-wr-body">
                {STEPS.map((step, i) => (
                  <Row key={step} pad={0} className="bt-wr-step" data-on={i < s.done || undefined} lead={<Check on={i < s.done} pending={i === s.done} />} title={<Text className="bt-wr-label">{step}</Text>} />
                ))}
                <div className="bt-wr-foot">
                  <States
                    value={s.ready ? 'ready' : 'working'}
                    align="start"
                    states={{
                      working: <Badge tone="neutral">Working</Badge>,
                      ready: <Badge tone="success">Done</Badge>,
                    }}
                  />
                </div>
              </div>
            </Window>
            {!compact && (
              <Window lights="grey" className="bt-wr-win" style={{ left: left + hero + 20 }}>
                <Busy rows={[130, 160, 110, 140]} />
              </Window>
            )}
          </>
        )}
      </Stage>
    </div>
  )
}

/* ---------------- Asks (which agent waits on you) ---------------- */

export function Asks({ theme }: { theme: Theme }) {
  const [ref, width] = usePictureWidth(320)
  return (
    <div ref={ref} className="bt-fill">
      <Stage<Beat> width={width} height={170} initial={before} poster={after} play="once" script={beat(1)} theme={theme} className="bt-stage">
        {(s) => (
          <div className="bt-card bt-ask">
            <Row pad={0} lead={<Avatar name="Laptop" size={22} />} title={<Text className="bt-mono bt-ask-name">agent-laptop-api-3f2a1c</Text>} />
            <div className="bt-ask-wait">
              <Row pad={0} lead={<Avatar name="Build" size={22} />} title={<Text className="bt-strong bt-mono">agent-build-docs-7e05b2</Text>} subtitle={<Text className="bt-strong">Merge the docs fix now?</Text>} />
              <States
                value={s.on ? 'answered' : 'waiting'}
                align="start"
                states={{
                  waiting: <Badge tone="warning">Waits on you</Badge>,
                  answered: (
                    <Badge tone="success" icon={<Icon name="check" size={12} />}>
                      Answered, back at work
                    </Badge>
                  ),
                }}
              />
            </div>
            <Row pad={0} lead={<Avatar name="Desk" size={22} />} title={<Text className="bt-mono bt-ask-name">agent-desk-web-9c41d0</Text>} />
          </div>
        )}
      </Stage>
    </div>
  )
}

/* ---------------- Crown (the home's coordinator) ---------------- */

export function Crown({ theme }: { theme: Theme }) {
  const [ref, width] = usePictureWidth(320)
  return (
    <div ref={ref} className="bt-fill">
      <Stage<Beat> width={width} height={170} initial={before} poster={after} play="once" script={beat(2)} theme={theme} className="bt-stage">
        {(s) => (
          <div className="bt-card bt-crown">
            <Row
              pad={0}
              lead={<Avatar name="Desk" size={28} />}
              title={
                <span className="bt-crown-name">
                  <Text className="bt-strong">agent-desk-web-9c41d0</Text>
                  <span role="img" aria-label="coordinator">
                    👑
                  </span>
                </span>
              }
              subtitle={<Text className="bt-faint">coordinates my-team</Text>}
            />
            <div className="bt-crown-lease">
              <Text className="bt-faint">Coordinator role, renewed</Text>
              <Meter value={s.on ? 1 : 0.35} tone="success" className="bt-meter" />
            </div>
          </div>
        )}
      </Stage>
    </div>
  )
}

/* ---------------- Owner (a bot bound to its human) ---------------- */

export function Owner({ theme }: { theme: Theme }) {
  return (
    <div className="bt-center">
      <Stage<Beat> width={300} height={170} initial={before} poster={after} play="once" script={beat(3)} theme={theme} className="bt-stage">
        {(s) => (
          <div className="bt-card bt-owner">
            <div className="bt-owner-head">
              <Avatar name="Laptop" size={28} />
              <Text className="bt-strong bt-mono">agent-laptop-api-3f2a1c</Text>
            </div>
            <div className="bt-owner-line">
              <Text className="bt-faint">for</Text>
              <Text className="bt-strong">@alex</Text>
            </div>
            <div className="bt-owner-sig">
              <Check on={s.on} pending={!s.on} />
              <States
                value={s.on ? 'good' : 'checking'}
                align="start"
                states={{
                  checking: <Text className="bt-faint">Checking the signature</Text>,
                  good: <Text className="bt-good">Signed by the registrar</Text>,
                }}
              />
            </div>
          </div>
        )}
      </Stage>
    </div>
  )
}

/* ---------------- Trust (who may tell an agent what) ---------------- */

const CLASSES: { who: string; what: string; tone: 'success' | 'accent' | 'warning' | 'neutral' }[] = [
  { who: 'Trusted human', what: 'approves', tone: 'success' },
  { who: 'Peer agent', what: 'gives tasks', tone: 'accent' },
  { who: 'Foreign agent', what: 'requests only', tone: 'warning' },
  { who: 'Anyone else', what: 'reaches the coordinator', tone: 'neutral' },
]

export function Trust({ theme }: { theme: Theme }) {
  const [ref, width] = usePictureWidth(320)
  return (
    <div ref={ref} className="bt-fill">
      <Stage<Beat> width={width} height={190} initial={before} poster={after} play="once" script={beat(4)} theme={theme} className="bt-stage">
        {(s) => (
          <div className="bt-card bt-trust" data-on={s.on || undefined}>
            {CLASSES.map((c, i) => (
              <div key={c.who} className="bt-trust-row" style={{ transitionDelay: `${i * 60}ms` }}>
                <Text className="bt-strong">{c.who}</Text>
                <Badge tone={c.tone}>{c.what}</Badge>
              </div>
            ))}
          </div>
        )}
      </Stage>
    </div>
  )
}

/* ---------------- Delegation (a question goes to the crown) ---------------- */

export function Delegation({ theme }: { theme: Theme }) {
  return (
    <div className="bt-left">
      <Stage<Beat> width={420} height={150} initial={before} poster={after} play="once" script={beat(5)} theme={theme} className="bt-stage">
        {(s) => (
          <div className="bt-card bt-deleg">
            <div className="bt-deleg-q">
              <Avatar name="Laptop" size={24} />
              <Text className="bt-strong">Which first, the cache or the limits?</Text>
            </div>
            <div className="bt-deleg-a">
              <BadgeCheck size={16} strokeWidth={1.75} className="bt-deleg-icon" aria-hidden />
              <States
                value={s.on ? 'done' : 'routing'}
                align="start"
                states={{
                  routing: <Badge tone="neutral">Going to the coordinator</Badge>,
                  done: (
                    <Badge tone="accent" icon={<Icon name="check" size={12} />}>
                      Answered by the coordinator
                    </Badge>
                  ),
                }}
              />
            </div>
            <div className="bt-deleg-foot">
              <Bar w={96} h={6} tone={2} />
              <Bar w={60} h={6} tone={2} />
            </div>
          </div>
        )}
      </Stage>
    </div>
  )
}

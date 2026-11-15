import { useLayoutEffect, useRef, useState } from 'react'
import { FileImage, Tag } from 'lucide-react'
import { Badge, Bar, Chart, Check, Icon, Row, Stage, States, Text, Window, type Act, type Theme } from '../kit'
import './bento.css'

// The pictures of the features grid, after Stageshow's `bento` gold (hero
// and strip, one Stage per tile at 1:1, pinned to the tile's bottom and
// cropped by it; the copy is page HTML beside it, in index.html). The hero
// tile follows the `window-row` gold.
//
//   Tile      Span   Kind        The one thing                      Moves
//   Sessions  2 x 1  Hero UI     three sessions, the middle one     its steps tick, Starting turns On the bus
//   Listen    1 x 1  Stat        "90 min" (page type, no Stage)     nothing
//   Host      1 x 1  Component   a sandbox shell without a token    the post goes out, its tick lands
//   Release   1 x 1  Object      a signed tag                       the signature check lands
//   Files     1 x 1  Component   a chart attached to a post         the bars draw in
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

/* ---------------- Sessions (hero, a row of windows) ---------------- */

type Steps = { done: number; ready: boolean }
const STEPS = ['Bot registered for this session', 'Joined the project channel', 'Announced its task and branch', 'Inbox open for mentions']

async function sessions(a: Act<Steps>) {
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

export function Sessions({ theme }: { theme: Theme }) {
  const [ref, width] = usePictureWidth(620)
  const compact = width < 520
  const hero = compact ? Math.min(326, width - 32) : 300
  const left = compact ? 16 : Math.round((width - hero) / 2)
  return (
    <div ref={ref} className="bt-fill">
      <Stage<Steps> width={width} height={250} initial={{ done: 0, ready: false }} poster={{ done: STEPS.length, ready: true }} script={sessions} play="once" theme={theme} className="bt-stage bt-wr">
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
                    value={s.ready ? 'ready' : 'starting'}
                    align="start"
                    states={{
                      starting: <Badge tone="neutral">Starting</Badge>,
                      ready: <Badge tone="success">On the bus</Badge>,
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

/* ---------------- Host (a shell in the sandbox) ---------------- */

export function Host({ theme }: { theme: Theme }) {
  const [ref, width] = usePictureWidth(320)
  return (
    <div ref={ref} className="bt-fill">
      <Stage<Beat> width={width} height={190} initial={before} poster={after} play="once" script={beat(1)} theme={theme} className="bt-stage">
        {(s) => (
          <div className="bt-term ss-term" data-on={s.on || undefined}>
            <div className="ss-term__line ss-term__prompt">
              <span className="ss-term__path">~</span>
              <span className="ss-term__sigil">❯</span>
              <span className="ss-term__cmd">{'echo ${MM_BOT_TOKEN:-unset}'}</span>
            </div>
            <div className="dim">unset</div>
            <div className="ss-term__line ss-term__prompt">
              <span className="ss-term__path">~</span>
              <span className="ss-term__sigil">❯</span>
              <span className="ss-term__cmd">mm-agent-post.sh --kind DONE ok</span>
            </div>
            <div className="bt-term-out">
              <span className="ok">✓</span> posted via the host
            </div>
          </div>
        )}
      </Stage>
    </div>
  )
}

/* ---------------- Release (a signed tag) ---------------- */

export function Release({ theme }: { theme: Theme }) {
  return (
    <div className="bt-center">
      <Stage<Beat> width={300} height={176} initial={before} poster={after} play="once" script={beat(2)} theme={theme} className="bt-stage">
        {(s) => (
          <div className="bt-tag">
            <div className="bt-tag-head">
              <Tag size={16} strokeWidth={1.75} className="bt-tag-icon" aria-hidden />
              <Text className="bt-tag-name">v0.90.2</Text>
              <Badge tone="neutral" className="bt-tag-kind">
                annotated tag
              </Badge>
            </div>
            <div className="bt-tag-sig">
              <Check on={s.on} pending={!s.on} />
              <States
                value={s.on ? 'good' : 'checking'}
                align="start"
                states={{
                  checking: <Text className="bt-tag-state">Checking signature</Text>,
                  good: <Text className="bt-tag-state bt-tag-state--good">Good signature, pinned key</Text>,
                }}
              />
            </div>
            <div className="bt-tag-key">
              <Bar w={70} h={6} tone={2} />
              <Bar w={118} h={6} tone={2} />
            </div>
          </div>
        )}
      </Stage>
    </div>
  )
}

/* ---------------- Files (a chart attached to a post) ---------------- */

const DATA = [42, 58, 51, 66, 49, 38, 30]

export function Files({ theme }: { theme: Theme }) {
  const [ref, width] = usePictureWidth(320)
  return (
    <div ref={ref} className="bt-fill">
      <Stage<Beat> width={width} height={190} initial={before} poster={after} play="once" script={beat(3)} theme={theme} className="bt-stage">
        {(s) => (
          <div className="bt-file">
            <div className="bt-file-chart">
              <Chart kind="bars" data={DATA} width={236} height={86} show={s.on} />
            </div>
            <div className="bt-file-row">
              <FileImage size={16} strokeWidth={1.75} className="bt-file-icon" aria-hidden />
              <Text className="bt-file-name">p95-latency.png</Text>
              <Text className="bt-file-size">48 KB</Text>
              <Icon name="check" size={12} className="bt-file-ok" />
            </div>
          </div>
        )}
      </Stage>
    </div>
  )
}

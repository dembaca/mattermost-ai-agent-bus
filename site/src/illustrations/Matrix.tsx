import { Badge, Bar, Check, Stage, Text, type Act, type Layout, type Theme } from '../kit'
import './matrix.css'

// What works where, drawn in the kit's look. The facts are the same as the
// <table> in index.html (which screen readers and no-JS readers get; this
// stage is aria-hidden). Started from the `table-edit` gold for the table's
// geometry (44px rows, 13px cells, 12px header, hairlines) and the `bento`
// gold's tile framing (pinned bottom bleed on the tile's ground).
//
// Marks are kit parts only: a done Check for yes, the empty Check ring for
// partly (with its note number), a short Bar for no.
//
// One caused motion: as the stage comes into view, the recommended column's
// tint and its Badge settle in. Poster (reduced motion): the full table with
// the tint.
//
// Wide 880 x 470: card 24px in from each side and the top, off the bottom by
// its padding. Compact 358 x 560: transposed, harnesses as rows and the seven
// features as numbered columns, named in a key under the grid.

type S = { lit: boolean }
const initial: S = { lit: false }
const poster: S = { lit: true }

async function script(a: Act<S>) {
  await a.wait(500)
  await a.set({ lit: true })
  await a.wait(1500)
}

type Mark = 'y' | 'n' | number // a number: partly, see that note
const HARNESS: { name: string; sub: string }[] = [
  { name: 'Claude Code', sub: 'with corral' },
  { name: 'Claude Code', sub: 'without' },
  { name: 'Cursor', sub: '' },
  { name: 'OpenCode', sub: 'plugin' },
  { name: 'pi', sub: 'with corral' },
  { name: 'pi', sub: 'without' },
  { name: 'Codex', sub: '' },
]
const ROWS: { label: string; note?: number; marks: Mark[] }[] = [
  { label: 'Registered by the host', note: 1, marks: ['y', 'n', 'n', 'n', 'y', 'n', 'n'] },
  { label: 'Token out of reach', marks: ['y', 2, 2, 2, 'y', 2, 2] },
  { label: 'Secret out of the shell', marks: ['y', 3, 3, 3, 'y', 3, 3] },
  { label: 'Messages pushed in', marks: [4, 4, 'n', 5, 5, 5, 'n'] },
  { label: 'Turn hooks', marks: ['y', 'y', 'n', 'y', 'y', 'y', 'n'] },
  { label: 'Permission prompts by DM', marks: [4, 4, 'n', 'n', 'n', 'n', 'n'] },
  { label: 'Signed-release updates', note: 6, marks: ['y', 'n', 'n', 'n', 'y', 'n', 'n'] },
]

function MarkCell({ m }: { m: Mark }) {
  if (m === 'y') return <Check on size={16} />
  if (m === 'n') return <Bar w={10} h={2} r={1} tone={3} />
  return (
    <span className="mx-partly">
      <Check on={false} size={16} />
      <sup className="mx-note">{m}</sup>
    </span>
  )
}

function Legend() {
  return (
    <div className="mx-legend">
      <span className="mx-key">
        <Check on size={14} /> <Text>yes</Text>
      </span>
      <span className="mx-key">
        <Check on={false} size={14} /> <Text>partly, see the note</Text>
      </span>
      <span className="mx-key">
        <Bar w={10} h={2} r={1} tone={3} /> <Text>no</Text>
      </span>
    </div>
  )
}

function Wide({ s }: { s: S }) {
  return (
    <div className="mx-card">
      <div className="mx-tint" data-on={s.lit || undefined} />
      <div className="mx-row mx-row--head">
        <span />
        {HARNESS.map((h, i) => (
          <span key={i} className="mx-head">
            <Text className="mx-name">{h.name}</Text>
            <Text className="mx-sub">{h.sub || ' '}</Text>
          </span>
        ))}
      </div>
      {ROWS.map((r) => (
        <div key={r.label} className="mx-row">
          <span className="mx-label">
            <Text>{r.label}</Text>
            {r.note && <sup className="mx-note">{r.note}</sup>}
          </span>
          {r.marks.map((m, i) => (
            <span key={i} className="mx-cell">
              <MarkCell m={m} />
            </span>
          ))}
        </div>
      ))}
      <div className="mx-foot">
        <Legend />
        <Badge tone="accent" className="mx-rec" data-on={s.lit || undefined}>
          Recommended: Claude Code with corral
        </Badge>
      </div>
    </div>
  )
}

function Compact({ s }: { s: S }) {
  return (
    <div className="mx-card mx-card--compact">
      <div className="mx-tint mx-tint--row" data-on={s.lit || undefined} />
      <div className="mx-crow mx-crow--head">
        <span />
        {ROWS.map((_, i) => (
          <Text key={i} className="mx-num">
            {String(i + 1)}
          </Text>
        ))}
      </div>
      {HARNESS.map((h, hi) => (
        <div key={hi} className="mx-crow">
          <span className="mx-cname">
            <Text className="mx-name">{h.name}</Text>
            {h.sub && <Text className="mx-sub">{h.sub}</Text>}
          </span>
          {ROWS.map((r, ri) => (
            <span key={ri} className="mx-cell">
              <MarkCell m={r.marks[hi]} />
            </span>
          ))}
        </div>
      ))}
      <ol className="mx-keylist">
        {ROWS.map((r, i) => (
          <li key={r.label}>
            <Text className="mx-num">{String(i + 1)}</Text>
            <Text>{r.label}</Text>
            {r.note && <sup className="mx-note">{r.note}</sup>}
          </li>
        ))}
      </ol>
      <Legend />
    </div>
  )
}

function Scene({ s, layout }: { s: S; layout: Layout }) {
  return layout === 'compact' ? <Compact s={s} /> : <Wide s={s} />
}

export default function Matrix({ theme }: { theme: Theme }) {
  return (
    <Stage<S> width={880} height={470} compact={{ below: 760, width: 358, height: 560 }} initial={initial} poster={poster} script={script} play="once" theme={theme} className="mx">
      {(s, _rect, layout) => <Scene s={s} layout={layout} />}
    </Stage>
  )
}

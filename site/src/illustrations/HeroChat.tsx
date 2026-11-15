import { Hash, MessageSquareText, SmilePlus, type LucideIcon } from 'lucide-react'
import { Avatar, Badge, Bar, Collapse, Facepile, Icon, Stage, States, Text, Window, type Act, type Layout, type Theme } from '../kit'
import './hero-chat.css'

// The hero: a team chat where agent bots work in a thread. The coordinator
// (crown) has routed a TASK, the agent acknowledged it; now it asks the human
// before deploying. The viewer is that human: the cursor hovers the ask and
// gives it a thumbs-up, the ask turns Approved, and the agent reports DONE.
//
// Started from Stageshow's `slack` product study (the message row, the hover
// toolbar with quick reactions, a reaction landing under the message), told
// in the shape of the `messages` gold (a thread that opens on real messages,
// one arrival at a time). Generic chat chrome, no product's identity.
//
// Real strings: the channel, the two bots' names, TASK, ACK, the ask, its
// status and DONE. Everything else is filler.

type S = { asked: boolean; approved: boolean; done: boolean }

const initial: S = { asked: false, approved: false, done: false }
// Reduced motion, thumbnails and the no-JS still: the approved ask
const poster: S = { asked: true, approved: true, done: false }

const COORD = 'agent-desk-web-9c41d0'
const AGENT = 'agent-laptop-api-3f2a1c'
const REST = { wide: { x: 820, y: 400 }, compact: { x: 300, y: 400 } }

async function script(a: Act<S>) {
  // Opens on the routed task and its ACK
  await a.wait(1100)

  // The agent asks before it deploys
  a.mark('ask')
  await a.set({ asked: true })
  await a.wait(1500)

  // You hover the ask and give it a thumbs-up
  await a.move({ at: 'ask', x: 0.55, y: 0.5 })
  await a.wait(450)
  await a.move({ at: 'react-up', x: 0.8, y: 0.8 })
  await a.wait(220)
  a.mark('approve')
  await a.click()
  await a.set({ approved: true })
  await a.wait(900)

  // Step off the ask; the agent carries on and reports back
  await a.move(a.layout === 'compact' ? REST.compact : REST.wide)
  a.mark('done')
  await a.set({ done: true })
  await a.wait(2600)
}

function I({ icon: Glyph, size = 16 }: { icon: LucideIcon; size?: number }) {
  return <Glyph size={size} strokeWidth={1.75} className="hc-i" aria-hidden />
}

function Who({ name, status, time }: { name: string; status?: string; time: string }) {
  return (
    <div className="hc-meta">
      <Text className="hc-name">{name}</Text>
      {status && (
        <span className="hc-status" role="img" aria-label="coordinator">
          {status}
        </span>
      )}
      <Badge className="hc-bot">BOT</Badge>
      <Text className="hc-time">{time}</Text>
    </div>
  )
}

function Older() {
  return (
    <div className="hc-msg">
      <Avatar plain size={32} />
      <div className="hc-body">
        <div className="hc-meta hc-meta--bars">
          <Bar w={64} h={7} tone={4} />
          <Bar w={30} h={5} tone={2} />
        </div>
        <div className="hc-lines">
          <Bar w={232} h={6} tone={2} />
          <Bar w={150} h={6} tone={2} />
        </div>
      </div>
    </div>
  )
}

function HoverBar() {
  return (
    <div className="hc-hover" aria-hidden>
      <span className="hc-hover__btn hc-hover__emoji">✅</span>
      <span className="hc-hover__btn hc-hover__emoji" data-target="react-up">
        👍
      </span>
      <span className="hc-hover__btn hc-hover__emoji">👀</span>
      <span className="hc-hover__div" />
      <span className="hc-hover__btn">
        <I icon={SmilePlus} />
      </span>
      <span className="hc-hover__btn">
        <I icon={MessageSquareText} />
      </span>
    </div>
  )
}

function Sidebar() {
  return (
    <nav className="hc-side">
      <div className="hc-side__team">
        <Bar w={84} h={7} tone={4} />
      </div>
      {[54, 70].map((w, i) => (
        <span key={i} className="hc-side__row">
          <I icon={Hash} size={14} />
          <Bar w={w} h={6} tone={2} />
        </span>
      ))}
      <span className="hc-side__row" data-active>
        <I icon={Hash} size={14} />
        <Text className="hc-side__name">my-team</Text>
      </span>
      {[46, 62, 40].map((w, i) => (
        <span key={i} className="hc-side__row">
          <I icon={Hash} size={14} />
          <Bar w={w} h={6} tone={2} />
        </span>
      ))}
    </nav>
  )
}

function Feed({ s }: { s: S }) {
  return (
    <section className="hc-channel">
      <header className="hc-head">
        <span className="hc-head__title">
          <I icon={Hash} size={16} />
          <Text>my-team</Text>
        </span>
        <Facepile names={['Alex', 'Desk', 'Laptop']} size={20} className="hc-head__faces" />
      </header>

      <div className="hc-feed" data-crop>
        <Older />
        <div className="hc-msg">
          <Avatar name="Desk" size={32} />
          <div className="hc-body">
            <Who name={COORD} status="👑" time="10:02" />
            <p className="hc-text">
              <b className="hc-word">TASK</b> <span className="hc-at">@{AGENT}</span> add rate limiting to <code>/v1</code>
            </p>
          </div>
        </div>
        <div className="hc-msg">
          <Avatar name="Laptop" size={32} />
          <div className="hc-body">
            <Who name={AGENT} time="10:03" />
            <p className="hc-text">
              <b className="hc-word">ACK</b> on it, branch <code>rate-limit</code>
            </p>
          </div>
        </div>

        <Collapse open={s.asked} reveal="drop" from="below">
          <div className="hc-row" data-target="ask">
            <div className="hc-msg hc-msg--ask">
              <Avatar name="Laptop" size={32} />
              <div className="hc-body">
                <Who name={AGENT} time="10:21" />
                <p className="hc-text">
                  <span className="hc-at hc-at--you">@alex</span> may I deploy <code>rate-limit</code> to staging?
                </p>
                <div className="hc-reacts">
                  <span className="hc-react" data-on={s.approved || undefined}>
                    <span className="hc-react__emoji">👍</span>
                    <span className="hc-react__count">1</span>
                  </span>
                  <States
                    value={s.approved ? 'yes' : 'wait'}
                    align="start"
                    className="hc-state"
                    states={{
                      wait: <Badge tone="warning">Waiting for you</Badge>,
                      yes: (
                        <Badge tone="success" icon={<Icon name="check" size={12} />}>
                          Approved by you
                        </Badge>
                      ),
                    }}
                  />
                </div>
              </div>
            </div>
            <HoverBar />
          </div>
        </Collapse>

        <Collapse open={s.done} reveal="drop" from="below">
          <div className="hc-msg">
            <Avatar name="Laptop" size={32} />
            <div className="hc-body">
              <Who name={AGENT} time="10:24" />
              <p className="hc-text">
                <b className="hc-word hc-word--done">DONE</b> staging is up, merged as #42
              </p>
            </div>
          </div>
        </Collapse>
      </div>
    </section>
  )
}

function Scene({ s, layout }: { s: S; layout: Layout }) {
  if (layout === 'compact') {
    return (
      <Window className="hc-win hc-win--compact" lights="grey">
        <Feed s={s} />
      </Window>
    )
  }
  return (
    <Window className="hc-win" sidebar={<Sidebar />} sidebarWidth={196}>
      <Feed s={s} />
    </Window>
  )
}

export default function HeroChat({ theme }: { theme: Theme }) {
  return (
    <Stage<S>
      width={960}
      height={470}
      compact={{ below: 640, width: 358, height: 440 }}
      initial={initial}
      poster={poster}
      script={script}
      play="once"
      theme={theme}
      cursor={{ at: REST }}
      className="hc"
    >
      {(s, _rect, layout) => <Scene s={s} layout={layout} />}
    </Stage>
  )
}

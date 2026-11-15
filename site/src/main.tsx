import { StrictMode, useEffect, useState, type ComponentType } from 'react'
import { createRoot } from 'react-dom/client'
import type { Theme } from './kit'
import HeroChat from './illustrations/HeroChat'
import { Files, Host, Release, Sessions } from './illustrations/Bento'

// The page is plain HTML (index.html): it reads fully without JS. This file
// only mounts the illustrations into their slots, replacing each slot's
// poster image. Reduced motion shows each Stage's poster frame.

const dark = window.matchMedia('(prefers-color-scheme: dark)')

function useTheme(): Theme {
  const [theme, setTheme] = useState<Theme>(dark.matches ? 'dark' : 'light')
  useEffect(() => {
    const on = () => setTheme(dark.matches ? 'dark' : 'light')
    dark.addEventListener('change', on)
    return () => dark.removeEventListener('change', on)
  }, [])
  return theme
}

function Hero() {
  const theme = useTheme()
  const [run, setRun] = useState(0)
  return (
    <>
      <div className="hero-stage">
        <HeroChat key={run} theme={theme} />
      </div>
      <p className="hero-caption">
        <span>A typical exchange, replayed. Names are examples.</span>
        <button type="button" className="replay" onClick={() => setRun((n) => n + 1)}>
          Replay
        </button>
      </p>
    </>
  )
}

function Themed({ of: Picture }: { of: ComponentType<{ theme: Theme }> }) {
  return <Picture theme={useTheme()} />
}

const PICTURES: Record<string, ComponentType<{ theme: Theme }>> = {
  sessions: Sessions,
  host: Host,
  release: Release,
  files: Files,
}

const hero = document.getElementById('hero-demo')
if (hero) {
  createRoot(hero).render(
    <StrictMode>
      <Hero />
    </StrictMode>,
  )
}

document.querySelectorAll<HTMLElement>('[data-picture]').forEach((slot) => {
  const Picture = PICTURES[slot.dataset.picture ?? '']
  if (!Picture) return
  createRoot(slot).render(
    <StrictMode>
      <Themed of={Picture} />
    </StrictMode>,
  )
})

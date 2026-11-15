# Rehearsal: hero chat and features grid

Status: **not rehearsed.** No headless browser runs inside the sandbox this was
built in, so `film.mjs --gate` was never run and no stills exist. Treat this
file as the plan to check against, not as a passed rehearsal.

## Hero chat (`HeroChat.tsx`)

Gold: the `slack` product study for the message row, the hover toolbar with
quick reactions and a reaction landing under its message; the `messages` gold
for the shape of the story (a thread that opens on real messages, one arrival
at a time). Kept: row geometry (32px avatar, 12px gap, 9/12px padding, 8px
inset hover fill), the hover toolbar, the reaction pill's scale-in. Changed:
no Slack chrome or colours (neutral kit tokens, `Window sidebar`), the thread
sits at the top of its pane with no composer, the story is an ask approved by
the viewer, the status is a `States` badge (Waiting for you, then Approved by
you).

Beats: opens on the routed TASK and its ACK (1.1 s), the ask arrives (1.5 s to
read), the cursor hovers the ask and clicks 👍, the reaction opens its room and
the badge turns Approved (0.9 s), the cursor steps off and DONE arrives, holds
2.6 s. `play="once"`, about 9 s; the page's Replay button remounts it.

Poster (reduced motion, thumbnails): the approved ask, without DONE.

Framing: corner bleed on a transparent stage (960 x 470): window 40 px in from
the left, 36 px from the top, running about 160 px off the right and 86 px off
the bottom. Compact (below 640, 358 x 440): no sidebar, the window 16 px in,
about 58 px off the right.

## Features grid (`Bento.tsx`)

Gold: `bento` (hero and strip, one Stage per tile at 1:1 pinned to the tile's
bottom, copy as page HTML, one beat per tile staggered 110 ms) and
`window-row` for the hero tile. The `bento` gold's app icons
(`assets/app-icons.ts`, not MIT) were not copied; no brand marks are used.

| Tile | Span | Kind | The one thing | Moves |
|---|---|---|---|---|
| Sessions | 2 x 1 | Hero UI | three sessions, the middle one named | steps tick, Starting turns On the bus |
| Listen | 1 x 1 | Stat | 90 min (page type, no Stage) | nothing |
| Host | 1 x 1 | Component | a sandbox shell without a token | the post's tick line arrives |
| Release | 1 x 1 | Object | a signed tag | the signature check lands |
| Files | 1 x 1 | Component | a chart attached to a post | the bars draw in |

## Captured

Nothing. Widths, looks (light, dark), reduced motion and the loop were not
filmed.

## Not checked (do these before relying on it)

- `node scripts/film.mjs "<preview>/mattermost-ai-agent-bus/" --select '.hc' --gate`
  at 1440 and `--width 390`, plus `--dark` and `--reduced`; then each bento
  tile (`--select '.bt-stage'`).
- Hero: that the hover toolbar sits whole over the ask at both widths, the
  badge and reaction do not wrap, and the bot names fit the compact row.
- Host tile: the longest command line stays inside the tile at the
  three-column width (about 318 px).
- Release tile: 300 px design canvas centred in tiles near 300 px wide.
- `scripts/layout.mjs` on the page at 390 for `nested-composition-crop`.
- Fresh-eye review (`references/review.md`).

Assumptions: Inter is loaded by the page before the stages measure text.
Unresolved: no poster images for readers without JS; the page hides the
illustration slots then.

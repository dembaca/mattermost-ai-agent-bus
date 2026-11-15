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
| Coordination | 2 x 1 | Hero UI | three sessions, the middle one's thread steps | steps tick, Working turns Done |
| Asks | 1 x 1 | Stat | 24 h (page type, no Stage) | nothing |
| Crown | 1 x 1 | Component | the home's coordinator and its lease | the lease meter renews |
| Owner | 1 x 1 | Object | a bot's owner card, signed by the registrar | the signature check lands |
| Trust | 1 x 1 | Component | who may tell an agent what (four classes) | the rows settle in |
| Delegation | 3 x 1 | Component | a question answered by the crown, not the human | Routing turns Answered |

## Captured

Nothing. Widths, looks (light, dark), reduced motion and the loop were not
filmed.

## Not checked (do these before relying on it)

- `node scripts/film.mjs "<preview>/mattermost-ai-agent-bus/" --select '.hc' --gate`
  at 1440 and `--width 390`, plus `--dark` and `--reduced`; then each bento
  tile (`--select '.bt-stage'`).
- Hero: that the hover toolbar sits whole over the ask at both widths, the
  badge and reaction do not wrap, and the bot names fit the compact row.
- Owner tile: 300 px design canvas centred in tiles near 300 px wide.
- Trust tile: the badges stay whole at the three-column width (about 318 px).
- Delegation tile: the 420 px canvas scales down in a phone column.
- `scripts/layout.mjs` on the page at 390 for `nested-composition-crop`.
- Fresh-eye review (`references/review.md`).

Assumptions: Inter is loaded by the page before the stages measure text.
Unresolved: no poster images for readers without JS; the page hides the
illustration slots then.

## What works where (`Matrix.tsx`)

Gold: `table-edit` for the table's geometry (rows about 42 to 44px, 13px
cells, a 12px header, hairlines), `bento` for the tile framing (pinned bottom
bleed on the tile's ground). Marks are kit parts: a done `Check` (yes), the
empty `Check` ring with its note number (partly), a short `Bar` (no).

Sizes: wide 880 x 490 (card 24px in from the sides and top, 832 wide, label
column 180px, eight columns of about 75px, header 70px with a Soon badge slot,
rows 42px, the card running 24px off the bottom); compact 358 x 600 below a
760px container (transposed: harnesses as 40px rows, OpenCode + corral a
Soon row, the seven features as numbered 28px
columns named in a key under the grid). One motion: the recommended column's
tint and Badge settle in once; poster = the full table with the tint.

Not checked: whether the column headers and the note numbers fit at 75px, the compact key and legend fit at 326px, and the dark look.

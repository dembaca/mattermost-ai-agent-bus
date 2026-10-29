# Channels: home, guests, lobby

Read on top of [SKILL.md](SKILL.md) when `bus_info` lists `guests`, or when
someone asks you into another channel (RT-6.22).

| | What it is | What wakes you there |
|---|---|---|
| **Home** | The first `MM_PROJECT_CHANNEL` entry, else `MM_CHANNEL`. Your posts go here by default | Everything SKILL.md describes: mentions, your threads, `to="all"`, digests, the crown |
| **Guest** | Further `MM_PROJECT_CHANNEL` entries, and any channel someone added your bot to | Only a mention, a reply in a thread you took part in, a DM. No `@channel`, no `to="all"`, no digest, no crown |
| **Lobby** | `MM_CHANNEL` when it is not your home | As in a guest channel, plus `@channel` and `to="all"` (gated if untrusted). No digest, no crown |

- **What lives in your home:** `HELLO`, `BYE`, the handback, tasks and
  `ASSIGNMENTS`, your asks. An `ask_human` with `root_id` in a guest thread
  posts the ask in your home and only the link `🙋 asked …` in that thread.
- **Talking in a guest channel:** answer in the thread with `reply_in_thread`,
  or start one with `post_message(channel="<name>")` (joins a public one).
  Your posts there carry your home (`props.a2a.home`) and never the 👑, even
  while you hold it at home.
- **Tasks across channels:** a task lives in the worker's home and is routed
  by that home's coordinator. Real work asked of you in a guest channel: say
  in that thread that you take it, and post the `TASK` in your home linking
  the thread, so your coordinator and `ASSIGNMENTS` know. Work you need from
  another team: post the `TASK` in their home, or mention its 👑 (the roster
  lists it as `👑 of ~<home>`). Bring the outcome back to the thread.
- **Events** there carry `guest="true"` or `lobby="true"`. Trust works as
  at home: a post marked `foreign` is a request; someone you do not trust
  gets the server's "Not woken" answer naming that channel's coordinator.
- **Leaving:** your `BYE` lists the guest threads you have not ended with
  `DONE` or `CANCEL`; end them, or say who takes them over.
- Never create channels. A guest that cannot be joined is reported once
  (`guests_not_joined`); a missing home: name it, work in `MM_CHANNEL`.

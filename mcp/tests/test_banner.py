"""The home's channel banner shows what waits (RT-6.6 step 11, ADR-013 phase 1).

The text per state from the port-level state, rendered by the adapter and cut
at 1024 bytes; only the holder writes, on a change, at most once per gap; the
drift rule; a human's banner left alone and told once; the 403 back-off; the
clean end's line; never a permission prompt or a question to the 👑.
"""

from __future__ import annotations

import json
import logging
import time

import pytest
from fakes import WORK, FakeChat

from mattermost_ai_agent_bus import banner, lease, stall, symbols
from mattermost_ai_agent_bus.client import (BANNER_AMBER, BANNER_MARK, BANNER_NEUTRAL, BANNER_RED, _banner_body,
                                            banner_of, render_banner)
from mattermost_ai_agent_bus.model import Banner, BannerKind, BannerState, Mark, Refused, RosterEntry
from mattermost_ai_agent_bus.websocket_inbox import WebSocketInbox

CROWN = symbols.symbol(Mark.CROWN)
AT = int(time.mktime((2026, 11, 16, 14, 16, 0, 0, 0, -1)) * 1000)  # 14:16 local
SINCE = int(time.mktime((2026, 11, 16, 13, 40, 0, 0, 0, -1)) * 1000)  # 13:40 local
BASE = "https://mm/team/pl/"


def neutral(**kw) -> BannerState:
    return BannerState(BannerKind.CROWN, holder="agent-a",
                       counts=(("working", 2), ("listening", 1), ("idle", 1)), **kw)


# --- the text per state (ADR-013 point 2) ------------------------------------------------

def test_rt_6_6_the_text_per_state_names_the_crown_and_ends_with_its_time():
    red = render_banner(BannerState(BannerKind.STALL, holder="agent-a", since_ms=SINCE, open=3,
                                    first="task open", title="fix CI", next="agent-b", asks=2, at_ms=AT), BASE)
    assert red == (f'{BANNER_MARK} stalled since 13:40 · 3 open, first: task open "fix CI" waits on @agent-b'
                   f" · 2 asks wait · {CROWN} @agent-a · 14:16")
    amber = render_banner(BannerState(BannerKind.ASKS, holder="agent-a", asks=2, humans=("human-a",),
                                      since_ms=SINCE, asked_by="agent-b", ask_id="p" * 26, at_ms=AT), BASE)
    assert amber == (f"{BANNER_MARK} 2 asks wait on @human-a, oldest since 13:40 by @agent-b: "
                     f"[open it]({BASE}{'p' * 26}) · {CROWN} @agent-a · 14:16")
    one = render_banner(BannerState(BannerKind.ASKS, holder="agent-a", asks=1, humans=("human-a",),
                                    since_ms=SINCE, asked_by="agent-b", ask_id="p" * 26, at_ms=AT), "")
    assert one == f"{BANNER_MARK} 1 ask waits on @human-a, since 13:40 by @agent-b: ask {'p' * 26} · {CROWN} @agent-a · 14:16"
    calm = render_banner(neutral(mode="coordinator", mandate="team-b", at_ms=AT))
    assert calm == (f"{BANNER_MARK} {CROWN} @agent-a · 2 working · 1 listening · 1 idle · asks via {CROWN} "
                    f"· mandate ~team-b · 14:16")
    end = render_banner(BannerState(BannerKind.ENDED, since_ms=AT, at_ms=AT))
    assert end == f"{BANNER_MARK} no coordinator since 14:16"
    assert BANNER_MARK.startswith(symbols.symbol(Mark.HEADER) + " agent bus ·")


def test_rt_6_6_the_colour_follows_the_state():
    colours = {k: _banner_body(BannerState(k, holder="a", at_ms=AT), "")[1]["banner_info"]["background_color"]
               for k in BannerKind}
    assert colours == {BannerKind.STALL: BANNER_RED, BannerKind.ASKS: BANNER_AMBER,
                       BannerKind.CROWN: BANNER_NEUTRAL, BannerKind.ENDED: BANNER_NEUTRAL}
    assert (BANNER_RED, BANNER_AMBER, BANNER_NEUTRAL) == ("#D24B4E", "#FFBC1F", "#DDE3EA")
    assert _banner_body(neutral(at_ms=AT), "")[1]["banner_info"]["enabled"] is True


def test_rt_6_6_the_cut_counts_bytes_and_keeps_head_crown_and_time():
    long = "a" * 64
    state = BannerState(BannerKind.CROWN, holder=long + "x" * 20, at_ms=AT,
                        counts=tuple((f"word{'é' * 200}{i}", i) for i in range(4)),
                        mode="coordinator", mandate="m" * 64)
    text = render_banner(state)
    assert len(text.encode()) <= 1024
    assert f"{CROWN} @{long} " in text and "x" not in text  # a name counts at 64 characters
    assert text.endswith(" · 14:16") and "mandate" not in text and "asks via" not in text
    small = render_banner(neutral(mode="coordinator", mandate="m" * 64, at_ms=AT))
    assert "mandate" in small  # nothing given way while it fits
    title = "t" * 200
    red = render_banner(BannerState(BannerKind.STALL, holder="agent-a", since_ms=SINCE, open=1,
                                    first="task open", title=title, next="agent-b", at_ms=AT))
    assert f'"{"t" * 79}…"' in red  # a title at 80 characters


def test_rt_6_6_the_marker_tells_the_bus_banner_from_a_human_one():
    raw = {"banner_info": {"enabled": True, "text": f"{BANNER_MARK} x", "background_color": "#fff"}}
    assert banner_of(raw) == Banner(enabled=True, text=f"{BANNER_MARK} x", marked=True)
    assert banner_of({"banner_info": {"enabled": True, "text": "release Friday"}}).marked is False
    assert banner_of({}).free and banner_of({"banner_info": {"enabled": False, "text": "x"}}).free


# --- the state from the stall check's look -------------------------------------------------

def _ask(n: int, human: str, by: str, asked: int, reminded: bool = True) -> dict:
    return {"what": "ask", "id": f"{n:026d}", "title": "Merge?", "next": f"{human} (human)", "at": asked,
            "human": human, "by": by, "asked": asked, **({"reminded": True} if reminded else {})}


ROSTER = [RosterEntry("agent-a", "working", coordinator=True, crown=True), RosterEntry("agent-b", "listening"),
          RosterEntry("agent-c", "silent"), RosterEntry("agent-x", "working", crown_of="other")]


def test_rt_6_6_state_priority_stall_then_asks_then_crown():
    now = AT / 1000
    asks = [_ask(2, "human-a", "agent-b", SINCE + 60000), _ask(1, "human-b", "agent-c", SINCE)]
    st = banner.state_of(asks, SINCE, ROSTER, "agent-a", now, 600)
    assert st.kind is BannerKind.ASKS and st.asks == 2 and st.humans == ("human-b", "human-a")
    assert (st.ask_id, st.asked_by, st.since_ms) == (f"{1:026d}", "agent-c", SINCE)
    task = {"what": "task open", "id": "t" * 26, "title": "fix CI", "next": "agent-b", "at": SINCE}
    st = banner.state_of([task, *asks], SINCE, ROSTER, "agent-a", now, 600)
    assert st.kind is BannerKind.STALL and (st.open, st.asks, st.first, st.next) == (3, 2, "task open", "agent-b")
    # Not quiet long enough yet: no stall, the asks show.
    assert banner.state_of([task, *asks], AT - 60000, ROSTER, "agent-a", now, 600).kind is BannerKind.ASKS
    st = banner.state_of([], 0, ROSTER, "agent-a", now, 600, mode="direct")
    assert st.kind is BannerKind.CROWN and st.mode == "" and st.mandate == ""
    # The holder counts itself, an ambassador of another home does not, silent only when one is.
    assert st.counts == (("working", 1), ("listening", 1), ("idle", 0), ("silent", 1))
    st = banner.state_of([], 0, ROSTER[:2], "agent-z", now, 600, mode="coordinator", mandate="team-b")
    assert st.counts == (("working", 2), ("listening", 1), ("idle", 0)) and st.mode == "coordinator"


def test_rt_6_6_never_a_question_to_the_crown_nor_a_permission_prompt():
    now = AT / 1000
    question = {"what": "question", "id": "q" * 26, "title": "secret?", "next": "dembaca", "at": SINCE,
                "human": "dembaca", "by": "agent-b", "asked": SINCE}
    st = banner.state_of([question], SINCE, ROSTER, "agent-a", now, 600)
    assert st.kind is BannerKind.CROWN and "secret" not in render_banner(st)
    # A permission prompt (a DM, kind permission) is no open item of the stall check.
    from test_stall import BOB, ME, post
    perm = post(1, BOB, "\U0001F64B @dembaca — **Bash: rm -rf x?**", 50, kind="permission")
    found = stall.items([perm], {ME: "agent-a", BOB: "agent-b"}, ["agent"], [], {"agent-b"}, int(now * 1000))
    assert found == []


# --- the writer ------------------------------------------------------------------------------

@pytest.fixture
def clock(monkeypatch):
    now = [1_800_000_000.0]
    monkeypatch.setattr(banner.time, "time", lambda: now[0])
    sleeps: list[float] = []

    async def sleep(sec: float) -> None:
        sleeps.append(sec)
        now[0] += sec

    monkeypatch.setattr(banner, "_sleep", sleep)
    held = {"on": True}
    monkeypatch.setattr(lease, "crown_held", lambda uid: held["on"])
    return type("Clock", (), {"now": now, "sleeps": sleeps, "held": held})


async def _settle() -> None:
    if banner._h.timer is not None:
        await banner._h.timer


async def test_rt_6_6_only_a_change_writes_and_the_gap_coalesces(clock):
    chat = FakeChat()
    await banner.show(chat, "u", neutral(), "agents")
    assert len(chat.banners) == 1 and chat.banner.text.startswith(BANNER_MARK)
    await banner.show(chat, "u", neutral())  # the same state: nothing
    assert len(chat.banners) == 1 and banner._h.timer is None
    clock.now[0] += 10
    await banner.show(chat, "u", neutral(mode="coordinator"))
    await banner.show(chat, "u", neutral(mandate="team-b"))  # inside the gap: only the latest counts
    assert len(chat.banners) == 1
    await _settle()
    assert [s.mandate for s, _ in chat.banners] == ["", "team-b"] and clock.sleeps == [50.0]
    assert chat.posted == []  # a banner write is no post: no quiet period starts


async def test_rt_6_6_a_pending_write_checks_the_crown_when_it_fires(clock):
    chat = FakeChat()
    await banner.show(chat, "u", neutral())
    await banner.show(chat, "u", neutral(mode="coordinator"))
    clock.held["on"] = False
    await _settle()
    assert len(chat.banners) == 1
    await banner.show(chat, "u", neutral(mandate="x"))  # no crown: no write
    assert len(chat.banners) == 1


async def test_rt_6_6_the_crown_passing_writes_again_also_when_two_settle(clock):
    chat = FakeChat()
    await banner.show(chat, "u", neutral())
    clock.now[0] += 100
    banner.crown_came()
    await banner.show(chat, "u", neutral())
    assert len(chat.banners) == 2
    clock.now[0] += 100
    lease.on_kept(banner.crown_kept)
    lease._kept("other-holder")
    lease._kept("other-holder")  # once per settling, not per tick
    await banner.show(chat, "u", neutral())
    await banner.show(chat, "u", neutral())
    assert len(chat.banners) == 3


async def test_rt_6_6_a_403_is_logged_once_and_backs_off(clock, caplog):
    chat = FakeChat()
    chat.banner_error = Refused("Mattermost refused the banner (403): license_error.feature_unavailable.specific",
                                status=403)
    with caplog.at_level(logging.DEBUG, logger="mattermost_ai_agent_bus.banner"):
        await banner.show(chat, "u", neutral())
        clock.now[0] += 3600
        await banner.show(chat, "u", neutral(mode="coordinator"))  # inside the 6 h: not even tried
        assert chat.banner_reads == 1
        clock.now[0] += 6 * 3600
        await banner.show(chat, "u", neutral(mandate="x"))
        assert chat.banner_reads == 2
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert "not written" in (banner.info() or "")
    chat.banner_error = None
    banner.crown_came()  # taking the crown again ends the back-off
    await banner.show(chat, "u", neutral())
    await _settle()  # at the gap's end after the last try
    assert len(chat.banners) == 1 and banner.info() is None


async def test_rt_6_6_another_failure_is_tried_with_the_next_change(clock):
    chat = FakeChat()
    chat.banner_error = OSError("timeout")
    await banner.show(chat, "u", neutral())
    chat.banner_error = None
    clock.now[0] += 120
    await banner.show(chat, "u", neutral())  # no change: no retry storm
    assert chat.banners == []
    await banner.show(chat, "u", neutral(mode="coordinator"))
    assert len(chat.banners) == 1


# --- a human's banner and the drift rule (ADR-013 points 1 and 4) ---------------------------

async def test_rt_6_6_a_human_banner_stops_the_writing_and_is_told_once(clock):
    chat = FakeChat()
    told: list[str] = []

    async def tell(text: str) -> None:
        told.append(text)

    banner.tell = tell
    try:
        chat.banner = Banner(enabled=True, text="Release freeze until Friday")
        await banner.show(chat, "u", neutral(), "agents")
        assert chat.banners == [] and banner.info() == "a human's own in ~agents"
        clock.now[0] += 120
        await banner.show(chat, "u", neutral(mode="coordinator"))
        await banner.observed(WORK, chat.banner)  # the same text again: told once
        for t in list(banner._telling):
            await t
        assert chat.banners == [] and len(told) == 1 and "~agents" in told[0]
        assert not any(c in told[0] for c in (symbols.symbol(Mark.HEADER), CROWN))  # plain words
        # The human turns it off: free, the bus writes at the gap's end.
        chat.banner = Banner(enabled=False, text="Release freeze until Friday")
        await banner.observed(WORK, chat.banner)
        await _settle()
        assert len(chat.banners) == 1 and banner.info() is None
    finally:
        banner.tell = None


async def test_rt_6_6_drift_own_write_ignored_a_changed_marker_rewritten(clock):
    chat = FakeChat()
    await banner.show(chat, "u", neutral(), "agents")
    await banner.observed(WORK, chat.banner)  # its own write coming back
    await banner.observed("o" * 26, Banner(enabled=False))  # another channel
    assert len(chat.banners) == 1 and banner._h.timer is None
    spoof = Banner(enabled=True, text=f"{BANNER_MARK} all fine", marked=True)
    chat.banner = spoof
    await banner.observed(WORK, spoof)
    await _settle()
    assert len(chat.banners) == 2 and clock.sleeps == [60.0]  # at most one gap
    clock.now[0] += 100
    chat.banner = Banner()
    await banner.observed(WORK, Banner())  # cleared: written again
    assert len(chat.banners) == 3
    # An event that came before the write's answer (its own text after all): read, nothing written.
    clock.now[0] += 100
    await banner.observed(WORK, Banner(enabled=True, text=f"{BANNER_MARK} older", marked=True))
    assert len(chat.banners) == 3 and not banner._h.drift


async def test_rt_6_6_after_a_restart_a_marker_banner_is_the_bus_own(clock):
    chat = FakeChat()
    chat.banner = Banner(enabled=True, text=f"{BANNER_MARK} stalled since 09:00", marked=True)
    await banner.show(chat, "u", neutral())
    assert len(chat.banners) == 1


async def test_rt_6_6_the_inbox_hands_channel_updated_to_the_banner():
    seen: list[tuple[str, dict]] = []

    async def on_channel(cid: str, raw: dict) -> None:
        seen.append((cid, raw))

    inbox = WebSocketInbox(client=FakeChat(), on_channel=on_channel)  # type: ignore[arg-type]
    channel = {"id": WORK, "banner_info": {"enabled": True, "text": "x"}}
    await inbox._on_channel_updated({"event": "channel_updated", "data": {"channel": json.dumps(channel)},
                                     "broadcast": {"channel_id": WORK}})
    assert seen == [(WORK, channel)]


# --- the clean end (RT-6.1 step 7, RT-6.18 step 7) -------------------------------------------

async def test_rt_6_6_the_clean_end_writes_no_coordinator_since(clock):
    chat = FakeChat()
    assert banner.end_sync(chat, "u") == "no banner of the bus"
    await banner.show(chat, "u", neutral())
    assert banner.end_sync(chat, "u") == "banner ended"
    assert chat.banners[-1][1].startswith(f"{BANNER_MARK} no coordinator since ")
    assert CROWN not in chat.banners[-1][1]
    clock.held["on"] = False
    assert banner.end_sync(chat, "u") == "no crown"


# --- the stall check feeds it ----------------------------------------------------------------

async def test_rt_6_6_the_stall_check_shows_an_ask_of_another_agent(monkeypatch, clock):
    from test_stall import BOB, ME, NOW, Client, post

    client = Client()
    client.banners: list[tuple[BannerState, str]] = []
    client.banner = Banner()
    client.work_channel_name = "agents"

    async def channel_banner(cid):
        return client.banner

    async def set_channel_banner(cid, state):
        text = render_banner(state, BASE)
        client.banners.append((state, text))
        return text

    async def roster(_client):
        return [RosterEntry("agent-coord", "listening", coordinator=True, crown=True),
                RosterEntry("agent-bob", "working")]

    client.channel_banner, client.set_channel_banner = channel_banner, set_channel_banner
    monkeypatch.setattr(lease, "roster", roster)
    monkeypatch.setattr(stall, "sink", lambda text: True)
    monkeypatch.setenv("MM_AGENT_PREFIX", "agent-")
    now = clock.now[0] = NOW
    client.posts = [post(1, BOB, "\U0001F64B @dembaca — **Merge PR #4?**", 5, kind="ask")]
    await stall.check(client, ME, now)
    state, text = client.banners[-1]
    assert state.kind is BannerKind.ASKS and state.humans == ("dembaca",) and state.asked_by == "agent-bob"
    assert "1 ask waits on @dembaca" in text

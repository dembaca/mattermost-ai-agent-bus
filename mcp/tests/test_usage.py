"""ADR-008 phase 1: context and limits of the agents in the chat. The tap's
endpoint (RT-6.17, POST /v1/usage), the bot's status text (RT-6.6 step 1),
its status text and the roster (RT-6.14 step 4); ADR-008 phase 3 in part: the
context line in the HELLO thread (RT-6.6 step 9)."""

import logging
import time

import httpx
import pytest

from mattermost_ai_agent_bus import a2a, hookapi, lease, ownpost, tasks, turnend, usage
from mattermost_ai_agent_bus.model import Refused
from mattermost_ai_agent_bus.model import RosterEntry

from fakes import FakeChat, HookClient, post_of

UID = "u" * 26
OTHER = "o" * 26
THIRD = "t" * 26
NOW = 1_800_000_000  # 08:00:00Z
RESET = NOW + 3 * 3600 + 40 * 60  # 11:40Z


@pytest.fixture
async def api():
    made = []

    async def serve(client):
        async def get():
            return client

        a = hookapi.HookAPI(get)
        await a.start(write_file=False)
        h = httpx.AsyncClient(base_url=a.endpoint, headers={"Authorization": f"Bearer {a.key}"})
        made.append((a, h))
        return h

    yield serve
    for a, h in made:
        await h.aclose()
        await a.close()


# --- the report: only numbers in range are kept (RT-6.17) ------------------------------

def test_a_report_keeps_the_valid_numbers_only():
    """RT-6.17 /v1/usage: invalid fields are ignored one by one."""
    got = usage.parse({
        "ctx": 62.46, "ctx_size": 200000,
        "five_hour": {"used": 40, "resets_at": NOW + 600},
        "seven_day": {"used": 140, "resets_at": "soon"}}, NOW)
    # A value above 100 is 100, not dropped: `5h` must not vanish at the limit.
    assert got == {"ctx": 62.5, "ctx_size": 200000, "five_hour": {"used": 40.0, "resets_at": NOW + 600},
                   "seven_day": {"used": 100.0}}
    bad = usage.parse({"ctx": -1, "ctx_size": 0, "five_hour": {"used": True, "resets_at": 5},
                       "seven_day": "x"}, NOW)
    assert bad == {}
    assert usage.parse({"ctx": float("nan"), "ctx_size": 1.5}, NOW) == {}
    assert usage.parse(["ctx"], NOW) == {} and usage.parse({"ctx": "50"}, NOW) == {}
    assert usage.parse({"ctx": 100, "five_hour": {"used": 0}}, NOW) == {"ctx": 100.0, "five_hour": {"used": 0.0}}


async def test_the_endpoint_keeps_the_last_report_and_logs_once(api, caplog):
    """RT-6.17 /v1/usage: 200 {ok}; the last report per session in memory;
    one log line whether rate_limits came (ADR-008 open question)."""
    h = await api(HookClient(uid=UID))
    caplog.set_level(logging.INFO, logger="mattermost_ai_agent_bus.usage")
    r = await h.post("/v1/usage", json={"ctx": 30, "ctx_size": 200000})
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert (await h.post("/v1/usage", json={"ctx": 31})).status_code == 200
    assert usage.last()["ctx"] == 31 and "at" in usage.last()
    assert [r.message for r in caplog.records if r.name == usage.log.name] == [
        "usage: no rate_limits (only ctx)"]
    # The limits come later: told once more, then no more.
    both = {"ctx": 32, "five_hour": {"used": 5, "resets_at": int(time.time()) + 600},
            "seven_day": {"used": 1, "resets_at": int(time.time()) + 6000}}
    for _ in range(2):
        await h.post("/v1/usage", json=both)
    told = [r.message for r in caplog.records if r.name == usage.log.name]
    assert told[1:] == ["usage: rate_limits five_hour+seven_day present"]


async def test_invalid_fields_never_fail_the_status_line(api):
    """RT-6.17 error handling: ignored, 200; a body that is no object is 400,
    no session 503 as for every endpoint."""
    h = await api(HookClient(uid=UID))
    r = await h.post("/v1/usage", json={"ctx": "much", "five_hour": 3, "ctx_size": -4})
    assert r.status_code == 200 and usage.last() is None
    assert (await h.post("/v1/usage", content=b"[1]")).status_code == 400
    nobot = HookClient(uid=UID)
    nobot.token = None
    h3 = await api(nobot)
    assert (await h3.post("/v1/usage", json={"ctx": 3})).status_code == 503
    assert usage.last() is None


# --- the status text (RT-6.6 step 1) ---------------------------------------------------

def _report(**kw):
    usage.record(kw, NOW)


def test_segments_between_label_and_home():
    """ADR-008 step 2: ctx and 5h always, 7d and the reset only from 90 %."""
    _report(ctx=62.4, five_hour={"used": 40, "resets_at": RESET}, seven_day={"used": 89.9, "resets_at": RESET})
    assert usage.segments(NOW) == ["ctx 62%", "5h 40%"]
    _report(ctx=62, five_hour={"used": 93, "resets_at": RESET}, seven_day={"used": 92, "resets_at": RESET})
    assert usage.segments(NOW) == ["ctx 62%", "5h 93% ↻11:40Z", "7d 92% ↻11:40Z"]
    # A window that has reset since is old news.
    assert usage.segments(RESET + 1) == ["ctx 62%"]


async def test_status_text_carries_the_numbers_with_the_home_last():
    """RT-6.6 step 1: working · bus · ctx 62% · 5h 40% · ~home; no report, no segments."""
    c = FakeChat()
    await lease.set_status(c, "working", "bus")
    assert c.status["text"] == "working · bus · ~agents"
    usage.record({"ctx": 62, "five_hour": {"used": 40, "resets_at": int(time.time()) + 600}})
    await lease.set_status(c, "working", "bus")
    assert c.status["text"] == "working · bus · ctx 62% · 5h 40% · ~agents"
    await lease.set_status(c, "listening", str(int(time.time()) + 600))
    assert c.status["text"].endswith(" · ctx 62% · 5h 40% · ~agents")
    assert lease.home_of(c.status["text"]) == "agents"


def test_the_cut_drops_the_label_then_5h_then_ctx_never_the_home():
    """ADR-008 step 2 / RT-6.6 step 1: 100 characters hold."""
    segs = ["ctx 62%", "5h 40%"]
    full = lease.status_text("working", "bus", segs, "home")
    assert full == "working · bus · ctx 62% · 5h 40% · ~home"
    # No room for the label.
    assert lease.status_text("working", "x" * 80, segs, "home") == "working · ctx 62% · 5h 40% · ~home"
    # No room for 5h either (head 80: with ctx 98, with 5h 107).
    assert lease.status_text("h" * 80, "bus", segs, "home") == "h" * 80 + " · ctx 62% · ~home"
    # None of them: the old cut of the text before the home.
    out = lease.status_text("h" * 95, "bus", segs, "home")
    assert out == "h" * 92 + " · ~home" and len(out) == lease.STATUS_MAX
    for head in ("h" * 70, "h" * 85, "h" * 93):
        out = lease.status_text(head, "l" * 30, ["ctx 99%", "5h 93% ↻21:40Z", "7d 92% ↻21:40Z"], "agents")
        assert len(out) <= lease.STATUS_MAX and out.endswith(" · ~agents")
    # Without segments: as before, the label is cut.
    assert lease.status_text("working", "l" * 120, [], "home") == lease.homed("working · " + "l" * 120, "home")


def test_old_and_new_status_texts_parse():
    """RT-6.6: a text without the segments (an older plugin) and one with them
    give the same home and status word; nothing reads the numbers."""
    for text in ("working · bus · ~proj", "working · bus · ctx 62% · 5h 93% ↻21:40Z · ~proj",
                 "coordinator · working · ctx 5% · ~proj"):
        assert lease.home_of(text) == "proj"
        row = {"username": "agent-a", "text": text, "emoji": "crown" if text.startswith("coord") else "x"}
        assert lease.entry(row).status == "working"
    assert lease.home_of("working · ctx 62%") is None


# --- bands and the heartbeat (RT-6.6 step 1) -------------------------------------------

class _Counting(FakeChat):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.writes = 0

    async def set_custom_status(self, emoji, text, expires_at):
        self.writes += 1
        await super().set_custom_status(emoji, text, expires_at)


async def test_a_band_crossing_writes_the_status_once_and_a_normal_beat_not_again(api):
    """ADR-008 step 2: the status carries the numbers with the lease on the beat, one more
    write when a band is crossed (ctx 50/70/85/95, a window 70/90/100); nothing else is written."""
    c = _Counting()
    await lease.set_status(c, "working", "bus")
    assert c.writes == 1
    # Inside the band: numbers move, nothing is written.
    usage.record({"ctx": 10})
    assert not usage.band_due()
    usage.record({"ctx": 49})
    assert not usage.band_due() and c.writes == 1
    # The report that crosses 50 %: one status write, at once.
    usage.record({"ctx": 52})
    assert usage.band_due()
    await lease.usage_crossed(c)
    assert c.writes == 2 and c.status["text"] == "working · bus · ctx 52% · ~agents"
    # More reports in the band, and a crossing within the gap: nothing more.
    usage.record({"ctx": 53})
    await lease.usage_crossed(c)
    usage.record({"ctx": 72})
    assert not usage.band_due()  # the gap since the last band write
    await lease.usage_crossed(c)
    assert c.writes == 2
    # The beat renews the status, with the numbers of then.
    await lease.command(c, "tick")
    assert c.writes == 3 and "ctx 72%" in c.status["text"]


async def test_the_endpoint_writes_at_a_band_only(api):
    """RT-6.17 /v1/usage: a report inside a band costs no Mattermost call."""
    c = _Counting(uid=UID)
    h = await api(c)
    await lease.set_status(c, "working", "bus")
    for ctx in (5, 6, 7):
        await h.post("/v1/usage", json={"ctx": ctx})
    assert c.writes == 1
    await h.post("/v1/usage", json={"ctx": 90})
    assert c.writes == 2


async def test_no_band_write_without_a_status():
    """An agent that is offline has no status to renew: the crossing is dropped."""
    c = _Counting()
    usage.record({"ctx": 99})
    await lease.usage_crossed(c)
    assert c.writes == 0


# --- the roster reads the status text (ADR-008 step 3, amended 2026-11-05) ---------------

def _rows(*names):
    return [RosterEntry(n, "working", user_id=i) for n, i in zip(names, (UID, OTHER, THIRD), strict=False)]


def test_a_status_text_gives_its_numbers_to_the_roster():
    """The segments segments() writes are read back, whatever the label and the crown say."""
    text = "coordinator · working · bus · ctx 62% · 5h 93% ↻11:40Z · $4.20 · ~team-a"
    got = usage.from_status(text, now=NOW)  # NOW is 08:00:00Z
    assert got == {"ctx": 62.0, "limit_5h": {"used": 93.0, "resets_at": NOW + 3 * 3600 + 40 * 60},
                   "cost_usd": 4.2}
    assert usage.from_status("working · bus · ctx 62% · 7d 92% ↻21:40Z · ~team-a", now=NOW) == {
        "ctx": 62.0, "limit_7d": {"used": 92.0}}
    assert usage.from_status("working · bus · ctx 5% · 5h 40% · ~team-a", now=NOW) == {
        "ctx": 5.0, "limit_5h": {"used": 40.0}}


def test_the_cut_text_gives_what_is_left_and_unknown_is_not_zero():
    """RT-6.6 step 1: the cut drops the label, `7d`, `5h`, `ctx` in that order; a missing segment is unknown."""
    assert usage.from_status("working · ctx 62% · ~team-a", now=NOW) == {"ctx": 62.0}
    assert usage.from_status("working · ~team-a", now=NOW) == {}
    assert usage.from_status("", now=NOW) == {}
    assert usage.from_status("working · bus · ctx 0% · ~a", now=NOW) == {"ctx": 0.0}  # a 0 is a number


def test_what_is_no_segment_or_out_of_range_is_ignored():
    for text in ("working · ctx 62 · ~a", "working · ctx 1000% · ~a", "working · $4 · ~a",
                 "working · 5h 40%x · ~a", "working · ctx -5% · ~a", "working · CTX 62% · ~a",
                 "listening · until 17:55, then idle · ~a"):
        assert usage.from_status(text, now=NOW) == {}, text
    assert usage.from_status("working · 5h 93% ↻25:99Z · ~a", now=NOW) == {"limit_5h": {"used": 93.0}}
    assert usage.from_status("working · ctx 150% · ~a", now=NOW) == {"ctx": 100.0}  # above 100 it is 100


def test_the_roster_row_carries_the_numbers_of_its_status_text():
    row = {"username": "agent-a", "emoji": "hammer_and_wrench", "id": OTHER,
           "text": "working · bus · ctx 62% · $4.20 · ~team-a", "coordinator": False, "crown": False}
    entry = lease.entry(row)
    assert entry.usage == {"ctx": 62.0, "cost_usd": 4.2} and entry.status == "working"
    rows = tasks.roster([entry], [], {}, usage={"agent-a": entry.usage})
    assert rows[0]["ctx"] == 62.0 and rows[0]["cost_usd"] == 4.2


async def test_the_roster_reads_others_from_their_status_and_the_own_row_from_memory():
    from mattermost_ai_agent_bus import server

    usage.reset()
    usage.record({"ctx": 11, "cost_usd": 3.0}, now=time.time())
    c = FakeChat(uid=UID)
    rows = [RosterEntry("agent-me", "working", user_id=UID, usage={"ctx": 99.0}),
            RosterEntry("agent-a", "working", user_id=OTHER, usage={"ctx": 22.0, "cost_usd": 5.0}),
            RosterEntry("agent-b", "working", user_id=THIRD)]
    got = await server._usage_of(c, rows)
    assert got["agent-me"] == {"ctx": 11.0, "cost_usd": 3.0}  # its own: exact, from memory
    assert got["agent-a"] == {"ctx": 22.0, "cost_usd": 5.0} and "agent-b" not in got
    usage.reset()


async def test_the_numbers_reach_the_roster_through_the_lease_status():
    """The whole way: report → status text (the lease) → what another agent's roster reads."""
    usage.reset()
    usage.record({"ctx": 33, "cost_usd": 7.5}, now=time.time())
    c = _Counting()
    await lease.set_status(c, "working", "bus")
    text = c.status["text"]
    assert "ctx 33%" in text and "$7.50" in text
    assert usage.from_status(text) == {"ctx": 33.0, "cost_usd": 7.5}
    usage.reset()


def test_the_usage_info_of_bus_info():
    usage.reset()
    assert usage.info(NOW) == {"report": None, "shown": ""}
    usage.record({"ctx": 5, "cost_usd": 1.0}, now=NOW)
    assert usage.info(NOW) == {"report": {"ctx": 5.0, "cost_usd": 1.0}, "shown": "ctx 5% · $1.00"}
    usage.reset()


def test_a_window_that_is_over_is_dropped_however_long_ago():
    """Review of #161: resets_at older than a day was dropped by itself and left
    `used` standing; the whole window goes, in a report and in the agent's own row."""
    old = NOW - 3 * 86400
    assert usage.parse({"five_hour": {"used": 95, "resets_at": old}, "ctx": 5}, NOW) == {"ctx": 5.0}
    usage.reset()
    usage.record({"ctx": 5, "five_hour": {"used": 95, "resets_at": NOW - 100},
                  "seven_day": {"used": 3, "resets_at": NOW + 600}}, now=NOW - 200)
    assert usage.own(NOW) == {"ctx": 5.0, "limit_7d": {"used": 3.0, "resets_at": NOW + 600}}
    usage.reset()
    assert usage.parse({"five_hour": {"used": 100.4}}, NOW) == {"five_hour": {"used": 100.0}}


def test_one_subscription_needs_the_same_owner_too():
    """Review of #161: a reset time may be rounded; agents of different humans are not one."""
    win = {"used": 30.0, "resets_at": 1_800_003_600}
    rows = _rows("agent-a", "agent-b", "agent-c")
    got = {n: {"limit_5h": win} for n in ("agent-a", "agent-b", "agent-c")}
    owners = {"agent-a": {"owner": "dembaca"}, "agent-b": {"owner": "dembaca"}, "agent-c": {"owner": "other"}}
    out = tasks.roster(rows, [], {}, owners, got)
    assert out[0]["limit_shared"] == out[1]["limit_shared"] == {"group": 1, "agents": 2}
    assert "limit_shared" not in out[2]


def test_one_five_hour_reset_is_one_subscription():
    """ADR-008 step 3: the same five_hour.resets_at (exact) gives the same
    limit_shared group and its size; another one is no group."""
    win = {"used": 30.0, "resets_at": 1_800_003_600}
    rows = _rows("agent-a", "agent-b", "agent-c")
    got = {"agent-a": {"limit_5h": win}, "agent-b": {"limit_5h": dict(win, used=31.0)},
           "agent-c": {"limit_5h": dict(win, resets_at=1_800_003_601)}}
    out = tasks.roster(rows, [], {}, None, got)
    assert out[0]["limit_shared"] == out[1]["limit_shared"] == {"group": 1, "agents": 2}
    assert "limit_shared" not in out[2]
    third = {"agent-a": {"limit_5h": win}, "agent-b": {"limit_5h": win}, "agent-c": {"limit_5h": win}}
    assert [r["limit_shared"] for r in tasks.roster(rows, [], {}, None, third)] == [{"group": 1, "agents": 3}] * 3


def test_the_cost_is_kept_in_cents_and_only_when_it_is_a_number_from_zero():
    now = float(NOW)
    assert usage.parse({"cost_usd": 4.2049}, now) == {"cost_usd": 4.2}
    assert usage.parse({"cost_usd": 0}, now) == {"cost_usd": 0.0}  # a 0 is a number
    for bad in (-0.01, True, "4.2", None, float("nan"), float("inf"), 2e6, [1]):
        assert "cost_usd" not in usage.parse({"ctx": 5, "cost_usd": bad}, now)


def test_the_cost_stands_in_the_place_of_5h_where_no_window_is_reported():
    usage.reset()
    usage.record({"ctx": 62, "cost_usd": 4.2}, now=NOW)
    assert usage.segments(NOW) == ["ctx 62%", "$4.20"]
    # A reported 5-hour window shows instead; the cost is then left out.
    usage.record({"ctx": 62, "cost_usd": 4.2, "five_hour": {"used": 40, "resets_at": RESET}}, now=NOW)
    assert usage.segments(NOW) == ["ctx 62%", "5h 40%"]
    usage.reset()


def test_the_status_text_keeps_the_cost_before_it_drops_ctx_and_never_the_home():
    """RT-6.6 step 1: the order of the cut (label, 7d, the 5h/cost segment, ctx) is as before."""
    usage.reset()
    usage.record({"ctx": 62, "cost_usd": 4.2}, now=NOW)
    text = lease.status_text("working", "bus", usage.segments(NOW), "team-a")
    assert text == "working · bus · ctx 62% · $4.20 · ~team-a"
    cut = lease.status_text("working", "x" * 90, usage.segments(NOW), "team-a")
    assert cut.endswith("· ~team-a") and len(cut) <= 100
    usage.reset()


def test_a_status_text_older_than_its_window_gives_no_reset():
    """A 5-hour window ends at most 5 h ahead: a later time of day is a text of yesterday's window."""
    assert usage.from_status("working · 5h 93% ↻07:50Z · ~a", now=NOW) == {  # 08:00Z now: 07:50 is past
        "limit_5h": {"used": 93.0}}
    assert usage.from_status("working · 5h 93% ↻12:30Z · ~a", now=NOW) == {  # 4.5 h ahead
        "limit_5h": {"used": 93.0, "resets_at": NOW + 4 * 3600 + 1800}}
    assert usage.from_status("working · 5h 93% ↻13:30Z · ~a", now=NOW) == {  # 5.5 h ahead: no
        "limit_5h": {"used": 93.0}}


def test_the_own_row_and_a_parsed_peer_of_one_subscription_are_grouped_together():
    """`limit_shared` counts exact equality of `resets_at`: the own row (a report, to the second)
    is cut to the minute the status text names."""
    usage.reset()
    reset = NOW + 3 * 3600 + 40 * 60 + 17  # 11:40:17Z
    usage.record({"ctx": 10, "five_hour": {"used": 93, "resets_at": reset}}, now=NOW)
    mine = usage.own(NOW)
    peer = usage.from_status("working · bus · ctx 20% · 5h 93% ↻11:40Z · ~a", now=NOW)
    assert mine["limit_5h"]["resets_at"] == peer["limit_5h"]["resets_at"]
    rows = [RosterEntry("agent-me", "working", user_id=UID), RosterEntry("agent-a", "working", user_id=OTHER)]
    out = tasks.roster(rows, [], {}, None, {"agent-me": mine, "agent-a": peer})
    assert out[0]["limit_shared"] == out[1]["limit_shared"] == {"group": 1, "agents": 2}
    usage.reset()


@pytest.mark.parametrize("report", [
    {"ctx": 62}, {"ctx": 0}, {"ctx": 62, "cost_usd": 4.2}, {"cost_usd": 0},
    {"ctx": 5, "five_hour": {"used": 40}},
    {"ctx": 5, "five_hour": {"used": 93, "resets_at": NOW + 3 * 3600 + 40 * 60}},
    {"ctx": 5, "seven_day": {"used": 92, "resets_at": NOW + 86400}},
    {"ctx": 95, "five_hour": {"used": 100, "resets_at": NOW + 600}, "seven_day": {"used": 91, "resets_at": NOW + 99999},
     "cost_usd": 123.456},
    {"five_hour": {"used": 70}, "seven_day": {"used": 30}},
])
def test_what_segments_writes_from_status_reads_back(report):
    """The writer (segments, RT-6.6 step 1) and the parser (from_status) are two literals: they agree,
    the percent to the whole number, the cost to the cent, the 5-hour reset to the minute."""
    usage.reset()
    usage.record(report, now=NOW)
    text = lease.status_text("working", "bus", usage.segments(NOW), "team-a")
    got = usage.from_status(text, now=NOW)
    shown = usage.own(NOW)
    for key in ("ctx",):
        if key in shown:
            assert got[key] == float(round(shown[key]))
    if "cost_usd" in shown:  # shown only where no 5-hour window is reported
        if (shown.get("limit_5h") or {}).get("used") is None:
            assert got["cost_usd"] == shown["cost_usd"]
        else:
            assert "cost_usd" not in got
    for key in ("limit_5h", "limit_7d"):
        window = shown.get(key)
        # `7d` shows only from 90 %, `5h` always: what is shown must be read back
        if window and (key == "limit_5h" or window["used"] >= 90):
            assert got[key]["used"] == float(round(window["used"]))
            if key == "limit_5h" and window["used"] >= 90 and "resets_at" in window:
                assert got[key]["resets_at"] == window["resets_at"]
        else:
            assert key not in got
    usage.reset()


async def test_a_label_cannot_carry_the_segment_separator(api):
    """RT-6.6 step 8: " · " in a label becomes a space, so a label cannot pass for a segment."""
    c = _Counting(uid=UID)
    h = await api(c)
    resp = await h.post("/v1/presence", json={"state": "working", "label": "proj · ctx 1% · x"})
    assert resp.status_code == 200
    assert "proj ctx 1% x" in c.status["text"] and " · ctx 1% ·" not in c.status["text"]
    assert usage.from_status(c.status["text"], now=NOW) == {}


# --- the context line in the HELLO thread (ADR-008 step 4, RT-6.6 step 9) -----------------

HELLO = "h" * 26
WORK_ID = "w" * 26


def _announced(monkeypatch):
    """An agent that posted its HELLO: the thread the line goes to (ownpost.py)."""
    monkeypatch.setenv("MM_BOT_USER_ID", UID)
    ownpost.record(UID, WORK_ID, post_of({"id": HELLO, "message": "HELLO · bus", "create_at": 1000}))


async def test_the_context_line_comes_once_per_window_and_again_after_a_compaction(api, monkeypatch):
    """RT-6.6 step 9: at 85 % one fyi line in the HELLO thread; not again until the
    context drops below 85 % (a compaction) and climbs again."""
    _announced(monkeypatch)
    c = FakeChat(uid=UID)
    h = await api(c)
    for ctx in (80, 84):
        await h.post("/v1/usage", json={"ctx": ctx})
    assert c.posted == []
    await h.post("/v1/usage", json={"ctx": 85})
    assert len(c.posted) == 1
    line = c.posted[0]
    assert line["message"] == "ctx 85% · compaction soon"
    assert line["root_id"] == HELLO and line["channel_id"] == WORK_ID
    assert line["props"]["a2a"]["kind"] == a2a.USAGE and line["props"]["a2a"]["intent"] == a2a.FYI
    for ctx in (90, 96, 99):
        await h.post("/v1/usage", json={"ctx": ctx})
    assert len(c.posted) == 1  # once per context window
    await h.post("/v1/usage", json={"ctx": 12})  # compacted: below 70 %, armed again
    assert len(c.posted) == 1
    await h.post("/v1/usage", json={"ctx": 88})
    assert [p["message"] for p in c.posted] == ["ctx 85% · compaction soon", "ctx 88% · compaction soon"]


async def test_a_jitter_around_the_threshold_posts_one_line(api, monkeypatch):
    """RT-6.6 step 9: only a report below CTX_REARM_BELOW (70 %) arms the next line."""
    _announced(monkeypatch)
    c = FakeChat(uid=UID)
    h = await api(c)
    for ctx in (85, 84, 86, 80, 71, 85, 90):
        await h.post("/v1/usage", json={"ctx": ctx})
    assert len(c.posted) == 1
    await h.post("/v1/usage", json={"ctx": 69})
    await h.post("/v1/usage", json={"ctx": 85})
    assert len(c.posted) == 2


async def test_a_restarted_server_posts_the_line_once_more(api, monkeypatch):
    """RT-6.6 step 9, error row: a restart forgets the line, so a context still at 85 %
    or more posts it once more (accepted)."""
    _announced(monkeypatch)
    c = FakeChat(uid=UID)
    h = await api(c)
    await h.post("/v1/usage", json={"ctx": 90})
    usage.reset()  # the server restarts: its memory is new
    await h.post("/v1/usage", json={"ctx": 91})
    await h.post("/v1/usage", json={"ctx": 92})
    assert [p["message"] for p in c.posted] == ["ctx 90% · compaction soon", "ctx 91% · compaction soon"]


async def test_a_4xx_ends_the_line_for_this_window(api, monkeypatch, caplog):
    """RT-6.6 step 9, error row: a 4xx comes back the same (the HELLO deleted, the channel
    archived): no retry on every report, logged once; a compaction arms the next."""
    _announced(monkeypatch)

    class Refusing(FakeChat):
        refuse = True

        async def post(self, message, **kw):
            self.tries = getattr(self, "tries", 0) + 1
            if self.refuse:
                raise Refused("Mattermost refused the post (403)", status=403)
            return await super().post(message, **kw)

    c = Refusing(uid=UID)
    h = await api(c)
    with caplog.at_level(logging.INFO, logger="mattermost_ai_agent_bus.lease"):
        for ctx in (86, 87, 88, 89):
            await h.post("/v1/usage", json={"ctx": ctx})
    assert c.tries == 1 and not usage.ctx_line_due()
    assert sum("ctx line not posted" in r.getMessage() for r in caplog.records) == 1
    c.refuse = False
    await h.post("/v1/usage", json={"ctx": 30})
    await h.post("/v1/usage", json={"ctx": 86})
    assert [p["message"] for p in c.posted] == ["ctx 86% · compaction soon"]


def test_another_agents_context_line_is_quiet_for_the_listener():
    """RT-6.6 step 9: /v1/posts marks an agent's context line `quiet` (only from an agent),
    and the listener's rules (/v1/wake) neither wake for it nor count it for a digest."""
    line = post_of({"id": "l" * 26, "user_id": OTHER, "message": "ctx 86% · compaction soon",
                    "root_id": HELLO, "create_at": 5, "props": a2a.props(kind=a2a.USAGE)})
    slim = hookapi._slim(line, {OTHER}, set(), set())
    assert slim["quiet"] is True and slim["fyi"] is True
    assert hookapi._slim(line, set(), set(), set())["quiet"] is False  # a human's props do not count
    recent = [{"id": HELLO, "user_id": UID, "root_id": ""}]  # even in a thread this agent started
    for coordinator in (False, True):
        got = turnend.wake([slim], recent, me=UID, username="agent-me", since_ms=0,
                           coordinator=coordinator, digest_sec=0, now=10)
        assert got["wake"] is False


async def test_no_context_line_for_a_runtime_that_reports_nothing(api, monkeypatch):
    """RT-6.6 step 9: no report, or one without `ctx` (only the cost), posts nothing."""
    _announced(monkeypatch)
    c = FakeChat(uid=UID)
    h = await api(c)
    await lease.ctx_line(c)
    await h.post("/v1/usage", json={"cost_usd": 3.5, "five_hour": {"used": 99}})
    assert c.posted == [] and not usage.ctx_line_due()


async def test_the_context_line_waits_for_the_hello_and_a_failed_post_tries_again(api, monkeypatch):
    """RT-6.6 step 9: without a HELLO thread nothing is posted and the line waits; a post
    Mattermost refuses is tried again with the next report."""
    monkeypatch.setenv("MM_BOT_USER_ID", UID)

    class Failing(FakeChat):
        fail = True

        async def post(self, message, **kw):
            if self.fail:
                raise RuntimeError("refused")
            return await super().post(message, **kw)

    c = Failing(uid=UID)
    h = await api(c)
    await h.post("/v1/usage", json={"ctx": 90})
    assert c.posted == [] and usage.ctx_line_due()  # not announced: still due
    _announced(monkeypatch)
    await h.post("/v1/usage", json={"ctx": 91})
    assert c.posted == [] and usage.ctx_line_due()  # refused: still due
    c.fail = False
    await h.post("/v1/usage", json={"ctx": 92})
    assert [p["message"] for p in c.posted] == ["ctx 92% · compaction soon"]


def test_the_context_line_is_no_post_of_the_agent():
    """RT-6.6 step 9: the server's line neither undoes a sign-off (ownpost.py) nor is
    taken as the agent's latest post."""
    ownpost.record(UID, WORK_ID, post_of({"id": "i" * 26, "message": "IDLE · done", "create_at": 4000}))
    ownpost.record(UID, WORK_ID, post_of({"id": "l" * 26, "message": "ctx 86% · compaction soon",
                                          "root_id": HELLO, "create_at": 5000,
                                          "props": a2a.props(kind=a2a.USAGE)}))
    assert ownpost.read(UID)["MSG"].startswith("IDLE")

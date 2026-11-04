"""ADR-008 phase 1: context and limits of the agents in the chat. The tap's
endpoint (RT-6.17, POST /v1/usage), the bot's status text (RT-6.6 step 1),
its props and the roster (RT-6.14 step 4)."""

import json
import logging
import time

import httpx
import pytest

from mattermost_ai_agent_bus import hookapi, lease, tasks, usage
from mattermost_ai_agent_bus.client import MattermostAdapter
from mattermost_ai_agent_bus.model import RosterEntry

from fakes import FakeChat, HookClient

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


# --- bands, the heartbeat and the props (RT-6.6, RT-6.24 step 3) ---------------------

class _Counting(FakeChat):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.writes = 0
        self.locked: list[bool] = []

    async def set_custom_status(self, emoji, text, expires_at):
        self.writes += 1
        await super().set_custom_status(emoji, text, expires_at)

    async def publish_usage(self, value):
        self.locked.append(lease.status_lock().locked())
        return await super().publish_usage(value)


async def test_a_band_crossing_writes_once_and_a_normal_beat_not_again(api):
    """ADR-008 steps 2 and 3: status and props with the lease on the beat, one
    more write when a band is crossed (ctx 50/70/85/95, a window 70/90/100)."""
    c = _Counting()
    c.props = {"agent_attestation": "tok", "customStatus": "{}"}
    await lease.set_status(c, "working", "bus")
    assert c.writes == 1
    # Inside the band: numbers move, nothing is written.
    usage.record({"ctx": 10})
    assert not usage.band_due()
    usage.record({"ctx": 49})
    assert not usage.band_due() and c.writes == 1
    # The report that crosses 50 %: one status write and the props, at once.
    usage.record({"ctx": 52})
    assert usage.band_due()
    await lease.usage_crossed(c)
    assert c.writes == 2 and c.status["text"] == "working · bus · ctx 52% · ~agents"
    assert len(c.usage_writes) == 1 and json.loads(c.usage_writes[0])["ctx"] == 52.0
    assert c.props["agent_attestation"] == "tok" and c.props["customStatus"] == "{}"
    assert c.locked == [True]
    # More reports in the band, and a crossing within the gap: nothing more.
    usage.record({"ctx": 53})
    await lease.usage_crossed(c)
    usage.record({"ctx": 72})
    assert not usage.band_due()  # the gap since the last band write
    await lease.usage_crossed(c)
    assert c.writes == 2 and len(c.usage_writes) == 1
    # The beat renews the status (one write, with the numbers) and leaves the props alone.
    await lease.command(c, "tick")
    assert c.writes == 3 and "ctx 72%" in c.status["text"]
    await lease.write_usage(c)
    assert len(c.usage_writes) == 1
    # A minute later the changed numbers go with the next beat's write.
    usage._props_at -= 61
    await lease.write_usage(c)
    assert len(c.usage_writes) == 2 and json.loads(c.usage_writes[1])["ctx"] == 72.0
    usage._props_at -= 61
    await lease.write_usage(c)  # nothing changed
    assert len(c.usage_writes) == 2


async def test_the_endpoint_writes_at_a_band_only(api):
    """RT-6.17 /v1/usage: a report inside a band costs no Mattermost call."""
    c = _Counting(uid=UID)
    h = await api(c)
    await lease.set_status(c, "working", "bus")
    for ctx in (5, 6, 7):
        await h.post("/v1/usage", json={"ctx": ctx})
    assert c.writes == 1 and c.usage_writes == []
    await h.post("/v1/usage", json={"ctx": 90})
    assert c.writes == 2 and len(c.usage_writes) == 1


async def test_no_band_write_without_a_status():
    """An agent that is offline has no status to renew: the crossing is dropped."""
    c = _Counting()
    usage.record({"ctx": 99})
    await lease.usage_crossed(c)
    assert c.writes == 0 and c.usage_writes == []


async def test_a_failing_props_write_is_tried_again_after_the_gap():
    c = _Counting()

    async def boom(value):
        raise RuntimeError("403")
    c.publish_usage = boom  # type: ignore[method-assign]
    usage.record({"ctx": 20})
    await lease.write_usage(c)  # never raises
    assert usage.props_due() == ""  # the gap holds
    usage._props_at -= 61
    assert usage.props_due() != ""


async def test_the_adapter_keeps_the_other_props_and_reads_usage_cached():
    """RT-6.24 step 3 as for the attestation: the user patch replaces the map,
    so the props are read first and sent back whole."""
    seen: list = []
    reads: list = []
    props = {"customStatus": "{}", "agent_attestation": "tok"}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v4/users/me":
            return httpx.Response(200, json={"id": UID, "username": "agent-me", "is_bot": True})
        if req.url.path == f"/api/v4/users/{UID}" and req.method == "GET":
            return httpx.Response(200, json={"id": UID, "username": "agent-me", "props": props})
        if req.url.path == f"/api/v4/users/{OTHER}" and req.method == "GET":
            reads.append(1)
            return httpx.Response(200, json={"id": OTHER, "props": {"agent_usage": '{"ctx":5.0}'}})
        if req.url.path == f"/api/v4/users/{UID}/patch":
            seen.append(json.loads(req.content))
            return httpx.Response(200, json={"id": UID})
        return httpx.Response(404)

    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await c.publish_usage('{"ctx":5.0}') is True
    assert seen[0]["props"] == {**props, "agent_usage": '{"ctx":5.0}'}
    props["agent_usage"] = '{"ctx":5.0}'
    assert await c.publish_usage('{"ctx":5.0}') is False and len(seen) == 1
    assert await c.agent_usage(OTHER) == '{"ctx":5.0}' and await c.agent_usage(OTHER) == '{"ctx":5.0}'
    assert len(reads) == 1  # one lookup, cached
    assert await c.agent_usage("not-an-id") is None


# --- the roster (RT-6.14 step 4) -------------------------------------------------------

def _rows(*names):
    return [RosterEntry(n, "working", user_id=i) for n, i in zip(names, (UID, OTHER, THIRD), strict=False)]


def _prop(**kw):
    return json.dumps({**kw, "at": NOW}, separators=(",", ":"))


async def test_the_roster_reads_the_props_not_the_status_text():
    """ADR-008 step 3: ctx, limit_5h, limit_7d from agent_usage; nothing for an agent without."""
    from mattermost_ai_agent_bus import server

    future = int(time.time()) + 3600
    c = FakeChat()
    # The first row is this agent itself (its own numbers are in memory, none here).
    c.usages = {OTHER: _prop(ctx=62.0, five_hour={"used": 40.0, "resets_at": future},
                             seven_day={"used": 12.5, "resets_at": future + 9999}),
                THIRD: "not json"}
    rows = _rows("agent-me", "agent-a", "agent-b")
    got = await server._usage_of(c, rows)
    assert got == {"agent-a": {"ctx": 62.0, "limit_5h": {"used": 40.0, "resets_at": future},
                               "limit_7d": {"used": 12.5, "resets_at": future + 9999}}}
    out = tasks.roster(rows, [], {}, None, got)
    assert out[1]["ctx"] == 62.0 and out[1]["limit_5h"]["used"] == 40.0
    assert "ctx" not in out[0] and "limit_5h" not in out[2] and "limit_shared" not in out[1]
    assert tasks.roster(rows, [], {}) == [{"agent": n, "status": "working"} for n in
                                          ("agent-me", "agent-a", "agent-b")]
    # A window that has reset since is not shown.
    c.usages = {OTHER: _prop(ctx=0, five_hour={"used": 99.0, "resets_at": int(time.time()) - 5})}
    assert await server._usage_of(c, rows) == {"agent-a": {"ctx": 0.0}}


def test_a_window_that_is_over_is_dropped_however_long_ago():
    """Review of #161: resets_at older than a day was dropped by itself and left
    `used` standing; the whole window goes, in a report and from the props."""
    old = NOW - 3 * 86400
    assert usage.parse({"five_hour": {"used": 95, "resets_at": old}, "ctx": 5}, NOW) == {"ctx": 5.0}
    raw = json.dumps({"ctx": 5, "five_hour": {"used": 95, "resets_at": old},
                      "seven_day": {"used": 3, "resets_at": NOW + 600}})
    assert usage.from_props(raw, NOW) == {"ctx": 5.0, "limit_7d": {"used": 3.0, "resets_at": NOW + 600}}
    assert usage.parse({"five_hour": {"used": 100.4}}, NOW) == {"five_hour": {"used": 100.0}}


async def test_the_own_row_comes_from_memory_and_the_others_from_one_lookup():
    """Review of #161: no lookup for the agent's own row."""
    from mattermost_ai_agent_bus import server

    usage.record({"ctx": 11}, now=time.time())
    c = FakeChat()
    c.usages = {OTHER: _prop(ctx=22.0)}
    looked: list[str] = []
    orig = c.agent_usage

    async def spy(user_id):
        looked.append(user_id)
        return await orig(user_id)

    c.agent_usage = spy
    got = await server._usage_of(c, _rows("agent-a", "agent-b"))
    assert got["agent-a"]["ctx"] == 11.0 and got["agent-b"]["ctx"] == 22.0
    assert looked == [OTHER]


async def test_the_description_read_carries_the_usage_so_the_roster_needs_one_get():
    """Review of #161: _owners_of and _usage_of read one user object per agent."""
    reads: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == f"/api/v4/users/{OTHER}" and req.method == "GET":
            reads.append(req.url.path)
            return httpx.Response(200, json={"id": OTHER, "username": "agent-b", "is_bot": True,
                                             "bot_description": "for @dembaca",
                                             "props": {"agent_usage": '{"ctx":7.0}'}})
        return httpx.Response(404)

    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await c.bot_description(OTHER) == "for @dembaca"
    assert await c.agent_usage(OTHER) == '{"ctx":7.0}'
    assert len(reads) == 1


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

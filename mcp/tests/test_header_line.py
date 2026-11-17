"""The channel header's bus line (RT-6.6 step 7) with the home's ask mode
(RT-6.25) and its mandate (RT-6.26): only the crown holder's server writes it,
only when it changes (every change posts a system message), and nothing is
ever decided from it."""

import time

import pytest

from mattermost_ai_agent_bus import askroute, lease, mandate, stall
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.model import Refused

from fakes import BOT, WORK, FakeChat

HUMAN = "h" * 26
SWITCH = "s" * 26
CROWN = "\U0001F451"
TODAY = "\U0001F68C agent bus · plain post → \U0001F451 routes it · @agent → that agent · " \
        "\U0001F64B = a question for you"
DAY_MS = mandate.DAY_MS
END = 1_796_083_200_000  # 2026-12-01T00:00Z


def _old_bus(name: str = "agent-old", at: str = "22:40") -> str:
    """The bus part before 0.65.4: it named the holder and the time."""
    return (f"\U0001F68C agent bus · {CROWN} @{name} since {at}\n"
            "plain post → \U0001F451 routes it · @agent → that agent · \U0001F64B = a question for you")


class _Chat(FakeChat):
    """FakeChat that counts header reads and writes."""

    def __init__(self) -> None:
        super().__init__(trusted={HUMAN: "dembaca"}, names={HUMAN: "dembaca", BOT: "agent-me"},
                         agents={BOT})
        self.reads = 0
        self.writes: list[str] = []

    async def channel_header(self, channel_id: str) -> str:
        self.reads += 1
        return self.header

    async def set_channel_header(self, channel_id: str, header: str) -> None:
        self.writes.append(header)
        self.header = header


@pytest.fixture
def holder(monkeypatch):
    """This agent holds its home's crown (lease.crown_held)."""
    held = {"now": True}
    monkeypatch.setattr(lease, "crown_held", lambda uid: held["now"])
    return held


def _mandate(home: str = "hub", end_ms: int = END) -> None:
    """A mandate read for the home (RT-6.26 step 1), as mandate.refresh keeps it."""
    m = mandate.Mandate(post="m" * 26, home=home, home_id="g" * 26, by="dembaca", by_id=HUMAN,
                        at_ms=end_ms - 7 * DAY_MS, until_ms=end_ms)
    mandate._reads[WORK] = mandate._Read(m, "")
    mandate._fetched[WORK] = time.time()


def _no_mandate() -> None:
    mandate._reads[WORK] = mandate._Read(None, "none pinned")
    mandate._fetched[WORK] = time.time()


def test_the_line_per_mode_and_mandate():
    """RT-6.6 step 7, RT-6.25, RT-6.26: the default is today's line byte for
    byte; the coordinator mode and a mandate (with its end, RT-6.26 step 3)
    are added after it."""
    assert lease.bus_line() == lease.BUS_HEADER == TODAY
    assert lease.bus_line(True) == f"{TODAY} · asks → {CROWN} first"
    assert lease.bus_line(mandate=("hub", END)) == f"{TODAY} · tasks here from {CROWN} of ~hub ends 2026-12-01 00:00Z"
    assert lease.bus_line(mandate=("hub", 0)) == f"{TODAY} · tasks here from {CROWN} of ~hub"
    assert lease.bus_line(True, ("hub", END)) == (
        f"{TODAY} · asks → {CROWN} first · tasks here from {CROWN} of ~hub ends 2026-12-01 00:00Z")


@pytest.mark.parametrize("old", [TODAY, _old_bus(), f"{TODAY} · asks → {CROWN} first",
                                 f"{TODAY} · tasks here from {CROWN} of ~hub ends 2026-11-20 00:00Z",
                                 f"{TODAY} · asks → {CROWN} first · tasks here from {CROWN} of ~x ends 2026-01-01 00:00Z"])
def test_every_older_form_is_replaced_and_peoples_text_stays(old):
    """RT-6.6 step 7: today's line, the pre-0.65.4 form and a line with any
    suffix are replaced where they stand; what people wrote before and after stays."""
    new = lease.bus_line(True, ("hub", END))
    assert lease.with_bus_header(f"Release on Fridays\n\n{old}\nmore notes", new) == (
        f"Release on Fridays\n\n{new}\nmore notes")
    assert lease.with_bus_header(f"Notes\n\n{new}", TODAY) == f"Notes\n\n{TODAY}"


def test_the_longest_line_stays_within_the_limit():
    """RT-6.6 step 7: past HEADER_MAX people's text is cut, never the bus part."""
    line = lease.bus_line(True, ("h" * 64, END))
    out = lease.with_bus_header("x" * 2000, line)
    assert len(out) == lease.HEADER_MAX and out.endswith(line) and "…\n\n" in out


async def test_the_default_writes_nothing_on_an_upgrade(holder):
    """RT-6.6 step 7: with the default mode and no mandate a header that has
    today's line gets no write, so an upgrade posts no system message."""
    c = _Chat()
    c.header = f"Notes\n\n{TODAY}"
    _no_mandate()
    await lease.header_tick(c, BOT)
    await lease.header_tick(c, BOT)
    assert c.writes == [] and c.reads == 1  # read once to compare, then not again


async def test_a_mode_change_on_the_heartbeat_writes_once(holder, monkeypatch):
    """RT-6.25 step 8 and RT-6.6 step 7: a changed mode (here MM_ASK_ROUTE, as
    a record read on the heartbeat would) makes the holder write the header
    once; an unchanged one reads nothing more."""
    c = _Chat()
    c.header = TODAY
    _no_mandate()
    await lease.header_tick(c, BOT)
    monkeypatch.setenv("MM_ASK_ROUTE", "coordinator")
    for _ in range(3):
        await lease.header_tick(c, BOT)
    assert c.writes == [f"{TODAY} · asks → {CROWN} first"] and c.reads == 2


async def test_a_non_holder_never_writes(holder, monkeypatch):
    """RT-6.6 step 7: only the home's crown holder writes the header."""
    holder["now"] = False
    c = _Chat()
    monkeypatch.setenv("MM_ASK_ROUTE", "coordinator")
    _mandate()
    await lease.header_tick(c, BOT)
    assert c.writes == [] and c.reads == 0


async def test_a_kept_switch_shows_at_once(holder):
    """RT-6.25 step 2 and RT-6.6 step 7: the holder's server that keeps a
    trusted human's switch writes the header right away; the header itself
    is never read for the mode."""
    c = _Chat()
    c.header = f"{TODAY} · asks → {CROWN} first"  # says coordinator: decides nothing
    _no_mandate()
    assert (await askroute.mode(c, BOT))[0] == askroute.DIRECT
    await lease.header_tick(c, BOT)
    assert c.header == TODAY
    switch = post_of({"id": SWITCH, "message": "ask-mode coordinator", "user_id": HUMAN,
                      "channel_id": WORK, "create_at": 1000})
    assert await askroute.observe(c, switch, BOT) is True
    assert c.writes == [TODAY, f"{TODAY} · asks → {CROWN} first"]


async def test_a_kept_switch_on_a_non_holder_writes_nothing(holder):
    """RT-6.6 step 7: a server without the crown keeps the switch, the header stays."""
    holder["now"] = False
    c = _Chat()
    switch = post_of({"id": SWITCH, "message": "ask-mode coordinator", "user_id": HUMAN,
                      "channel_id": WORK, "create_at": 1000})
    assert await askroute.observe(c, switch, BOT) is True
    assert c.writes == [] and c.reads == 0


async def test_a_mandate_shows_and_its_expiry_rewrites(holder):
    """RT-6.26 steps 1 and 3, RT-6.6 step 7: a mandate read for the home is
    shown with its end; once it expires the holder writes the line without it,
    once."""
    c = _Chat()
    c.header = f"Notes\n\n{TODAY}"
    _mandate(end_ms=END)
    await lease.header_tick(c, BOT, now=END / 1000 - 60)
    assert c.header == f"Notes\n\n{TODAY} · tasks here from {CROWN} of ~hub ends 2026-12-01 00:00Z"
    await lease.header_tick(c, BOT, now=END / 1000 - 30)
    assert len(c.writes) == 1
    await lease.header_tick(c, BOT, now=END / 1000 + 1)   # expired
    await lease.header_tick(c, BOT, now=END / 1000 + 60)
    assert c.header == f"Notes\n\n{TODAY}" and len(c.writes) == 2


async def test_the_heartbeat_runs_the_check(holder, monkeypatch):
    """RT-6.6 step 7: stall.watch, the heartbeat's watch, re-checks the header."""
    c = _Chat()
    c.header = TODAY
    _mandate(end_ms=int(time.time() * 1000) + DAY_MS)
    monkeypatch.setattr(lease, "_user_id", lambda client: _uid())
    await stall.watch(c)
    assert len(c.writes) == 1 and "tasks here from" in c.header


async def _uid() -> str:
    return BOT


async def test_a_refused_header_is_tried_again_only_on_a_change(holder, monkeypatch):
    """RT-6.6 step 7: without permission (403) the write fails open, and the
    next heartbeats read nothing until the line changes."""
    c = _Chat()

    async def refused(channel_id: str, header: str) -> None:
        raise Refused("Mattermost refused the header change (403)", status=403)

    monkeypatch.setattr(c, "set_channel_header", refused)
    _no_mandate()
    await lease.header_tick(c, BOT)
    await lease.header_tick(c, BOT)
    assert c.reads == 1
    monkeypatch.setenv("MM_ASK_ROUTE", "coordinator")
    await lease.header_tick(c, BOT)
    assert c.reads == 2


@pytest.mark.parametrize("failure", [Refused("Mattermost refused the header change (502)", status=502),
                                     ConnectionError("reset")])
async def test_a_transient_failure_is_tried_again_at_the_next_heartbeat(holder, monkeypatch, failure):
    """RT-6.6 step 7: a 5xx or a network error is not remembered as done: the
    next heartbeat writes the line."""
    c = _Chat()
    real = c.set_channel_header
    fails = [failure]

    async def flaky(channel_id: str, header: str) -> None:
        if fails:
            raise fails.pop()
        await real(channel_id, header)

    monkeypatch.setattr(c, "set_channel_header", flaky)
    monkeypatch.setenv("MM_ASK_ROUTE", "coordinator")
    _no_mandate()
    await lease.header_tick(c, BOT)
    assert c.writes == []
    await lease.header_tick(c, BOT)
    assert c.writes == [f"{TODAY} · asks → {CROWN} first"]
    await lease.header_tick(c, BOT)
    assert len(c.writes) == 1 and c.reads == 2  # done: no read again


async def test_a_change_without_the_crown_is_written_when_it_is_regained(holder, monkeypatch):
    """RT-6.6 step 7: the line changes while this server holds no crown; when
    the crown passes to it again, it writes the new line (_header)."""
    c = _Chat()
    c.header = TODAY
    _no_mandate()
    await lease.header_tick(c, BOT)
    holder["now"] = False
    monkeypatch.setenv("MM_ASK_ROUTE", "coordinator")
    await lease.header_tick(c, BOT)
    assert c.writes == [] and c.reads == 1
    holder["now"] = True
    await lease._header(c, WORK, BOT)  # the crown passes to it (take_now)
    assert c.writes == [f"{TODAY} · asks → {CROWN} first"]

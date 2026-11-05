"""The mandate from the registrar's signed `/agent-mandate` record (RT-6.26 step 8,
ADR-006 phase 2): the record, which source counts with and without pinned
keys, who may set it, `off`, a failed read, a replay, the refresh, the hint
under a pinned post, and what bus_info shows."""

import calendar
import json
import time

import httpx
import pytest

from mattermost_ai_agent_bus import attest, lease, mandate, server
from mattermost_ai_agent_bus.client import MattermostAdapter, post_of
from mattermost_ai_agent_bus.model import NoMandateEndpoint

from fakes import BOT, WORK
from test_ask_mode_signed import _key, _sign, _x, record as mode_record
from test_mandate import (GUEST, HOLDER, HUB, HUMAN, MPOST, NOW, NOW_MS, OTHER, _event,
                          _Homes, _items, _mandate, _post)
from mattermost_ai_agent_bus import gate

DAY = mandate.DAY_MS


def record(*, set_at=NOW_MS - 60_000, by=HUMAN, name="dembaca", channel=WORK, home=HUB,
           until="", note="", typ=attest.MANDATE_TYP, iat=None, **over) -> str:
    """A record as the registrar signs it, fresh (iat now, exp an hour later)."""
    iat = int(time.time()) if iat is None else iat
    claims = {"channel_id": channel, "home": home, "set_by": name, "set_by_id": by,
              "set_at_ms": set_at, "until": until, "note": note, "iat": iat,
              "exp": iat + 3600} | over
    header = {"alg": "EdDSA", "typ": typ, "kid": "Vkdap1RjR0wChd9d"}
    return _sign(json.dumps(header).encode(), json.dumps(claims).encode())


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setenv(attest.KEYS_ENV, _x(_key()))


@pytest.fixture
def chat(monkeypatch, pinned):
    c = _Homes()
    c.pinned = [_mandate()]      # a pinned post, which counts nothing now
    c.mandate_records[WORK] = record()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(gate, "_coord", None)
    monkeypatch.setattr(lease, "crown_held", lambda uid: False)
    return c


# --- the record (attest.verify_mandate) -------------------------------------------------

def test_a_mandate_record_verifies_and_is_strict():
    keys = attest.pinned(_x(_key()))
    got, why = attest.verify_mandate(record(until="2099-01-02", note="for #141"), keys,
                                     channel_id=WORK)
    assert why == "" and got is not None
    assert (got.home, got.set_by, got.set_by_id, got.until, got.note) == (
        HUB, "dembaca", HUMAN, "2099-01-02", "for #141")
    assert attest.verify_mandate(record(), keys, channel_id=HUB) == (None, "channel_id mismatch")
    # A mode record or an attestation is no mandate, and a mandate no mode.
    assert attest.verify_mandate(mode_record(), keys, channel_id=WORK)[0] is None
    assert attest.verify_mandate(record(typ=attest.TYP), keys, channel_id=WORK) == (
        None, "wrong typ")
    assert attest.verify_mode(record(), keys, channel_id=WORK)[0] is None
    old = int(time.time()) - 7200
    assert attest.verify_mandate(record(iat=old), keys, channel_id=WORK) == (None, "expired")


# The interop vector shared with the registrar (its Go test, mandate_test.go, has the
# same header and payload bytes): a public test seed, no credential.
VECTOR_HEADER = b'{"alg":"EdDSA","typ":"mm-agent-mandate+jwt","kid":"Vkdap1RjR0wChd9d"}'
VECTOR_PAYLOAD = (b'{"channel_id":"c0000000000000000000000000","home":"c1111111111111111111111111",'
                  b'"set_by":"alice","set_by_id":"u0000000000000000000000000",'
                  b'"set_at_ms":1699999000000,"until":"2023-12-01","note":"ship it",'
                  b'"iat":1700000000,"exp":1700003600}')


def test_the_interop_vector_holds():
    """RT-6.26 step 8: the registrar's vector, byte for byte, verifies here with the
    claim names it writes (`home`, not a crown)."""
    from test_ask_mode_signed import SEED
    keys = attest.pinned(_x(_key(SEED)))
    tok = _sign(VECTOR_HEADER, VECTOR_PAYLOAD, _key(SEED))
    got, why = attest.verify_mandate(tok, keys, channel_id="c0000000000000000000000000",
                                     now=1700000100)
    assert why == "" and got is not None
    assert (got.home, got.set_by, got.set_by_id, got.set_at_ms, got.until, got.note) == (
        "c1111111111111111111111111", "alice", "u0000000000000000000000000",
        1699999000000, "2023-12-01", "ship it")
    assert attest.verify_mandate(tok, keys, channel_id=WORK, now=1700000100) == (
        None, "channel_id mismatch")


@pytest.mark.parametrize("over", [
    {"home": "H" * 26}, {"home": "short"}, {"set_by_id": "u" * 25}, {"set_by": "no handle"},
    {"set_at_ms": 0}, {"until": "soon"}, {"until": 20990102}, {"note": "x" * 201},
    {"note": 5}, {"channel": "C" * 26},
])
def test_records_that_do_not_hold(over):
    keys = attest.pinned(_x(_key()))
    got, why = attest.verify_mandate(record(**over), keys, channel_id=WORK)
    assert got is None and why in ("bad claims", "channel_id mismatch")


def test_a_mandate_record_never_raises_and_needs_a_pinned_key():
    assert attest.verify_mandate(record(), {}, channel_id=WORK) == (None, "no pinned key")
    keys = attest.pinned(_x(_key()))
    for junk in (None, 7, "", "a.b", "a.b.c", "é.é.é"):
        assert attest.verify_mandate(junk, keys, channel_id=WORK)[0] is None


# --- which source counts ----------------------------------------------------------------

async def test_with_keys_pinned_the_record_is_the_mandate_and_a_pinned_post_counts_nothing(chat):
    """RT-6.26 step 8: the holder's TASK is marked from the record (`registrar-<set_at_ms>`,
    the human), and the home's pinned posts are not even read."""
    m, why = await mandate.current(chat, NOW)
    assert why == "" and m is not None
    assert (m.source, m.home, m.home_id, m.by, m.post) == (
        "registrar", "hub", HUB, "dembaca", f"registrar-{NOW_MS - 60_000}")
    [item] = await _items(_event(_post(HOLDER), "mention"))
    assert item["mandated"] == m.post and item["mandated_by"] == "dembaca"
    assert item["from_agent"] is True and item["from_trusted_human"] is False
    assert chat.pinned_reads == 0 and chat.fetched_mandates == [WORK]


async def test_without_a_record_a_pinned_post_mandates_nothing(chat):
    chat.mandate_records.clear()
    [item] = await _items(_event(_post(HOLDER), "mention"))
    assert "mandated" not in item
    m, why = await mandate.current(chat, NOW)
    assert m is None and "/agent-mandate" in why and chat.pinned_reads == 0


async def test_without_pinned_keys_the_post_holds_and_no_record_is_read(monkeypatch):
    """Phase 1 stays for installations without the registrar's keys."""
    monkeypatch.delenv(attest.KEYS_ENV, raising=False)
    c = _Homes()
    c.mandate_records[WORK] = record()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(gate, "_coord", None)
    m, _ = await mandate.current(c, NOW)
    assert m is not None and m.source == "post" and m.post == MPOST
    assert c.fetched_mandates == []


# --- who may set it, off, lifetime ------------------------------------------------------

async def test_a_record_counts_only_when_a_trusted_human_set_it(chat):
    chat.mandate_records[WORK] = record(by=OTHER, name="visitor")
    m, why = await mandate.current(chat, NOW)
    assert m is None and "not a trusted human" in why
    [item] = await _items(_event(_post(HOLDER), "mention"))
    assert "mandated" not in item


async def test_without_trusted_humans_no_record_counts(monkeypatch, pinned):
    c = _Homes(trusted={})
    c.mandate_records[WORK] = record()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    m, why = await mandate.current(c, NOW)
    assert m is None and why.startswith("no trusted humans")


async def test_off_ends_it_at_the_next_read_and_the_post_does_not_come_back(chat):
    assert (await mandate.current(chat, NOW))[0] is not None
    chat.mandate_records.clear()           # `/agent-mandate off`: the registrar answers 404
    assert await mandate.refresh(chat, NOW + 200) is True
    m, why = await mandate.current(chat, NOW + 200)
    assert m is None and "/agent-mandate" in why
    chat.pinned = [_mandate()]
    await mandate.refresh(chat, NOW + 400)
    assert (await mandate.current(chat, NOW + 400))[0] is None


async def test_the_default_until_and_the_cap_as_for_a_post(chat):
    m, _ = await mandate.current(chat, NOW)
    assert m is not None and m.until_ms == NOW_MS - 60_000 + 7 * DAY
    soon = time.strftime("%Y-%m-%d", time.gmtime(NOW + 86400))
    chat.mandate_records[WORK] = record(until=soon, note="for #141")
    await mandate.refresh(chat, NOW + 10)
    m, _ = await mandate.current(chat, NOW + 10)
    assert m is not None and m.note == "for #141"
    assert m.until_ms == calendar.timegm(time.strptime(soon, "%Y-%m-%d")) * 1000
    assert (await mandate.current(chat, m.until_ms / 1000 + 1))[0] is None
    chat.mandate_records[WORK] = record(until="2026-13-01")   # a shape of a date, no date
    await mandate.refresh(chat, NOW + 20)
    assert "no date" in (await mandate.current(chat, NOW + 20))[1]


async def test_a_failed_or_refused_read_keeps_the_last_good_one(chat):
    assert (await mandate.current(chat, NOW))[0] is not None
    chat.mandate_records[WORK] = RuntimeError("502 Bad Gateway")
    assert await mandate.refresh(chat, NOW + 200) is False
    chat.mandate_records[WORK] = "a.b.c"                     # does not verify
    assert await mandate.refresh(chat, NOW + 400) is False
    m, _ = await mandate.current(chat, NOW + 400)
    assert m is not None
    assert (await mandate.current(chat, m.until_ms / 1000 + 1))[0] is None   # to its expiry


async def test_an_older_record_is_a_replay_and_a_newer_one_replaces(chat):
    await mandate.current(chat, NOW)
    chat.mandate_records[WORK] = record(set_at=NOW_MS - 120_000)
    assert await mandate.refresh(chat, NOW + 200) is False
    assert (await mandate.current(chat, NOW + 200))[0].at_ms == NOW_MS - 60_000
    chat.mandate_records[WORK] = record(set_at=NOW_MS - 30_000, note="new")
    assert await mandate.refresh(chat, NOW + 400) is True
    assert (await mandate.current(chat, NOW + 400))[0].note == "new"


async def test_the_heartbeat_reads_the_record_every_120_s(chat):
    assert await mandate.tick(chat, BOT, NOW) is None
    await mandate.tick(chat, BOT, NOW + 60)
    assert len(chat.fetched_mandates) == 1
    await mandate.tick(chat, BOT, NOW + mandate.REFRESH_SEC)
    assert len(chat.fetched_mandates) == 2


async def test_a_foreign_holder_is_never_mandated_from_a_record_either(chat):
    chat.descriptions[HOLDER] = "for @carol · trusts @carol · via agent-registrar"
    [item] = await _items(_event(_post(HOLDER), "mention"))
    assert "mandated" not in item


# --- the hint under a pinned post ---------------------------------------------------------

def _pinned_post(text="mandate: :crown: of ~hub until 2099-01-02", **kw):
    return post_of({"id": MPOST, "user_id": HUMAN, "channel_id": WORK, "message": text,
                    "create_at": NOW_MS, **kw})


async def test_a_mandate_post_with_keys_pinned_gets_the_hint_once(chat, monkeypatch):
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    assert await mandate.observe(chat, _pinned_post(), BOT) is True
    [sent] = chat.posted
    assert sent["root_id"] == MPOST and sent["props"]["a2a"]["kind"] == "mandate"
    assert "/agent-mandate set ~hub until 2099-01-02" in sent["message"]
    assert "mandates nothing" in sent["message"]
    chat.posts = [_pinned_post(), post_of({"id": "r" * 26, "user_id": BOT, "channel_id": WORK,
                                           "root_id": MPOST, "message": "x",
                                           "props": {"a2a": {"kind": "mandate"}}})]
    assert await mandate.observe(chat, _pinned_post(), BOT) is True
    assert len(chat.posted) == 1                                 # a reply there already


async def test_other_servers_stay_quiet_and_other_posts_are_no_mandate_posts(chat, monkeypatch):
    assert await mandate.observe(chat, _pinned_post(), BOT) is True    # not the coordinator: no word
    assert chat.posted == []
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    for post in (_pinned_post(user_id=OTHER), _pinned_post(root_id="r" * 26),
                 _pinned_post(props={"from_webhook": "true"}), _pinned_post("lunch?"),
                 _pinned_post(channel_id=HUB), _pinned_post(type="system_header_change"),
                 _pinned_post(user_id=BOT)):
        assert await mandate.observe(chat, post, BOT) is False
    assert chat.posted == []


async def test_without_pinned_keys_the_post_is_left_alone(monkeypatch):
    monkeypatch.delenv(attest.KEYS_ENV, raising=False)
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    c = _Homes()
    assert await mandate.observe(c, _pinned_post(), BOT) is False
    assert c.posted == []


# --- bus_info ---------------------------------------------------------------------------

async def test_bus_info_names_the_source_and_the_note(chat, monkeypatch):
    chat.mandate_records[WORK] = record(note="for #141")
    got = await mandate.info(chat, NOW)
    assert got["source"] == "registrar" and got["note"] == "for #141"
    assert got["role"] == "coordinator of ~hub" and got["by"] == "dembaca"
    chat.mandate_records.clear()
    await mandate.refresh(chat, NOW + 200)
    assert "/agent-mandate" in (await mandate.info(chat, NOW + 200))["why"]


# --- the fetch (client.mandate_record) --------------------------------------------------

@pytest.mark.parametrize("register,url", [
    ("https://reg.example/register/v1/agents", f"https://reg.example/register/v1/mandates/{WORK}"),
    (None, f"https://mm.example/register/v1/mandates/{WORK}"),
])
async def test_the_fetch_finds_the_registrar_and_sends_no_token(register, url):
    c = MattermostAdapter(base_url="https://mm.example", token="t", register_url=register)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"record": "a.b.c"})
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await c.mandate_record(WORK) == "a.b.c"
    assert str(seen[0].url) == url and "authorization" not in seen[0].headers


async def test_only_the_registrars_own_404_means_none():
    """RT-6.26 step 8: a 404 without `{"error":"no mandate set"}` (a registrar
    before 0.6.0, a proxy) is a failed read, never `off`; other failures raise."""
    c = MattermostAdapter(base_url="https://mm.example", token="t")
    answers = iter([httpx.Response(404, json={"error": "no mandate set"}),
                    httpx.Response(404), httpx.Response(404, text="404 page not found"),
                    httpx.Response(404, json={"error": "no mode set"}),
                    httpx.Response(500),
                    httpx.Response(200, content=b'{"record":"' + b"x" * 9000 + b'"}'),
                    httpx.Response(200, json={"nothing": 1})])
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(answers)))
    assert await c.mandate_record(WORK) is None
    for _ in range(3):
        with pytest.raises(NoMandateEndpoint):
            await c.mandate_record(WORK)
    for _ in range(3):
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            await c.mandate_record(WORK)


async def test_a_registrar_without_the_endpoint_keeps_the_mandate_and_logs_once(chat, caplog):
    assert (await mandate.current(chat, NOW))[0] is not None
    chat.mandate_records[WORK] = NoMandateEndpoint("registrar has no mandate endpoint")
    with caplog.at_level("INFO"):
        assert await mandate.refresh(chat, NOW + 130) is False
        assert await mandate.refresh(chat, NOW + 260) is False
    assert caplog.text.count("no mandate endpoint") == 1
    assert (await mandate.current(chat, NOW + 260))[0] is not None


# --- a record that cannot be read again ---------------------------------------------------

async def test_a_failed_read_holds_only_a_few_refresh_periods(chat, caplog):
    """RT-6.26 step 8: the last good record counts for STALE_PERIODS refresh
    periods after the last read that went through, not to its end; a read that
    goes through renews it."""
    assert (await mandate.current(chat, NOW))[0] is not None
    chat.mandate_records[WORK] = RuntimeError("502 Bad Gateway")
    limit = mandate.STALE_PERIODS * mandate.REFRESH_SEC
    await mandate.refresh(chat, NOW + limit - 10)
    assert (await mandate.current(chat, NOW + limit - 5))[0] is not None
    with caplog.at_level("INFO"):
        m, why = await mandate.current(chat, NOW + limit + 5)
        await mandate.current(chat, NOW + limit + 6)
    assert m is None and "not read for" in why and caplog.text.count("not read for") == 1
    chat.mandate_records[WORK] = record()
    await mandate.refresh(chat, NOW + limit + 10)
    assert (await mandate.current(chat, NOW + limit + 20))[0] is not None


async def test_the_swallowed_post_is_logged_without_the_crown(chat, caplog):
    with caplog.at_level("INFO"):
        assert await mandate.observe(chat, _pinned_post(), BOT) is True
        assert await mandate.observe(chat, _pinned_post(), BOT) is True
    assert caplog.text.count("goes to no agent") == 1


# --- the holder's side: guests where a mandate names this home (RT-6.26 step 9) ---------

async def _guests(chat, *ids):
    async def guest_channel_ids():
        return set(ids)
    chat.guest_channel_ids = guest_channel_ids


async def test_a_guest_whose_mandate_names_this_home_is_held(chat):
    await _guests(chat, GUEST)
    chat.mandate_records[GUEST] = record(channel=GUEST, home=WORK)
    assert await mandate.held(chat, NOW) == [{"id": GUEST, "name": "proj-b"}]
    assert chat.fetched_mandates.count(GUEST) == 1


@pytest.mark.parametrize("rec", [
    lambda: record(channel=GUEST, home=HUB),                                     # another home's role
    lambda: record(channel=GUEST, home=WORK, by=OTHER, name="visitor"),          # not a trusted human
    lambda: record(channel=GUEST, home=WORK, until="2000-01-01"),                # ended
    lambda: None,                                                                 # off, or none set
    lambda: record(channel=WORK, home=WORK),                                     # signed for another channel
])
async def test_a_guest_without_a_mandate_for_this_home_is_not_held(chat, rec):
    await _guests(chat, GUEST)
    got = rec()
    if got is not None:
        chat.mandate_records[GUEST] = got
    assert await mandate.held(chat, NOW) == []


async def test_a_guest_is_read_again_only_after_the_refresh_period(chat):
    await _guests(chat, GUEST)
    chat.mandate_records[GUEST] = record(channel=GUEST, home=WORK)
    await mandate.held(chat, NOW)
    await mandate.held(chat, NOW + mandate.REFRESH_SEC - 1)
    assert chat.fetched_mandates.count(GUEST) == 1
    chat.mandate_records.pop(GUEST)               # `/agent-mandate off` there
    assert await mandate.held(chat, NOW + mandate.REFRESH_SEC + 1) == []
    assert chat.fetched_mandates.count(GUEST) == 2


async def test_no_guest_channels_known_means_none_held(chat):
    assert await mandate.held(chat, NOW) == []
    assert chat.fetched_mandates == []


async def test_bus_info_names_the_held_guests_only_with_the_crown(chat, monkeypatch):
    await _guests(chat, GUEST)
    chat.mandate_records[GUEST] = record(channel=GUEST, home=WORK)
    assert await mandate.holds(chat, BOT, NOW) == []
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    assert await mandate.holds(chat, BOT, NOW) == ["~proj-b"]

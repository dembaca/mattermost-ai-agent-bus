"""The ask mode from the registrar's signed `/agent-ask-mode` record (RT-6.25 step
8, ADR-005 phase 2): the record and its interop vector, which source counts
with and without pinned keys, who may set it, the refresh, the 👑's one
confirmation (also of a switch kept while no server held the crown), and
what bus_info shows."""

import base64
import hashlib
import json
import logging
import time

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from mattermost_ai_agent_bus import askroute, attest, lease, server, session_file
from mattermost_ai_agent_bus.client import MattermostAdapter, post_of

from fakes import WORK, FakeChat

BOT = "b" * 26
HUMAN = "h" * 26   # a trusted human, dembaca
OTHER = "o" * 26   # a human this session does not trust
SWITCH = "s" * 26

# The interop vector shared with the registrar (its Go test checks the same
# prefix): a public test seed, no credential.
SEED = bytes(range(32))
VECTOR_HEADER = b'{"alg":"EdDSA","typ":"mm-agent-mode+jwt","kid":"Vkdap1RjR0wChd9d"}'
VECTOR_PAYLOAD = (b'{"channel_id":"c0000000000000000000000000","mode":"coordinator",'
                  b'"set_by":"alice","set_by_id":"u0000000000000000000000000",'
                  b'"set_at_ms":1699999000000,"iat":1700000000,"exp":1700003600}')
VECTOR_SHA256 = "8663e8575e142e7c"
VCHANNEL = "c0000000000000000000000000"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _key(seed: bytes = SEED) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(seed)


def _x(k: Ed25519PrivateKey) -> str:
    return _b64(k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def _sign(header: bytes, payload: bytes, k: Ed25519PrivateKey | None = None) -> str:
    signing = f"{_b64(header)}.{_b64(payload)}"
    return f"{signing}.{_b64((k or _key()).sign(signing.encode()))}"


def record(mode="coordinator", *, set_at=1_000, by=HUMAN, name="dembaca", channel=WORK,
           typ=attest.MODE_TYP, iat=None, **over) -> str:
    """A record as the registrar signs it, fresh (iat now, exp an hour later)."""
    iat = int(time.time()) if iat is None else iat
    claims = {"channel_id": channel, "mode": mode, "set_by": name, "set_by_id": by,
              "set_at_ms": set_at, "iat": iat, "exp": iat + 3600} | over
    header = {"alg": "EdDSA", "typ": typ, "kid": "Vkdap1RjR0wChd9d"}
    return _sign(json.dumps(header).encode(), json.dumps(claims).encode())


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class _Client(FakeChat):
    def __init__(self, **kw):
        super().__init__(uid=BOT, username="agent-worker", trusted={HUMAN: "dembaca"},
                         names={HUMAN: "dembaca", OTHER: "visitor", BOT: "agent-worker"},
                         agents={BOT}, **kw)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for v in ("MM_ASK_ROUTE", "MM_ASK_ROUTE_WAIT_MIN"):
        monkeypatch.delenv(v, raising=False)
    for kept in (askroute._looked, askroute._warned, askroute._fetched, askroute._noted):
        kept.clear()
    monkeypatch.setattr(lease, "crown_held", lambda uid: False)


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setenv(attest.KEYS_ENV, _x(_key()))


def _kept() -> str:
    return (session_file.state_dir() / f"askmode-{BOT}.env").read_text()


def _switch(text="ask-mode coordinator", *, user=HUMAN, at=1000, pid=SWITCH):
    return post_of({"id": pid, "message": text, "user_id": user, "channel_id": WORK,
                    "create_at": at})


# --- the record (attest.verify_mode) ----------------------------------------------------

def test_the_interop_vector_holds():
    """RT-6.25 step 8: the registrar's vector, the same bytes as its Go test."""
    k = _key()
    assert attest.kid_of(base64.urlsafe_b64decode(_x(k) + "=")) == "Vkdap1RjR0wChd9d"
    tok = _sign(VECTOR_HEADER, VECTOR_PAYLOAD, k)
    assert _sha(tok).startswith(VECTOR_SHA256)
    keys = attest.pinned(_x(k))
    got, why = attest.verify_mode(tok, keys, channel_id=VCHANNEL, now=1700000100)
    assert why == "" and got is not None
    assert (got.mode, got.set_by, got.set_by_id, got.set_at_ms) == (
        "coordinator", "alice", "u0000000000000000000000000", 1699999000000)
    assert attest.verify_mode(tok, keys, channel_id=WORK, now=1700000100) == (
        None, "channel_id mismatch")
    late = 1700003600 + attest.SKEW_SEC
    assert attest.verify_mode(tok, keys, channel_id=VCHANNEL, now=late) == (None, "expired")
    as_attestation = VECTOR_HEADER.replace(b"mm-agent-mode+jwt", b"mm-agent-attestation+jwt")
    assert attest.verify_mode(_sign(as_attestation, VECTOR_PAYLOAD, k), keys,
                              channel_id=VCHANNEL, now=1700000100) == (None, "wrong typ")
    # And an attestation check refuses a mode record.
    assert attest.verify(tok, keys, bot_id="x", username="y", now=1700000100) == (None, "wrong typ")


@pytest.mark.parametrize("over,why", [
    ({"mode": "delegated"}, "unsupported mode"),
    ({"mode": 1}, "bad claims"),
    ({"channel_id": "C" * 26}, "bad claims"),
    ({"set_by_id": "u" * 25}, "bad claims"),
    ({"set_by": "not a handle"}, "bad claims"),
    ({"set_at_ms": 0}, "bad claims"),
    ({"set_at_ms": True}, "bad claims"),
    ({"exp": 1}, "bad claims"),
])
def test_records_that_do_not_hold(over, why):
    keys = attest.pinned(_x(_key()))
    assert attest.verify_mode(record(**over), keys, channel_id=WORK) == (None, why)


def test_a_record_never_raises_and_needs_a_pinned_key():
    assert attest.verify_mode(record(), {}, channel_id=WORK) == (None, "no pinned key")
    keys = attest.pinned(_x(_key()))
    for junk in (None, 7, "", "a.b", "a.b.c", "é.é.é"):
        assert attest.verify_mode(junk, keys, channel_id=WORK)[0] is None
    other = attest.pinned(_x(_key(bytes(32))))
    assert attest.verify_mode(record(), other, channel_id=WORK) == (None, "key not pinned")


# --- the fetch (client.ask_mode_record) -------------------------------------------------

@pytest.mark.parametrize("register,url", [
    ("https://reg.example/register/v1/agents", f"https://reg.example/register/v1/modes/{WORK}"),
    (None, f"https://mm.example/register/v1/modes/{WORK}"),
])
async def test_the_fetch_finds_the_registrar_like_the_keys_script(register, url):
    c = MattermostAdapter(base_url="https://mm.example", token="t", register_url=register)
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"record": "a.b.c"})
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await c.ask_mode_record(WORK) == "a.b.c"
    assert str(seen[0].url) == url
    assert "authorization" not in seen[0].headers  # the bot's token never goes there


async def test_the_fetch_404_is_none_and_the_rest_raises():
    c = MattermostAdapter(base_url="https://mm.example", token="t")
    answers = iter([httpx.Response(404), httpx.Response(500),
                    httpx.Response(200, content=b'{"record":"' + b"x" * 9000 + b'"}'),
                    httpx.Response(200, json={"nothing": 1})])
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: next(answers)))
    assert await c.ask_mode_record(WORK) is None
    for _ in range(3):
        with pytest.raises((httpx.HTTPStatusError, ValueError)):
            await c.ask_mode_record(WORK)


# --- which source counts ----------------------------------------------------------------

async def test_without_keys_nothing_is_fetched_and_phase_1_holds():
    c = _Client()
    c.mode_records[WORK] = record()
    assert await askroute.observe(c, _switch(), BOT) is True
    assert await askroute.mode(c, BOT) == ("coordinator", SWITCH)
    assert c.fetched == []
    assert "SOURCE=post" in _kept() and "SET_BY=dembaca" in _kept()


async def test_with_keys_only_the_signed_record_counts(pinned):
    """A trusted human's post neither switches nor counts once stored; the
    home is not read for one; the record sets the mode."""
    c = _Client(posts=[_switch()])
    assert await askroute.mode(c, BOT) == ("direct", "env")  # 404: the default
    assert c.calls == [] and c.fetched == [WORK]
    assert await askroute.observe(c, _switch(at=2000), BOT) is True  # to no agent's model
    assert await askroute.mode(c, BOT) == ("direct", "env")
    c.mode_records[WORK] = record(set_at=3000)
    await askroute.refresh(c, BOT, WORK)
    assert await askroute.mode(c, BOT) == ("coordinator", "registrar")
    kept = _kept()
    assert "SOURCE=registrar" in kept and "AT=3000" in kept and "SET_BY=dembaca" in kept


async def test_a_stored_phase_1_switch_is_ignored_once_keys_are_pinned(monkeypatch):
    c = _Client()
    await askroute.observe(c, _switch(), BOT)
    assert (await askroute.mode(c, BOT))[0] == "coordinator"
    monkeypatch.setenv(attest.KEYS_ENV, _x(_key()))
    assert await askroute.mode(c, BOT) == ("direct", "env")


async def test_a_record_by_someone_not_trusted_is_ignored_and_logged_once(pinned, caplog):
    c = _Client()
    c.mode_records[WORK] = record("direct", set_at=1000)
    await askroute.refresh(c, BOT, WORK)
    c.mode_records[WORK] = record("coordinator", set_at=2000, by=OTHER, name="visitor")
    with caplog.at_level(logging.INFO, logger="mattermost_ai_agent_bus.askroute"):
        for _ in range(3):
            assert await askroute.refresh(c, BOT, WORK) is False
    assert sum("not a trusted human" in r.getMessage() for r in caplog.records) == 1
    assert await askroute.mode(c, BOT) == ("direct", "registrar")  # the previous one stays


async def test_an_older_record_is_ignored(pinned):
    c = _Client()
    c.mode_records[WORK] = record("coordinator", set_at=2000)
    assert await askroute.refresh(c, BOT, WORK) is True
    c.mode_records[WORK] = record("direct", set_at=1000)
    assert await askroute.refresh(c, BOT, WORK) is False
    assert (await askroute.mode(c, BOT))[0] == "coordinator"
    c.mode_records[WORK] = record("direct", set_at=3000)
    assert await askroute.refresh(c, BOT, WORK) is True
    assert (await askroute.mode(c, BOT))[0] == "direct"


@pytest.mark.parametrize("answer", [RuntimeError("registrar down"), None, "not.a.jws",
                                    "delegated"])
async def test_a_failed_fetch_404_or_bad_record_keeps_the_stored_one(pinned, answer):
    c = _Client()
    c.mode_records[WORK] = record(set_at=2000)
    await askroute.refresh(c, BOT, WORK)
    if answer is None:
        del c.mode_records[WORK]
    else:
        c.mode_records[WORK] = record("delegated", set_at=9000) if answer == "delegated" else answer
    assert await askroute.refresh(c, BOT, WORK) is False
    assert await askroute.mode(c, BOT) == ("coordinator", "registrar")


async def test_the_refresh_cadence(pinned):
    """The first mode() of a process fetches, then the heartbeat every
    REFRESH_SEC; a failed fetch is tried again at the next cadence."""
    c = _Client()
    c.mode_records[WORK] = RuntimeError("down")
    await askroute.mode(c, BOT)
    await askroute.mode(c, BOT)
    assert c.fetched == [WORK]
    now = time.time()
    await askroute.tick(c, BOT, now + 60)
    assert len(c.fetched) == 1
    c.mode_records[WORK] = record()
    await askroute.tick(c, BOT, now + askroute.REFRESH_SEC + 1)
    assert len(c.fetched) == 2
    assert (await askroute.mode(c, BOT))[0] == "coordinator"


# --- the 👑's confirmation ---------------------------------------------------------------

async def test_the_crown_confirms_a_new_record_once(pinned, monkeypatch):
    c = _Client()
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    c.mode_records[WORK] = record(set_at=int(time.time() * 1000))
    await askroute.mode(c, BOT)
    assert len(c.posted) == 1
    got = c.posted[0]
    assert got["root_id"] is None and got["channel_id"] == WORK
    assert got["message"].startswith("\U0001F451 ask-mode coordinator, set by dembaca with /agent-ask-mode "
                                     "(signed by the registrar):")
    assert got["props"]["a2a"]["kind"] == "ask_mode"
    kept_at = int(_kept().split("AT=")[1].split()[0])
    c.mode_records[WORK] = record("direct", set_at=kept_at - 1)  # older: no word
    await askroute.refresh(c, BOT, WORK)
    askroute._fetched.clear()  # a restart: the same record, signed afresh, already kept
    c.mode_records[WORK] = record(set_at=kept_at)
    await askroute.mode(c, BOT)
    await askroute.tick(c, BOT)
    assert len(c.posted) == 1


async def test_other_servers_keep_the_record_quietly(pinned):
    c = _Client()
    c.mode_records[WORK] = record()
    assert await askroute.refresh(c, BOT, WORK) is True
    assert c.posted == []


async def test_a_command_post_with_keys_pinned_gets_the_hint_once(pinned, monkeypatch):
    c = _Client()
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    post = _switch()
    assert await askroute.observe(c, post, BOT) is True
    assert len(c.posted) == 1 and c.posted[0]["root_id"] == SWITCH
    assert "`/agent-ask-mode coordinator`, signed by the registrar, so this post changes nothing" in (
        c.posted[0]["message"])
    c.posts = [post, post_of({"id": "r" * 26, "root_id": SWITCH, "user_id": BOT,
                              "message": c.posted[0]["message"],
                              "props": c.posted[0]["props"]})]
    assert await askroute.observe(c, post, BOT) is True
    assert len(c.posted) == 1
    assert not (session_file.state_dir() / f"askmode-{BOT}.env").exists()


async def test_a_switch_kept_without_a_crown_is_confirmed_by_the_next_holder(monkeypatch):
    """Seen live: the switch came while no server held the crown; the server
    that takes it confirms it on the heartbeat, once."""
    c = _Client()
    post = _switch()
    c.posts = [post]
    assert await askroute.observe(c, post, BOT) is True
    await askroute.tick(c, BOT)
    assert c.posted == []
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    await askroute.tick(c, BOT)
    assert len(c.posted) == 1 and c.posted[0]["root_id"] == SWITCH
    assert c.posted[0]["message"].startswith("\U0001F451 ask-mode coordinator, set by dembaca:")
    assert "CONFIRMED=1" in _kept()
    await askroute.tick(c, BOT)
    assert len(c.posted) == 1


async def test_a_new_holder_finds_the_confirmation_already_there(monkeypatch):
    c = _Client()
    post = _switch()
    done = post_of({"id": "d" * 26, "root_id": SWITCH, "user_id": "k" * 26,
                    "message": "\U0001F451 ask-mode coordinator, set by dembaca: …",
                    "props": {"a2a": {"kind": "ask_mode"}}})
    c.posts = [post, done]
    await askroute.observe(c, post, BOT)
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    await askroute.tick(c, BOT)
    assert c.posted == [] and "CONFIRMED=1" in _kept()


async def test_a_record_kept_without_a_crown_is_confirmed_by_the_next_holder(pinned, monkeypatch):
    c = _Client()
    at = int(time.time() * 1000)
    c.mode_records[WORK] = record(set_at=at)
    await askroute.mode(c, BOT)
    assert c.posted == []
    monkeypatch.setattr(lease, "crown_held", lambda uid: True)
    await askroute.tick(c, BOT)
    assert len(c.posted) == 1 and c.posted[0]["root_id"] is None
    # Another holder later: the confirmation is in the home already.
    c.posts = [post_of({"id": "d" * 26, "user_id": "k" * 26, "channel_id": WORK,
                        "create_at": at + 5, "message": c.posted[0]["message"],
                        "props": c.posted[0]["props"]})]
    (session_file.state_dir() / f"askmode-{BOT}.env").write_text(
        _kept().replace("CONFIRMED=1\n", ""))
    await askroute.tick(c, BOT)
    assert len(c.posted) == 1 and "CONFIRMED=1" in _kept()


# --- bus_info ---------------------------------------------------------------------------

async def test_bus_info_shows_the_signed_source(pinned, monkeypatch):
    class _Info(_Client):
        trusted_source = "MM_TRUSTED_HUMANS"
        channel_name = "agents"

        async def username(self, uid):
            return self.names.get(uid, "")

    async def none(client):
        return []
    c = _Info()
    monkeypatch.setattr(server, "_get_client", lambda: c)
    monkeypatch.setattr(server, "_guest_names", none)
    monkeypatch.setattr(server, "_own_channels", none)
    monkeypatch.setattr(server, "_newer_installed", lambda: None)
    c.mode_records[WORK] = record()
    assert json.loads(await server.bus_info())["ask_mode"] == {
        "mode": "coordinator", "source": "registrar", "set_by": "dembaca", "signed": True}

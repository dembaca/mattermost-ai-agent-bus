"""RT-6.24 owner attestation: the registrar's signed claim, verified against
pinned keys, checked for the own session, published on the bot's account and
used for other agents' owners (ADR-003).

Tokens are never printed: the tests compare hashes, lengths and booleans.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import logging
import os

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from fakes import BOT, FakeChat

from mattermost_ai_agent_bus import a2a, attest, hosted, owners, server, session_file
from mattermost_ai_agent_bus.client import MattermostAdapter, SessionInfo, post_of
from mattermost_ai_agent_bus.model import NoAttestationEndpoint

# The interop vector shared with the registrar: a public test seed, no credential.
SEED = bytes(range(32))
VECTOR_HEADER = b'{"alg":"EdDSA","typ":"mm-agent-attestation+jwt","kid":"Vkdap1RjR0wChd9d"}'
VECTOR_PAYLOAD = (b'{"bot_id":"abcdefghijklmnopqrstuvwxyz","username":"agent-test-abc123",'
                  b'"responsible":"alice","trusted":["alice","bob"],"kind":"session",'
                  b'"via":"enrollment","iat":1798761600,"exp":1801353600}')
IAT, EXP = 1798761600, 1801353600
NOW = IAT + 3600
VBOT, VNAME = "abcdefghijklmnopqrstuvwxyz", "agent-test-abc123"


@pytest.fixture(autouse=True)
def _own_config(monkeypatch):
    """The session's own settings and the keys of its files come from each
    test, never from the shell: a Claude Code session's id or workspace
    decides which session file is read (RT-6.10, RT-6.17 step 2)."""
    for name in ("MM_RESPONSIBLE_HUMAN", "MM_TRUSTED_HUMANS", "MM_TRUSTED_HUMAN_USER_IDS",
                 "MM_OWNER", "MM_OWNER_USER_ID", "MM_AGENT_KIND", "MM_CHANNEL_PUSH",
                 "CLAUDE_CODE_SESSION_ID", "CLAUDE_SESSION_ID", "CLAUDE_PROJECT_DIR", "CLAUDECODE"):
        monkeypatch.delenv(name, raising=False)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _key(seed: bytes = SEED) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(seed)


def _raw(k: Ed25519PrivateKey) -> bytes:
    return k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def _x(k: Ed25519PrivateKey) -> str:
    return _b64(_raw(k))


def _sign(header: bytes, payload: bytes, k: Ed25519PrivateKey) -> str:
    signing = f"{_b64(header)}.{_b64(payload)}"
    return f"{signing}.{_b64(k.sign(signing.encode()))}"


def token(claims: dict | None = None, *, k: Ed25519PrivateKey | None = None,
          header: dict | None = None, **over) -> str:
    k = k or _key()
    h = {"alg": "EdDSA", "typ": attest.TYP, "kid": attest.kid_of(_raw(k))} | (header or {})
    c = {"bot_id": VBOT, "username": VNAME, "responsible": "alice", "trusted": ["alice", "bob"],
         "kind": "session", "via": "enrollment", "iat": IAT, "exp": EXP} | (claims or {}) | over
    return _sign(json.dumps(h).encode(), json.dumps(c).encode(), k)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _verify(tok, keys=None, *, bot_id=VBOT, username=VNAME, now=NOW):
    keys = attest.pinned(_x(_key())) if keys is None else keys
    return attest.verify(tok, keys, bot_id=bot_id, username=username, now=now)


# --- the format and the interop vector (steps 1, 5) -----------------------------

def test_the_interop_vector_holds(monkeypatch):
    """The registrar's vector: kid of seed 0x00..0x1f, the token's sha256, and
    the verifier accepts it inside iat..exp with the key pinned."""
    k = _key()
    assert attest.kid_of(_raw(k)) == "Vkdap1RjR0wChd9d"
    tok = _sign(VECTOR_HEADER, VECTOR_PAYLOAD, k)
    digest = _digest(tok)
    assert digest[:16] == "ac363c6df26b252b"
    monkeypatch.setattr(attest.time, "time", lambda: NOW)  # the clock, mocked
    claim, why = attest.verify(tok, attest.pinned(_x(k)), bot_id=VBOT, username=VNAME)
    assert why == ""
    assert claim == attest.Claim(bot_id=VBOT, username=VNAME, responsible="alice",
                                 trusted=("alice", "bob"), trusted_cut=0, kind="session",
                                 via="enrollment", iat=IAT, exp=EXP, kid="Vkdap1RjR0wChd9d")


def test_pinned_keys_by_kid_and_bad_entries_ignored(caplog):
    good = _x(_key())
    keys = attest.pinned(f" {good} ,not-a-key,,{_b64(b'short')}")
    assert list(keys) == ["Vkdap1RjR0wChd9d"]
    assert "entry 2 is no base64url Ed25519 public key" in caplog.text
    assert attest.pinned("") == {}


@pytest.mark.parametrize("change, why", [
    ({"exp": NOW - attest.SKEW_SEC}, "expired"),
    ({"iat": NOW + attest.SKEW_SEC + 1, "exp": NOW + 9999}, "issued in the future"),
    ({"bot_id": "z" * 26}, "bot_id mismatch"),
    ({"username": "agent-other"}, "username mismatch"),
    ({"kind": "daemon"}, "bad claims"),
    ({"via": "trust-me"}, "bad claims"),
    ({"trusted": "alice"}, "bad claims"),
    ({"trusted": ["alice", "no spaces"]}, "bad claims"),
    ({"trusted_cut": -1}, "bad claims"),
    ({"exp": True}, "bad claims"),
    ({"iat": EXP, "exp": IAT}, "bad claims"),
])
def test_claims_that_do_not_hold(change, why):
    claim, reason = _verify(token(change))
    assert claim is None and reason == why


def test_skew_and_defaults():
    """Within SKEW_SEC both ways still valid; missing trusted / responsible name nobody."""
    assert _verify(token(exp=NOW - attest.SKEW_SEC + 1))[0] is not None
    assert _verify(token(iat=NOW + attest.SKEW_SEC, exp=NOW + 9999))[0] is not None
    minimal = token({"bot_id": VBOT}, responsible="")
    claim, _ = _verify(minimal)
    assert claim is not None and claim.responsible == ""
    bare = _sign(json.dumps({"alg": "EdDSA", "typ": attest.TYP, "kid": attest.kid_of(_raw(_key()))}).encode(),
                 json.dumps({"bot_id": VBOT, "username": VNAME, "kind": "autonomous", "via": "secret",
                             "iat": IAT, "exp": EXP, "trusted_cut": 3}).encode(), _key())
    claim, _ = _verify(bare)
    assert claim is not None and claim.trusted == () and claim.trusted_cut == 3


def test_a_tampered_payload_fails_the_signature():
    tok = token()
    head, _, sig = tok.split(".")
    other = _b64(json.dumps({"bot_id": VBOT, "username": VNAME, "responsible": "mallory",
                             "trusted": ["mallory"], "kind": "session", "via": "enrollment",
                             "iat": IAT, "exp": EXP}).encode())
    assert _verify(f"{head}.{other}.{sig}") == (None, "bad signature")
    flipped = sig[:-2] + ("A" if sig[-2] != "A" else "B") + sig[-1]
    assert _verify(f"{head}.{tok.split('.')[1]}.{flipped}")[1] == "bad signature"


def test_wrong_kid_unpinned_key_and_wrong_alg():
    other = _key(bytes(range(1, 33)))
    # A kid that is not pinned, signed by the pinned key.
    assert _verify(token(header={"kid": "AAAAAAAAAAAAAAAA"})) == (None, "key not pinned")
    # Another key, its own kid: not pinned.
    assert _verify(token(k=other)) == (None, "key not pinned")
    # Another key under the pinned key's kid: the signature fails.
    assert _verify(token(k=other, header={"kid": "Vkdap1RjR0wChd9d"})) == (None, "bad signature")
    assert _verify(token(header={"alg": "HS256"})) == (None, "wrong alg")
    assert _verify(token(header={"alg": "none"})) == (None, "wrong alg")
    assert _verify(token(header={"typ": "JWT"})) == (None, "wrong typ")
    assert _verify(token(header={"crit": ["exp"]})) == (None, "unknown critical header")
    assert _verify(token(), keys={}) == (None, "no pinned key")


@pytest.mark.parametrize("garbage", [
    None, 42, b"x.y.z", "", "a", "a.b", "a.b.c.d", "a..c", "a.b.c", "é.b.c", "a+b.c.d",
    "x" * (attest.MAX_LEN + 1), "e30.e30.AAAA", "W10.W10.AAAA",
])
def test_garbage_is_invalid_never_an_error(garbage):
    claim, why = _verify(garbage)
    assert claim is None and why


def test_a_payload_that_is_no_object_or_has_duplicate_keys():
    k = _key()
    h = json.dumps({"alg": "EdDSA", "typ": attest.TYP, "kid": attest.kid_of(_raw(k))}).encode()
    assert _verify(_sign(h, b"[1, 2]", k)) == (None, "bad claims")
    dup = (b'{"bot_id":"' + VBOT.encode() + b'","bot_id":"' + VBOT.encode() + b'","username":"'
           + VNAME.encode() + b'","kind":"session","via":"enrollment","iat":1,"exp":2}')
    assert _verify(_sign(h, dup, k)) == (None, "bad claims")
    assert _verify(_sign(b'{"alg":"EdDSA","alg":"EdDSA"}', VECTOR_PAYLOAD, k)) == (None, "malformed")


# --- the own session (steps 1-3) --------------------------------------------------

ENV = {"MM_RESPONSIBLE_HUMAN": "alice", "MM_TRUSTED_HUMANS": "alice,bob"}


def _own(tok, environ=None, keys=None, **kw):
    keys = attest.pinned(_x(_key())) if keys is None else keys
    return attest.check_own(tok, bot_id=VBOT, username=VNAME, keys=keys,
                            environ=ENV if environ is None else environ, now=NOW, **kw)


def test_own_status_words():
    assert _own("").label == "none"
    valid = _own(token())
    assert valid.label == "valid" and valid.warnings == () and valid.publish
    assert _own(token(), keys={}).label == "unverified (no pinned key)"
    assert _own(token(), keys={}).publish
    assert not _own(token(bot_id="z" * 26), keys={}).publish  # not this bot's: kept off its account
    bad = _own(token(exp=IAT + 1))
    assert bad.label == "invalid (expired)" and not bad.publish
    assert "invalid (expired)" in bad.warnings[0]


def test_own_mismatch_warns_and_the_env_stays():
    """Step 2: responsible, trusted and kind against the configuration; only
    warnings, nothing changes the session's own settings."""
    env = {"MM_RESPONSIBLE_HUMAN": "carol", "MM_TRUSTED_HUMANS": "carol,alice,dave",
           "MM_AGENT_KIND": "autonomous"}
    w = " | ".join(_own(token(), environ=env).warnings)
    assert "names @alice as responsible, this session @carol" in w
    assert "trusts @bob, whom this session does not trust" in w
    assert "lacks 2 of 3 trusted names (carol, dave)" in w
    assert "says kind session, this session autonomous" in w
    cut = " | ".join(_own(token(trusted=["alice"], trusted_cut=1)).warnings)
    assert "(+1 cut): other owners' agents treat @agent-test-abc123 as foreign" in cut
    secret = " | ".join(_own(token(via="secret")).warnings)
    assert "registered with the shared secret (attestation via secret)" in secret
    assert "only where Mattermost's native owner agrees" in secret
    placeholder = {"MM_TRUSTED_HUMANS": "${MM_TRUSTED_HUMANS}", "MM_RESPONSIBLE_HUMAN": "alice"}
    assert attest.own_config(placeholder) == ("alice", [], "session")


SELF = "u" * 26  # FakeChat's bot


def _mine(**over) -> str:
    return token(**({"bot_id": SELF, "username": "agent-me"} | over))


async def test_start_checks_and_logs_but_writes_nothing_on_the_account(monkeypatch, caplog):
    """Step 2/3 (0.71.19): the own attestation is checked and logged once; it is not written onto the
    account (a bot's patch of its own user props is a 403): no write, no warning at every start."""
    seen: list = []
    c = _adapter(seen, {"customStatus": "{}"})
    monkeypatch.setenv("MM_REGISTRAR_KEYS", _x(_key()))
    monkeypatch.setenv(attest.ENV, _mine())
    monkeypatch.setattr(attest.time, "time", lambda: NOW)
    attest._started.clear()
    with caplog.at_level("INFO"):
        own = await attest.start(c)
    assert own.label == "valid" and own.publish and seen == []  # nothing PUT, the HELLO card still carries it
    assert "own attestation: valid" in caplog.text and "not published" not in caplog.text
    caplog.clear()
    with caplog.at_level("INFO"):
        assert (await attest.start(c)).label == "valid"
    assert "own attestation" not in caplog.text and seen == []  # once per bot and attestation


async def test_start_logs_an_invalid_one_and_nothing_without(monkeypatch):
    seen: list = []
    c = _adapter(seen, None)
    monkeypatch.setenv("MM_REGISTRAR_KEYS", _x(_key()))
    monkeypatch.setattr(attest.time, "time", lambda: NOW)
    attest._started.clear()
    assert (await attest.start(c)).label == "none"
    monkeypatch.setenv(attest.ENV, token())  # another bot's
    own = await attest.start(c)
    assert own.label == "invalid (bot_id mismatch)" and seen == []


def _adapter(seen: list, props: dict | None) -> MattermostAdapter:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/api/v4/users/me":
            return httpx.Response(200, json={"id": SELF, "username": "agent-me", "is_bot": True})
        if req.url.path == f"/api/v4/users/{SELF}" and req.method == "GET":
            return httpx.Response(200, json={"id": SELF, "username": "agent-me",
                                             **({"props": props} if props is not None else {})})
        if req.method in ("PUT", "POST", "DELETE"):
            seen.append((req.method, req.url.path))  # any write is recorded
            return httpx.Response(403, json={"message": "no"})
        return httpx.Response(404)

    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


def _registrar_adapter(answers: list, seen: list | None = None) -> MattermostAdapter:
    """An adapter whose registrar answers from `answers`, in order; seen lists the URLs asked."""
    it = iter(answers)

    def handler(req: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(req.url))
        return next(it)

    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


async def test_the_attestation_is_read_from_the_registrar(monkeypatch):
    """RT-6.24 step 5: `GET …/attestations/<bot id>` gives `{"record": …}`, read only with pinned keys."""
    tok = _mine()
    seen: list = []
    c = _registrar_adapter([httpx.Response(200, json={"record": tok})], seen)
    assert await c.agent_attestation(SELF) == ""   # no pinned keys: nothing to check it with
    assert seen == []
    monkeypatch.setenv("MM_REGISTRAR_KEYS", _x(_key()))
    monkeypatch.setattr(attest.time, "time", lambda: NOW)
    assert await c.agent_attestation(SELF) == tok
    assert seen == [f"https://mm.example/register/v1/attestations/{SELF}"]
    assert await c.agent_attestation(SELF) == tok and len(seen) == 1   # cached


async def test_the_registrars_own_404_is_none_and_any_other_failure_is_unreadable(monkeypatch):
    monkeypatch.setenv("MM_REGISTRAR_KEYS", _x(_key()))
    c = _registrar_adapter([httpx.Response(404, json={"error": "no attestation"}),
                            httpx.Response(404), httpx.Response(404, text="not found"),
                            httpx.Response(404, json={"error": "no mandate set"}),
                            httpx.Response(500), httpx.Response(200, json={"nothing": 1})])
    assert await c.agent_attestation(SELF) == ""                # the registrar's own: none
    for _ in range(5):                                           # cached as none, no further read
        assert await c.agent_attestation(SELF) == ""
    for other in ("b" * 26, "c" * 26, "d" * 26, "e" * 26, "f" * 26):  # the last: a 200 with no record
        assert await c.agent_attestation(other) is None        # a foreign 404, a 500: not readable
    with pytest.raises(NoAttestationEndpoint):
        await _registrar_adapter([httpx.Response(404)]).attestation_record(SELF)
    assert await _registrar_adapter([httpx.Response(404, json={"error": "no attestation"})]
                                    ).attestation_record(SELF) is None
    # The registrar's JSON ends with a newline: the body is parsed, not compared byte for byte.
    assert await _registrar_adapter([httpx.Response(404, content=b'{"error":"no attestation"}\n')]
                                    ).attestation_record(SELF) is None


async def test_an_unreadable_attestation_is_not_cached_as_none(monkeypatch):
    """The description decides on an unreadable one, and the next look asks again."""
    monkeypatch.setenv("MM_REGISTRAR_KEYS", _x(_key()))
    monkeypatch.setattr(attest.time, "time", lambda: NOW)
    tok = _mine()
    clock = [1000.0]
    monkeypatch.setattr("mattermost_ai_agent_bus.client.time.monotonic", lambda: clock[0])
    c = _registrar_adapter([httpx.Response(404), httpx.Response(200, json={"record": tok})])
    assert await c.agent_attestation(SELF) is None
    assert await c.agent_attestation(SELF) is None               # inside the short wait: no read
    clock[0] += 61.0
    assert await c.agent_attestation(SELF) == tok


async def test_the_cache_ends_with_the_records_exp(monkeypatch):
    monkeypatch.setenv("MM_REGISTRAR_KEYS", _x(_key()))
    monkeypatch.setattr(attest.time, "time", lambda: NOW)
    clock = [1000.0]
    monkeypatch.setattr("mattermost_ai_agent_bus.client.time.monotonic", lambda: clock[0])
    short, fresh = _mine(exp=NOW + 30), _mine()
    seen: list = []
    c = _registrar_adapter([httpx.Response(200, json={"record": short}),
                            httpx.Response(200, json={"record": fresh})], seen)
    assert await c.agent_attestation(SELF) == short
    clock[0] += 31.0
    assert await c.agent_attestation(SELF) == short and len(seen) == 1   # floor: a tenth of the TTL
    clock[0] += 31.0
    assert await c.agent_attestation(SELF) == fresh and len(seen) == 2


# --- registration, session file, host process (step 1) ----------------------------

def _registrar(answer: dict):
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.startswith("/api/v4/users/") or req.url.path.startswith("/api/v4/bots/"):
            return httpx.Response(200, json={"id": SELF, "username": "agent-me", "is_bot": True,
                                             "bot_description": "for @carol · trusts @carol · via agent-registrar"})
        return httpx.Response(200, json=answer)

    c = MattermostAdapter(base_url="https://mm.example", reg_secret="s",
                          register_url="https://mm.example/register/v1/agents")
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c


async def test_session_start_takes_the_attestation_and_compares_it(monkeypatch):
    """RT-6.10 with an attestation: it reaches the environment and the session
    file; a valid one replaces the profile read-back in the warning."""
    monkeypatch.setenv("MM_REGISTRAR_KEYS", _x(_key()))
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "dave")
    monkeypatch.setattr(attest.time, "time", lambda: NOW)
    tok = _mine()
    c = _registrar({"username": "agent-me", "user_id": SELF, "bot_token": "t", "attestation": tok})
    info = await c.session_start("proj", unique=False)
    in_env = _digest(os.environ[attest.ENV])
    assert _digest(info.attestation) == _digest(tok) and in_env == _digest(tok)
    stored = session_file.parse(session_file.session_file_path()).get(attest.ENV, "")
    assert _digest(stored) == _digest(tok)
    assert "names @alice as responsible, this session @dave" in c.profile_warning
    assert "profile of" not in c.profile_warning  # the read-back is replaced
    # Without one the read-back works as before, and the environment is clean.
    c2 = _registrar({"username": "agent-me", "user_id": SELF, "bot_token": "t", "attestation": "a.b"})
    info2 = await c2.session_start("proj", unique=False)
    assert info2.attestation == "" and attest.ENV not in os.environ
    assert "lacks 1 of 1 trusted names" in c2.profile_warning


@pytest.mark.parametrize("keyed", ["session id", "workspace"])
def test_adopt_takes_the_attestation_from_the_session_file(monkeypatch, tmp_path, keyed):
    """RT-6.10 steps 5 and 7: `claude --continue` (same session id), or the
    workspace's next server after one ended with the bot kept."""
    if keyed == "session id":
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "attest-test-session")
    else:
        workspace = tmp_path / "work"
        workspace.mkdir()
        monkeypatch.setenv("MM_WORKSPACE", str(workspace))
    tok = _mine()
    session_file.write(SessionInfo(url="https://mm.example", name="proj", username="agent-me",
                                   user_id=SELF, bot_token="t", attestation=tok), "team", "agents")
    stored = session_file.parse(session_file.session_file_path())[attest.ENV]
    assert _digest(stored) == _digest(tok)
    if keyed == "workspace":
        assert session_file.mark_ended("t")  # the server ended and kept its bot

    c = MattermostAdapter(base_url="https://mm.example")
    monkeypatch.setattr(httpx.Client, "get", lambda self, url, headers: httpx.Response(200))
    info = c.adopt_sync()
    assert info is not None and _digest(info.attestation) == _digest(tok)
    session_file.write(SessionInfo(url="u", name="n", username="x", user_id=SELF, bot_token="t"),
                       "team", "agents")
    assert attest.ENV not in session_file.parse(session_file.session_file_path())


def test_the_host_process_takes_the_attestation_and_the_keys():
    """RT-6.18 start line: both are bus settings and reach the host process."""
    tok = _mine()
    line = json.dumps({"key": "k" * 64, "env": {attest.ENV: tok, "MM_REGISTRAR_KEYS": _x(_key())}})
    _, env = hosted.read_start(io.BytesIO(line.encode() + b"\n"))
    assert set(env) == {attest.ENV, "MM_REGISTRAR_KEYS"} and _digest(env[attest.ENV]) == _digest(tok)


# --- bus_info and the HELLO card (steps 2, 4) --------------------------------------

class _Chat(FakeChat):
    trusted_source = "MM_TRUSTED_HUMANS"


async def test_bus_info_names_the_attestation(monkeypatch):
    chat = _Chat(trusted={"a" * 26: "alice"}, descriptions={SELF: "unbound · claims @alice"})
    monkeypatch.setattr(server, "_get_client", lambda: chat)
    monkeypatch.setattr(server.lease, "crown_held", lambda uid: False)
    monkeypatch.setattr(attest.time, "time", lambda: NOW)
    monkeypatch.setenv("MM_TRUSTED_HUMANS", "alice,bob")
    out = json.loads(await server.bus_info())
    assert out["attestation"] == "none" and "unbound" in out["warning"]
    monkeypatch.setenv(attest.ENV, _mine())
    out = json.loads(await server.bus_info())
    assert out["attestation"] == "unverified (no pinned key)"
    monkeypatch.setenv("MM_REGISTRAR_KEYS", _x(_key()))
    out = json.loads(await server.bus_info())
    assert out["attestation"] == "valid" and "warning" not in out  # replaces the profile's unbound
    monkeypatch.setenv(attest.ENV, _mine(exp=IAT + 5))
    out = json.loads(await server.bus_info())
    assert out["attestation"] == "invalid (expired)" and "invalid (expired)" in out["warning"]


async def test_the_hello_card_carries_it_and_the_text_stays(monkeypatch):
    chat = FakeChat(trusted={"a" * 26: "alice"}, post_id="p1")
    monkeypatch.setattr(server, "_get_client", lambda: chat)
    monkeypatch.setattr(server.lease, "crown_held", lambda uid: False)
    await server.announce(project="bus", task="t", model="m")
    plain = chat.posted[0]
    assert "attestation" not in plain["props"]["agent_card"]
    tok = _mine()
    monkeypatch.setenv(attest.ENV, tok)
    await server.announce(project="bus", task="t", model="m")
    signed = chat.posted[1]
    carried = _digest(signed["props"]["agent_card"]["attestation"])
    assert carried == _digest(tok)
    assert signed["message"] == plain["message"]
    assert "attestation" not in (a2a.card_of(post_of({"message": signed["message"]}), "agent-me") or {})


# --- other agents' owners (step 5, ADR-003) ------------------------------------------

ALICE, BOB, CAROL = "a" * 26, "b" * 26, "c" * 26
AGENT = "p" * 26
NAMES = {ALICE: "alice", BOB: "bob", CAROL: "carol", AGENT: "agent-p", BOT: "agent-me"}
MINE = "for @alice · trusts @alice, @bob · via agent-registrar"
CAROLS = "for @carol · trusts @carol · via agent-registrar"


def _theirs(**over) -> str:
    return token(**({"bot_id": AGENT, "username": "agent-p"} | over))


def _chat(attestation: str | None = None, *, description: str = CAROLS, native: str | None = None):
    return FakeChat(names=NAMES, agents={AGENT}, trusted={ALICE: "alice", BOB: "bob"},
                    descriptions={BOT: MINE, AGENT: description},
                    native_owners={AGENT: native} if native else {},
                    attestations={AGENT: attestation} if attestation else {})


@pytest.fixture
def pinned(monkeypatch):
    monkeypatch.setenv("MM_REGISTRAR_KEYS", _x(_key()))
    monkeypatch.setattr(attest.time, "time", lambda: NOW)


async def test_a_valid_attestation_wins_over_the_description(pinned, caplog):
    caplog.set_level(logging.WARNING)
    chat = _chat(_theirs())
    agent = await owners.read(chat, AGENT)
    assert agent == owners.Owner("alice", frozenset({"alice", "bob"}), False, attested=True)
    assert await owners.foreign(chat, {AGENT}) == {}
    assert "its description disagree: the attestation counts" in caplog.text
    # Without it, Carol's agent is foreign by its description.
    assert await owners.foreign(_chat(), {AGENT}) == {AGENT: "carol"}


async def test_enrollment_stands_against_the_native_owner(pinned):
    """`via: enrollment` is signed and checked: a reassigned owner_id does not change it."""
    assert (await owners.read(_chat(_theirs(), native="carol"), AGENT)).owner == "alice"
    assert await owners.foreign(_chat(_theirs(), native="carol"), {AGENT}) == {}


@pytest.mark.parametrize("native", [None, "agent-registrar", "carol"])
async def test_a_secret_claim_is_foreign_without_an_agreeing_native_owner(pinned, native):
    """`via: secret` never makes an agent more trusted than without it: no
    native owner, or one that disagrees, gives no owner (foreign, as unbound);
    its trusted list is still read for display."""
    for description in ("unbound · claims @alice", CAROLS):
        chat = _chat(_theirs(via="secret"), description=description, native=native)
        agent = await owners.read(chat, AGENT)
        assert agent.owner == "" and agent.attested and agent.trusts == {"alice", "bob"}
        assert not owners.is_peer(agent, {"alice", "bob"})
        assert await owners.foreign(chat, {AGENT}) == {AGENT: ""}


async def test_a_secret_claim_is_a_peer_only_when_the_native_owner_agrees(pinned):
    chat = _chat(_theirs(via="secret"), description="unbound · claims @alice", native="alice")
    assert (await owners.read(chat, AGENT)).owner == "alice"
    assert await owners.foreign(chat, {AGENT}) == {}


async def test_a_cut_trusted_list_is_foreign(pinned):
    chat = _chat(_theirs(trusted=["alice"], trusted_cut=2))
    agent = await owners.read(chat, AGENT)
    assert agent.cut and agent.attested
    assert await owners.foreign(chat, {AGENT}) == {AGENT: "alice"}


@pytest.mark.parametrize("bad", ["expired", "unpinned", "garbage", "other bot"])
async def test_an_invalid_attestation_falls_back_logged_once(pinned, caplog, bad):
    tok = {"expired": _theirs(exp=IAT + 1), "unpinned": token(k=_key(bytes(range(1, 33))),
                                                                   bot_id=AGENT, username="agent-p"),
           "garbage": "x.y.z", "other bot": _theirs(bot_id=CAROL)}[bad]
    caplog.set_level(logging.WARNING)
    chat = _chat(tok)
    assert await owners.read(chat, AGENT) == owners.parse(CAROLS)
    await owners.read(chat, AGENT)
    assert caplog.text.count("its profile decides") == 1


async def test_no_pinned_keys_change_nothing(monkeypatch):
    """Exactly as before: the description decides, nothing is verified or logged."""
    chat = _chat(_theirs())
    assert await owners.read(chat, AGENT) == owners.parse(CAROLS)
    assert await owners.foreign(chat, {AGENT}) == {AGENT: "carol"}
    assert owners._noted == set()


async def test_the_roster_marks_attested_agents(pinned):
    chat = _chat(_theirs())
    rows = [type("Row", (), {"user_id": AGENT, "username": "agent-p"})()]
    out = await server._owners_of(chat, rows)
    assert out["agent-p"] == {"owner": "alice", "trusts": ["alice", "bob"], "foreign": None,
                              "attested": True}

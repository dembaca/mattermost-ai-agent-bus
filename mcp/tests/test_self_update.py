"""The registrar's release record and the self-update notice (RT-6.32,
ADR-011 phase 3): attest.verify_release, the read on the host
(selfupdate.py, which the installer's --auto and the release check run), and
what bus_info and the banner say about it (release.auto_line, auto_info)."""

import base64
import json
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mattermost_ai_agent_bus import attest, release, selfupdate

from test_ask_mode_signed import _key, _sign, _x

# An Ed25519 SSH public key blob: the type, then the 32 key bytes, each with its length.
_NAME = b"ssh-ed25519"
_BLOB = len(_NAME).to_bytes(4, "big") + _NAME + (32).to_bytes(4, "big") + bytes(range(32))
OWNER = "ssh-ed25519 " + base64.b64encode(_BLOB).decode()
BACKUP = "ssh-ed25519 " + base64.b64encode(_BLOB[:-1] + b"\xff").decode()


def record(claims: dict | None = None, *, typ: str = attest.RELEASE_TYP,
           key: Ed25519PrivateKey | None = None, **over) -> str:
    k = key or _key()
    iat = int(time.time())
    c = {"signers": [OWNER, BACKUP], "version": "v1.2.3", "set_at_ms": 1, "iat": iat,
         "exp": iat + 3600} | (claims or {}) | over
    header = {"alg": "EdDSA", "typ": typ, "kid": attest.kid_of(_raw(k))}
    return _sign(json.dumps(header).encode(), json.dumps(c).encode(), k)


def _raw(k: Ed25519PrivateKey) -> bytes:
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return k.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


KEYS = attest.pinned(_x(_key()))


def test_a_sound_record_verifies():
    got, why = attest.verify_release(record(), KEYS)
    assert why == "" and got is not None
    assert got.signers == (OWNER, BACKUP) and got.version == "1.2.3"
    keys_only, _ = attest.verify_release(record(version=None), KEYS)
    assert keys_only is not None and keys_only.version == ""


@pytest.mark.parametrize("token, why", [
    (lambda: record(typ=attest.DEBUG_TYP), "wrong typ"),
    (lambda: record(key=_key(b"\x01" * 32)), "key not pinned"),
    (lambda: record(iat=int(time.time()) - 7200, exp=int(time.time()) - 3600), "expired"),
    (lambda: record(signers=[]), "bad signers"),
    (lambda: record(signers=[OWNER, OWNER]), "bad signers"),
    (lambda: record(signers=["ssh-rsa AAAAB3NzaC1yc2E="]), "bad signers"),
    (lambda: record(signers=["ssh-ed25519 " + base64.b64encode(b"\0\0\0\x07ssh-rsa").decode()]), "bad signers"),
    (lambda: record(signers=[OWNER + "\nssh-ed25519 AAAA"]), "bad signers"),
    (lambda: record(signers=[OWNER] * 17), "bad signers"),
    (lambda: record(version="1.2.3"), "bad version"),
    (lambda: record(version="v01.2.3"), "bad version"),
    (lambda: record(set_at_ms="now"), "bad claims"),
    (lambda: "not.a.jws", "malformed"),
])
def test_a_record_out_of_shape_is_refused(token, why):
    """RT-6.32 step 2: the pinned kid, the release typ, exp with skew, and the
    claims' shapes; never an exception."""
    assert attest.verify_release(token(), KEYS) == (None, why)
    assert attest.verify_release(record(), {}) == (None, "no pinned key")


def test_one_key_has_one_spelling():
    """The registrar's canonical form: type and standard base64 of the blob; a
    comment or another spacing of the same key is the same key."""
    b64 = OWNER.split()[1]
    assert attest.release_key(f"{OWNER} owner@laptop") == OWNER
    assert attest.release_key(f"ssh-ed25519  {b64}") == OWNER


def _transport(status: int, body: object) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(status, json=body)
    return httpx.MockTransport(handler)


def test_fetch_tells_none_from_a_failed_read():
    """RT-6.32 step 2: only the exact 404 body means none; any other 404, an
    error, a redirect or an answer without a record is a failed read."""
    url = "https://chat.example/register/v1/release"
    assert selfupdate.fetch(url, transport=_transport(200, {"record": "a.b.c"})) == "a.b.c"
    assert selfupdate.fetch(url, transport=_transport(404, {"error": "no release set"})) is None
    for status, body in ((404, {"message": "not found"}), (404, {"error": "no release set", "x": 1}),
                         (500, {"error": "boom"}), (302, {}), (200, {"nothing": 1})):
        with pytest.raises(selfupdate.Failed):
            selfupdate.fetch(url, transport=_transport(status, body))
    with pytest.raises(selfupdate.Failed, match="over"):
        selfupdate.fetch(url, transport=_transport(200, {"record": "x" * 9000}))


def test_the_url_follows_the_registrars():
    assert selfupdate.release_url({"MM_REGISTER_URL": "https://r.example/register/v1/agents/"}) == \
        "https://r.example/register/v1/release"
    assert selfupdate.release_url({"MM_CHAT_URL": "https://chat.example"}) == \
        "https://chat.example/register/v1/release"
    assert selfupdate.release_url({}) is None
    assert selfupdate.release_url({"MM_CHAT_URL": "file:///etc"}) is None


def test_read_and_render(monkeypatch):
    """The lines the installer parses: STATUS, WHY, KID, VERSION, SIGNER."""
    assert selfupdate.read({})[0] == "nokeys"
    env = {"MM_REGISTRAR_KEYS": _x(_key()), "MM_CHAT_URL": "https://chat.example"}
    monkeypatch.setattr(selfupdate, "fetch", lambda url: record())
    status, rec, why = selfupdate.read(env)
    assert status == "ok" and rec is not None
    out = selfupdate.render(status, rec, why).splitlines()
    assert out[0] == "STATUS=ok" and "VERSION=1.2.3" in out and f"SIGNER={OWNER}" in out
    monkeypatch.setattr(selfupdate, "fetch", lambda url: record(typ=attest.MODE_TYP))
    assert selfupdate.read(env)[0] == "failed"
    monkeypatch.setattr(selfupdate, "fetch", lambda url: None)
    assert selfupdate.read(env)[0] == "none"


def _auto(d: Path, text: str) -> None:
    (d / release.AUTO_FILE).write_text(text)


def test_the_notice_says_what_will_happen(tmp_path):
    """RT-6.32 step 7: in plain words, from release-auto.env and the hold;
    nothing for the plugin or a checkout, whose server never updates itself."""
    layout = tmp_path / "host"
    layout.mkdir()
    _auto(tmp_path, "REGISTRAR=ok\nTARGET=1.3.0\nKEYS=match\nRESULT=waiting\nTARGET=x;rm\n")
    assert release.auto_line("1.2.0", tmp_path, layout) == "updates itself to v1.3.0 when this session ends"
    assert release.auto_line("1.3.0", tmp_path, layout) == ""
    assert release.auto_line("1.2.0", tmp_path, None, root=tmp_path / "checkout") == ""
    _auto(tmp_path, "TARGET=1.3.0\nKEYS=mismatch\n")
    assert "doesn't trust" in release.auto_line("1.2.0", tmp_path, layout)
    (layout / release.HOLD_FILE).write_text("held")
    assert release.auto_line("1.2.0", tmp_path, layout).startswith("self-update held")
    info = release.auto_info("1.2.0", tmp_path, layout)
    assert info["held"] is True and info["keys"] == "mismatch" and info["target"] == "1.3.0"


def test_bus_info_carries_auto(tmp_path, monkeypatch):
    """bus_info's release.auto: the plugin's server says self-update does not apply."""
    monkeypatch.setattr(release, "state_dir", lambda: tmp_path)
    out = release.notice("1.2.0")
    assert out["auto"]["applies"] is False
    assert "updates itself between sessions" in release.NOTE

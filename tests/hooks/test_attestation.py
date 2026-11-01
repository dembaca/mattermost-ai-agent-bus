"""RT-6.24 owner attestation, the shell side: the registration carries the
registrar's attestation to the export lines, the session file and, in a
corral session, the host process, which publishes it on the bot's account
without losing its custom status; the keys helper shows the registrar's keys
for pinning and writes nothing.

No token is printed: the tests compare hashes and look for names.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import time

import pytest
from conftest import ME, ROOT

# The shape of a compact JWS, nothing signed: what the shell carries is opaque to it.
SHAPED = "aGVhZA.cGF5bG9hZA.c2ln"
ANSWER = {"username": "agent-xyz", "bot_token": "bot-token-for-tests", "user_id": ME}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _exports(stdout: str) -> dict[str, str]:
    out = {}
    for line in stdout.splitlines():
        if line.startswith("export ") and "=" in line:
            k, v = line.removeprefix("export ").split("=", 1)
            out[k] = v
    return out


def _session_file(stdout: str) -> dict[str, str]:
    path = _exports(stdout)["MM_AGENT_SESSION_FILE"]
    return dict(line.split("=", 1) for line in open(path).read().splitlines()
                if "=" in line and not line.startswith("#"))


def test_register_exports_the_attestation(mm, run):
    """Step 1: taken from the registrar's answer like the other fields, exported
    as MM_BOT_ATTESTATION and written to the session file; never on stderr."""
    mm.register_reply = {**ANSWER, "attestation": SHAPED}
    r = run("bin/mm-agent-register.sh", "--exports", "xyz", MM_BOT_TOKEN=None, MM_REG_SECRET="s")
    assert r.code == 0
    exported = _exports(r.stdout).get("MM_BOT_ATTESTATION", "")
    assert _digest(exported) == _digest(SHAPED)
    stored = _session_file(r.stdout).get("MM_BOT_ATTESTATION", "")
    assert _digest(stored) == _digest(SHAPED)
    assert SHAPED not in r.stderr


@pytest.mark.parametrize("attestation", [None, "", "not a jws", {"x": 1}, "a.b"])
def test_register_without_a_sound_attestation_changes_nothing(mm, run, attestation):
    """Absent: no line anywhere. Malformed: dropped with a warning."""
    mm.register_reply = ANSWER if attestation is None else {**ANSWER, "attestation": attestation}
    r = run("bin/mm-agent-register.sh", "--exports", "xyz", MM_BOT_TOKEN=None, MM_REG_SECRET="s",
            MM_BOT_ATTESTATION="stale.left.over")
    assert r.code == 0
    assert "MM_BOT_ATTESTATION" not in r.stdout
    assert "MM_BOT_ATTESTATION" not in _session_file(r.stdout)
    assert ("attestation is malformed" in r.stderr) == (attestation not in (None, ""))


def test_the_session_file_loader_takes_it(tmp_path):
    f = tmp_path / "s.env"
    f.write_text(f"MM_BOT_USER_ID={ME}\nMM_BOT_ATTESTATION={SHAPED}\nMM_OTHER=x\n")
    out = subprocess.run(["bash", "-c", 'source "$1"; mm_load_env_file "$2"; '
                          'printf "%s" "${MM_BOT_ATTESTATION:-}" | shasum -a 256',
                          "_", str(ROOT / "bin" / "mm-agent-lib.sh"), str(f)],
                         capture_output=True, text=True, timeout=30)
    assert out.stdout.split()[0] == _digest(SHAPED)


# --- the keys helper --------------------------------------------------------------

def _seed_x() -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))  # the public test seed
    return _b64(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def _keys(kid: str) -> dict:
    return {"keys": [{"kty": "OKP", "crv": "Ed25519", "alg": "EdDSA", "use": "sig",
                      "kid": kid, "x": _seed_x()}]}


@pytest.mark.parametrize("via", ["chat", "register"])
def test_the_keys_helper_shows_them_for_pinning(mm, run, tmp_path, via):
    mm.registrar_keys = _keys("Vkdap1RjR0wChd9d")
    env = {"MM_REGISTER_URL": f"{mm.url}/register/v1/agents"} if via == "register" else {}
    r = run("bin/mm-agent-registrar-keys.sh", MM_BOT_TOKEN=None, **env)
    assert r.code == 0, r.stderr
    x = _seed_x()
    assert f"kid Vkdap1RjR0wChd9d  x {x}" in r.stdout
    assert ("(kid checked)" in r.stdout) == bool(shutil.which("openssl"))
    assert f"export MM_REGISTRAR_KEYS={x}" in r.stdout and "out of band" in r.stdout
    assert mm.writes == [] and not (tmp_path / "state").exists()  # writes nothing


@pytest.mark.skipif(not shutil.which("openssl"), reason="the kid is checked with openssl")
def test_the_keys_helper_refuses_a_kid_that_is_not_the_keys(mm, run):
    mm.registrar_keys = _keys("AAAAAAAAAAAAAAAA")
    r = run("bin/mm-agent-registrar-keys.sh", MM_BOT_TOKEN=None)
    assert r.code == 1 and "MISMATCH" in r.stderr and "export MM_REGISTRAR_KEYS" not in r.stdout


def test_the_keys_helper_without_keys_fails(mm, run):
    r = run("bin/mm-agent-registrar-keys.sh", MM_BOT_TOKEN=None)
    assert r.code == 1 and "could not read" in r.stderr


# --- corral: the host process publishes it (RT-6.18) ---------------------------------

START = "adapters/corral/10-mm-session-start.sh"
PY = ROOT / "mcp" / ".venv" / "bin" / "python"
HOST = {"MM_BOT_TOKEN": None, "MM_BOT_USER_ID": None, "MM_TEAM_ID": None,
        "MM_CHANNEL_ID": None, "MM_TEAM": "team", "MM_CHANNEL": "agents",
        "MM_REG_SECRET": "test-secret", "MM_PROJECT_CHANNEL": "proj-x"}


def _for_me() -> str:
    """A token in the attestation's shape naming the test bot; no key is pinned,
    so the host process publishes it unverified. Leading blanks keep its
    segments from looking like a JWT to an output scanner."""
    head = b' {"alg":"EdDSA","typ":"mm-agent-attestation+jwt","kid":"Vkdap1RjR0wChd9d"}'
    body = json.dumps({"bot_id": ME, "username": "agent-test", "responsible": "dembaca",
                       "trusted": ["dembaca"], "kind": "session", "via": "enrollment",
                       "iat": int(time.time()), "exp": int(time.time()) + 3600})
    return f"{_b64(head)}.{_b64((' ' + body).encode())}.{_b64(bytes(64))}"


@pytest.mark.skipif(not PY.exists(), reason="needs mcp/.venv: uv run --directory mcp")
def test_corral_hands_it_to_the_host_process_which_publishes_it(mm, run):
    """Step 1 in a keyless corral session: the export lines carry it into the
    host process's start line, not into the sandbox; step 3: the host process
    writes it into the bot's props, the custom status kept."""
    signed = _for_me()
    status = json.dumps({"emoji": "zzz", "text": "idle · ~proj-x", "duration": "date_and_time",
                         "expires_at": "2026-11-01T12:00:00Z"})
    mm.user_props = {"customStatus": status}
    mm.register_reply = {"username": "agent-test", "bot_token": "bot-token-for-tests",
                         "user_id": ME, "attestation": signed}
    r = run(START, CORRAL_SESSION_ID="sess-attest", MM_BOT_ATTESTATION="stale.left.over", **HOST)
    assert r.code == 0
    out = json.loads(r.stdout)
    assert "MM_BUS_UNAVAILABLE" not in out["env"] and "MM_BOT_ATTESTATION" not in out["env"]
    assert signed not in r.stdout + r.stderr and "stale.left.over" not in r.stdout
    patch = f"/api/v4/users/{ME}/patch"
    for _ in range(150):
        sent = [b for m, p, b in mm.writes if m == "PUT" and p == patch]
        if sent:
            break
        time.sleep(0.1)
    assert sent, "the host process wrote no props"
    props = sent[0]["props"]
    assert props["customStatus"] == status
    published = _digest(props["agent_attestation"])
    assert published == _digest(signed)
    log = (run.state_dir / "sess-attest.host.log").read_text()
    assert "own attestation: unverified (no pinned key)" in log
    assert signed not in log
    assert not os.environ.get("MM_BOT_ATTESTATION")

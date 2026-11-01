"""Owner attestation: the registrar's signed claim of whom a bot answers to
(RT-6.24, ADR-003).

On registration the registrar may answer with ``attestation``, a compact JWS:
three base64url segments without padding, header
``{"alg":"EdDSA","typ":"mm-agent-attestation+jwt","kid":…}``, an Ed25519
signature over ``<header>.<payload>``. The payload names the bot (``bot_id``,
``username``), its owner (``responsible``), its trusted humans (``trusted``,
``trusted_cut`` when the registrar cut the list), ``kind``, ``via``
(``enrollment``: the registrar checked the owner; ``secret``: only the
caller's claim) and ``iat`` / ``exp`` in unix seconds. The ``kid`` is the first
16 characters of base64url(sha256(raw public key)).

The keys are pinned in ``MM_REGISTRAR_KEYS`` (base64url raw public keys,
comma-separated), never fetched at runtime: ``bin/mm-agent-registrar-keys.sh``
shows the registrar's for the operator to compare out of band. Without a
pinned key nothing here changes what the bus does.

``verify`` is strict: whatever is malformed gives no claim and a reason,
never an exception. ``check_own`` compares the session's own attestation with
its configuration (the environment stays authoritative for the own session);
``start`` publishes it in the bot's own user props, where every other agent's
owner read finds it (owners.attested).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import functools
import hashlib
import json
import logging
import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)

ALG = "EdDSA"
TYP = "mm-agent-attestation+jwt"
ENV = "MM_BOT_ATTESTATION"
KEYS_ENV = "MM_REGISTRAR_KEYS"
ENROLLMENT, SECRET = "enrollment", "secret"
KINDS = ("session", "autonomous")
# Clock skew allowed between the registrar and this host, both ways: a claim
# counts until exp + SKEW_SEC and from iat - SKEW_SEC.
SKEW_SEC = 60
# Longer is no attestation (the host process takes values up to 8192, hosted.py).
MAX_LEN = 8192
_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
# A plugin manifest's unexpanded ${VAR} is no value (client.env).
_PLACEHOLDER = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")
_HANDLE = re.compile(r"[A-Za-z0-9._-]{1,64}")

# The status words bus_info shows (RT-6.24 step 2).
VALID, INVALID, UNVERIFIED, NONE = "valid", "invalid", "unverified", "none"


@dataclass(frozen=True)
class Claim:
    """A verified attestation: what the registrar signed, names lower case."""

    bot_id: str
    username: str
    responsible: str  # '' when the registrar named no owner
    trusted: tuple[str, ...]
    trusted_cut: int  # how many trusted names the registrar left out; 0: none
    kind: str
    via: str
    iat: int
    exp: int
    kid: str


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64url(text: str) -> bytes | None:
    if not _SEGMENT.fullmatch(text) or len(text) % 4 == 1:
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (binascii.Error, ValueError):
        return None


def kid_of(raw: bytes) -> str:
    """The key id of a raw Ed25519 public key: base64url(sha256(key))[:16]."""
    return b64url(hashlib.sha256(raw).digest())[:16]


@functools.lru_cache(maxsize=4)
def _keys(value: str) -> Mapping[str, Ed25519PublicKey]:
    out: dict[str, Ed25519PublicKey] = {}
    for n, item in enumerate((i.strip() for i in value.split(",")), 1):
        if not item:
            continue
        raw = _unb64url(item)
        if raw is None or len(raw) != 32:
            log.warning("%s: entry %d is no base64url Ed25519 public key (32 bytes): ignored",
                        KEYS_ENV, n)
            continue
        try:
            out[kid_of(raw)] = Ed25519PublicKey.from_public_bytes(raw)
        except ValueError:
            log.warning("%s: entry %d is no Ed25519 public key: ignored", KEYS_ENV, n)
    return MappingProxyType(out)


def pinned(value: str | None = None) -> Mapping[str, Ed25519PublicKey]:
    """The pinned registrar keys by kid: MM_REGISTRAR_KEYS (or `value`);
    entries that are no key are left out, with a warning."""
    return _keys(os.environ.get(KEYS_ENV, "") if value is None else value)


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f"duplicate key {k}")
        out[k] = v
    return out


def _json(segment: str) -> dict[str, Any] | None:
    raw = _unb64url(segment)
    if raw is None:
        return None
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicates)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _handle(value: object) -> str | None:
    """A username as the registrar writes it, lower case; None when it is none."""
    if not isinstance(value, str):
        return None
    name = value.removeprefix("@")
    return name.lower() if _HANDLE.fullmatch(name) else None


def _claim(payload: Mapping[str, Any], kid: str) -> Claim | None:
    """The payload's claims, typed; None when one is missing or malformed."""
    bot_id, username = payload.get("bot_id"), payload.get("username")
    responsible = payload.get("responsible", "")
    trusted, cut = payload.get("trusted", []), payload.get("trusted_cut", 0)
    iat, exp = _int(payload.get("iat")), _int(payload.get("exp"))
    if not (isinstance(bot_id, str) and bot_id and isinstance(username, str) and username):
        return None
    owner = "" if responsible == "" else _handle(responsible)
    names = [_handle(t) for t in trusted] if isinstance(trusted, list) else [None]
    cut = _int(cut)
    if owner is None or None in names or cut is None or cut < 0 or iat is None or exp is None:
        return None
    if payload.get("kind") not in KINDS or payload.get("via") not in (ENROLLMENT, SECRET):
        return None
    if exp <= iat:
        return None
    return Claim(bot_id=bot_id, username=username, responsible=owner,
                 trusted=tuple(n for n in names if n), trusted_cut=cut,
                 kind=str(payload["kind"]), via=str(payload["via"]), iat=iat, exp=exp, kid=kid)


def verify(token: object, keys: Mapping[str, Ed25519PublicKey], *, bot_id: str, username: str,
           now: float | None = None) -> tuple[Claim | None, str]:
    """The attestation's claim when it is sound: a pinned key signed it,
    EdDSA and the attestation type, not expired, issued not later than now
    (both with SKEW_SEC of clock skew), and for this bot (`bot_id`,
    `username`). Else (None, why). Never raises."""
    try:
        return _verify(token, keys, bot_id=bot_id, username=username,
                       now=time.time() if now is None else now)
    except Exception as exc:  # noqa: BLE001 — strict: malformed is invalid, never an error
        log.debug("attestation not verified: %s", type(exc).__name__)
        return None, "malformed"


def _verify(token: object, keys: Mapping[str, Ed25519PublicKey], *, bot_id: str, username: str,
            now: float) -> tuple[Claim | None, str]:
    if not keys:
        return None, "no pinned key"
    if not isinstance(token, str) or not token or len(token) > MAX_LEN:
        return None, "malformed"
    parts = token.split(".")
    if len(parts) != 3 or not all(_SEGMENT.fullmatch(p) for p in parts):
        return None, "malformed"
    header = _json(parts[0])
    if header is None:
        return None, "malformed"
    if header.get("alg") != ALG:
        return None, "wrong alg"
    if header.get("typ") != TYP:
        return None, "wrong typ"
    if "crit" in header:
        return None, "unknown critical header"
    kid = header.get("kid")
    key = keys.get(kid) if isinstance(kid, str) else None
    if key is None or not isinstance(kid, str):
        return None, "key not pinned"
    signature = _unb64url(parts[2])
    if signature is None or len(signature) != 64:
        return None, "malformed"
    try:
        key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"))
    except InvalidSignature:
        return None, "bad signature"
    payload = _json(parts[1])
    claim = _claim(payload, kid) if payload is not None else None
    if claim is None:
        return None, "bad claims"
    if claim.exp + SKEW_SEC <= now:
        return None, "expired"
    if claim.iat - SKEW_SEC > now:
        return None, "issued in the future"
    if claim.bot_id != bot_id:
        return None, "bot_id mismatch"
    if claim.username != username:
        return None, "username mismatch"
    return claim, ""


def _unverified_payload(token: str) -> dict[str, Any] | None:
    """The payload as it is, unchecked: only to tell whether an attestation
    that cannot be verified here names this bot at all."""
    parts = token.split(".") if len(token) <= MAX_LEN else []
    return _json(parts[1]) if len(parts) == 3 and all(_SEGMENT.fullmatch(p) for p in parts) else None


# --- the own session (RT-6.24 steps 1–3) ---------------------------------------

@dataclass(frozen=True)
class Own:
    """What this session's own attestation is worth, and what to warn about."""

    status: str  # VALID, INVALID, UNVERIFIED or NONE
    reason: str = ""
    claim: Claim | None = None
    warnings: tuple[str, ...] = ()
    publish: bool = False  # worth putting into the bot's props and HELLO card

    @property
    def label(self) -> str:
        """`valid`, `invalid (<reason>)`, `unverified (no pinned key)` or `none`."""
        return f"{self.status} ({self.reason})" if self.reason else self.status


def own_config(environ: Mapping[str, str] | None = None) -> tuple[str, list[str], str]:
    """What this session sent to the registrar (client._registration_fields):
    the responsible human, the trusted names and the kind."""
    env = os.environ if environ is None else environ

    def get(name: str) -> str:
        value = env.get(name, "")
        return "" if _PLACEHOLDER.match(value) else value

    trusted = [n.strip().lstrip("@") for n in
               (get("MM_TRUSTED_HUMANS") or get("MM_OWNER")).split(",") if n.strip()]
    responsible = get("MM_RESPONSIBLE_HUMAN").strip().lstrip("@") or (
        trusted[0] if trusted else "")
    kind = "autonomous" if get("MM_AGENT_KIND") == "autonomous" else "session"
    return responsible, trusted, kind


def secret_warning(bot: str, owner: str) -> str:
    """The own bot's attestation says `via: secret` (RT-6.24 step 2)."""
    return (f"@{bot} was registered with the shared secret (attestation via secret): its owner "
            f"{'@' + owner if owner else '(none)'} is only claimed: other agents count it only "
            f"where Mattermost's native owner agrees, else they treat @{bot} as foreign; for a "
            f"bound bot put an enrollment token (/agent-token create) into MM_REG_SECRET")


def _compare(bot: str, claim: Claim, environ: Mapping[str, str] | None) -> list[str]:
    """The own claim against the session's configuration: '' where they agree."""
    responsible, trusted, kind = own_config(environ)
    out = []
    if claim.responsible != responsible.lower():
        out.append(f"the attestation of @{bot} names {'@' + claim.responsible if claim.responsible else 'no one'} "
                   f"as responsible, this session {'@' + responsible if responsible else 'no one'}: "
                   f"other agents judge @{bot} by the attestation (ADR-001); this session keeps "
                   f"MM_RESPONSIBLE_HUMAN")
    mine = {n.lower() for n in trusted}
    extra = sorted(set(claim.trusted) - mine)
    missing = [n for n in trusted if n.lower() not in claim.trusted]
    if extra:
        out.append(f"the attestation of @{bot} trusts {', '.join('@' + n for n in extra)}, "
                   f"whom this session does not trust")
    if claim.trusted_cut:
        out.append(f"the attestation of @{bot} lists only part of its trusted names "
                   f"(+{claim.trusted_cut} cut): other owners' agents treat @{bot} as foreign")
    elif missing:
        out.append(f"the attestation of @{bot} lacks {len(missing)} of {len(trusted)} trusted "
                   f"names ({', '.join(missing)}), but this session trusts all of them")
    if claim.kind != kind:
        out.append(f"the attestation of @{bot} says kind {claim.kind}, this session {kind}")
    if claim.via == SECRET:
        out.append(secret_warning(bot, claim.responsible))
    return out


def check_own(token: str, *, bot_id: str, username: str,
              keys: Mapping[str, Ed25519PublicKey] | None = None,
              environ: Mapping[str, str] | None = None, now: float | None = None) -> Own:
    """This session's attestation (MM_BOT_ATTESTATION) against the pinned
    keys, its own bot and its configuration. The configuration stays
    authoritative here: a difference is only a warning (issue #83)."""
    if not token:
        return Own(NONE)
    keys = pinned() if keys is None else keys
    if not keys:
        payload = _unverified_payload(token)
        mine = (payload is not None and payload.get("bot_id") == bot_id
                and payload.get("username") == username)
        return Own(UNVERIFIED, "no pinned key", publish=mine)
    claim, why = verify(token, keys, bot_id=bot_id, username=username, now=now)
    if claim is None:
        return Own(INVALID, why, warnings=(
            f"the attestation of @{username} is invalid ({why}): agents with pinned keys "
            f"judge @{username} by its profile; check {KEYS_ENV} and the registrar",))
    return Own(VALID, claim=claim, warnings=tuple(_compare(username, claim, environ)), publish=True)


_started: set[tuple[str, str]] = set()


async def start(client: ChatPort) -> Own:
    """At session start (RT-6.24 steps 1–3): check the own attestation, log
    what differs, and publish it in the bot's own user props unless it is
    invalid. Once per bot and attestation; never raises."""
    token = os.environ.get(ENV, "")
    if not token or not client.token:
        return Own(NONE)
    try:
        me = await client.get_me()
        own = check_own(token, bot_id=me.id, username=me.username)
        key = (me.id, hashlib.sha256(token.encode()).hexdigest())
        if key in _started:
            return own
        for w in own.warnings:
            log.warning("own attestation: %s", w)
        log.info("own attestation: %s", own.label)
        if own.publish:
            from . import lease  # the custom status lease writes the same props

            async with lease.status_lock():
                written = await client.publish_attestation(token)
            log.info("attestation %s in the bot's props", "written" if written else "already")
        _started.add(key)  # a failed write is tried again by the next start
        return own
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — the bus works without it
        log.warning("own attestation not published: %s", exc)
        return Own(NONE)

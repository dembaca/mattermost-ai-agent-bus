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
``start`` checks and logs it; it is not written to the bot's account (a bot's
patch of its own user props is a 403, so no other agent's owner read found it
there, owners.attested), the HELLO card carries it.
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, TypeVar

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import a2a

if TYPE_CHECKING:
    from .port import ChatPort

log = logging.getLogger(__name__)
_T = TypeVar("_T")

ALG = "EdDSA"
TYP = "mm-agent-attestation+jwt"
# The registrar's signed ask mode of a home (RT-6.25 step 8, ADR-005 phase 2)
# and the modes such a record may name here.
MODE_TYP = "mm-agent-mode+jwt"
MODE_MODES = ("direct", "coordinator")
# The registrar's signed mandate of a home (RT-6.26 step 8, ADR-006 phase 2).
MANDATE_TYP = "mm-agent-mandate+jwt"
MANDATE_NOTE_MAX = 200
# The registrar's signed debug setting of a home (RT-6.31, ADR-010 phase 2).
DEBUG_TYP = "mm-agent-debug+jwt"
# The registrar's signed release record (RT-6.32, ADR-011 phase 3): the
# release keys it offers and the version hosts update themselves to: a fixed
# v<x.y.z>, or RELEASE_LATEST, the newest release tag the host's release check
# knows that is not withdrawn (the owner's decision of 2026-11-13).
RELEASE_TYP = "mm-agent-release+jwt"
RELEASE_LATEST = "latest"
RELEASE_SIGNERS_MAX = 16
# One release key line, as mm_release_parse_keys in bin/mm-agent-lib.sh reads
# it: an Ed25519 type, its base64, an optional comment.
RELEASE_KEY = re.compile(r"(ssh-ed25519|sk-ssh-ed25519@openssh\.com)[ \t]+([A-Za-z0-9+/]+={0,2})([ \t].*)?")
_RELEASE_VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
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
_ID = re.compile(r"[a-z0-9]{26}")  # a Mattermost id

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


def expires_in(token: str, now: float | None = None) -> float:
    """Seconds until a record's `exp`, read without checking it (a cache
    bound; verify decides whether it counts); 0 when it has none."""
    parts = token.split(".") if isinstance(token, str) else []
    payload = _json(parts[1]) if len(parts) == 3 else None
    exp = _int(payload.get("exp")) if payload else None
    return 0.0 if exp is None else exp - (time.time() if now is None else now)


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
    claim, why = _verify_jws(token, keys, typ=TYP, now=now,
                             parse=lambda payload, kid: _claim(payload, kid) or "bad claims")
    if claim is None:
        return None, why
    if claim.bot_id != bot_id:
        return None, "bot_id mismatch"
    if claim.username != username:
        return None, "username mismatch"
    return claim, ""


def _verify_jws(token: object, keys: Mapping[str, Ed25519PublicKey], *, typ: str, now: float,
                parse: Callable[[Mapping[str, Any], str], _T | str]) -> tuple[_T | None, str]:
    """The checks every registrar-signed compact JWS of type `typ` gets: its
    shape, the header (EdDSA, `typ`, no `crit`, a pinned kid), the Ed25519
    signature, a JSON payload without duplicate keys whose iat and exp are
    ints with exp > iat, `parse` (the typed claims, or why not), and iat/exp
    against now with SKEW_SEC. (claims, '') or (None, why)."""
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
    if header.get("typ") != typ:
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
    if payload is None:
        return None, "bad claims"
    iat, exp = _int(payload.get("iat")), _int(payload.get("exp"))
    if iat is None or exp is None or exp <= iat:
        return None, "bad claims"
    claims = parse(payload, kid)
    if isinstance(claims, str):
        return None, claims
    if exp + SKEW_SEC <= now:
        return None, "expired"
    if iat - SKEW_SEC > now:
        return None, "issued in the future"
    return claims, ""


# --- the ask mode of a home (RT-6.25 step 8, ADR-005 phase 2) ------------------

@dataclass(frozen=True)
class ModeRecord:
    """A verified ask-mode record: the mode a human set for a home with the
    registrar's `/agent-ask-mode`, and who set it (name lower case)."""

    channel_id: str
    mode: str
    set_by: str
    set_by_id: str
    set_at_ms: int  # when the human set it, unix ms
    iat: int
    exp: int
    kid: str


def _mode_record(payload: Mapping[str, Any], kid: str) -> ModeRecord | str:
    """The record's claims, typed; why not when one is missing or malformed."""
    channel_id, set_by_id = payload.get("channel_id"), payload.get("set_by_id")
    mode, set_by = payload.get("mode"), _handle(payload.get("set_by"))
    set_at = _int(payload.get("set_at_ms"))
    if not (isinstance(channel_id, str) and _ID.fullmatch(channel_id)
            and isinstance(set_by_id, str) and _ID.fullmatch(set_by_id)
            and isinstance(mode, str) and set_by is not None and set_at is not None and set_at > 0):
        return "bad claims"
    if mode not in MODE_MODES:
        return "unsupported mode"
    return ModeRecord(channel_id=channel_id, mode=mode, set_by=set_by, set_by_id=set_by_id,
                      set_at_ms=set_at, iat=payload["iat"], exp=payload["exp"], kid=kid)


def verify_mode(token: object, keys: Mapping[str, Ed25519PublicKey], *, channel_id: str,
                now: float | None = None) -> tuple[ModeRecord | None, str]:
    """The registrar's ask-mode record for the home `channel_id` when it is
    sound (RT-6.25 step 8): a pinned key signed it, EdDSA and the mode type
    (an attestation is refused here, a record by verify), not expired, issued
    not later than now (both with SKEW_SEC), for this home, and a mode the bus
    has (`delegated` is not built). Else (None, why). Never raises."""
    try:
        record, why = _verify_jws(token, keys, typ=MODE_TYP,
                                  now=time.time() if now is None else now, parse=_mode_record)
    except Exception as exc:  # noqa: BLE001 — strict: malformed is invalid, never an error
        log.debug("ask mode record not verified: %s", type(exc).__name__)
        return None, "malformed"
    if record is not None and record.channel_id != channel_id:
        return None, "channel_id mismatch"
    return record, why


# --- the mandate of a home (RT-6.26 step 8, ADR-006 phase 2) -------------------

_DAY = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class MandateRecord:
    """A verified mandate record: what a human set with the registrar's
    `/agent-mandate` in the home `channel_id`: the coordinator of the channel
    `home` (a channel id) gives tasks and priorities for them there. `until`
    is `YYYY-MM-DD` or empty, `note` free text."""

    channel_id: str
    home: str
    set_by: str
    set_by_id: str
    set_at_ms: int  # when the human set it, unix ms
    until: str
    note: str
    iat: int
    exp: int
    kid: str


def _mandate_record(payload: Mapping[str, Any], kid: str) -> MandateRecord | str:
    """The record's claims, typed; why not when one is missing or malformed."""
    channel_id, home = payload.get("channel_id"), payload.get("home")
    set_by_id, set_by = payload.get("set_by_id"), _handle(payload.get("set_by"))
    set_at, until, note = _int(payload.get("set_at_ms")), payload.get("until", ""), payload.get("note", "")
    if not (isinstance(channel_id, str) and _ID.fullmatch(channel_id)
            and isinstance(home, str) and _ID.fullmatch(home)
            and isinstance(set_by_id, str) and _ID.fullmatch(set_by_id)
            and set_by is not None and set_at is not None and set_at > 0
            and isinstance(until, str) and (not until or _DAY.fullmatch(until))
            and isinstance(note, str) and len(note) <= MANDATE_NOTE_MAX):
        return "bad claims"
    return MandateRecord(channel_id=channel_id, home=home, set_by=set_by,
                         set_by_id=set_by_id, set_at_ms=set_at, until=until, note=note,
                         iat=payload["iat"], exp=payload["exp"], kid=kid)


def verify_mandate(token: object, keys: Mapping[str, Ed25519PublicKey], *, channel_id: str,
                   now: float | None = None) -> tuple[MandateRecord | None, str]:
    """The registrar's mandate record for the home `channel_id` when it is
    sound (RT-6.26 step 8): a pinned key signed it, EdDSA and the mandate type
    (a mode record or an attestation is refused here), not expired, issued not
    later than now (both with SKEW_SEC), for this home. Else (None, why). Never
    raises."""
    try:
        record, why = _verify_jws(token, keys, typ=MANDATE_TYP,
                                  now=time.time() if now is None else now,
                                  parse=_mandate_record)
    except Exception as exc:  # noqa: BLE001 — strict: malformed is invalid, never an error
        log.debug("mandate record not verified: %s", type(exc).__name__)
        return None, "malformed"
    if record is not None and record.channel_id != channel_id:
        return None, "channel_id mismatch"
    return record, why


# --- the debug setting of a home (RT-6.31, ADR-010 phase 2) ------------------------

@dataclass(frozen=True)
class DebugRecord:
    """A verified debug record: the categories a human switched on for a home
    with the registrar's `/agent-debug`, in canonical order, until when, and
    who set them (name lower case)."""

    channel_id: str
    categories: tuple[str, ...]
    set_by: str
    set_by_id: str
    set_at_ms: int
    until_ms: int  # the setting's end, unix ms; exp is the signature's freshness
    iat: int
    exp: int
    kid: str


def _debug_record(payload: Mapping[str, Any], kid: str) -> DebugRecord | str:
    """The record's claims, typed; why not when one is missing or malformed.
    The categories are checked here, not left to the registrar: a non-empty
    list of distinct known names (`full` is the registrar's word, never a claim)."""
    channel_id, set_by_id = payload.get("channel_id"), payload.get("set_by_id")
    set_by, cats = _handle(payload.get("set_by")), payload.get("categories")
    set_at, until = _int(payload.get("set_at_ms")), _int(payload.get("until_ms"))
    if not (isinstance(channel_id, str) and _ID.fullmatch(channel_id)
            and isinstance(set_by_id, str) and _ID.fullmatch(set_by_id)
            and set_by is not None and set_at is not None and set_at > 0
            and until is not None and until > 0):
        return "bad claims"
    if (not isinstance(cats, list) or not cats or not all(isinstance(c, str) for c in cats)
            or len(set(cats)) != len(cats) or not set(cats) <= set(a2a.DEBUG_CATEGORIES)):
        return "bad categories"
    return DebugRecord(channel_id=channel_id,
                       categories=tuple(c for c in a2a.DEBUG_CATEGORIES if c in cats),
                       set_by=set_by, set_by_id=set_by_id, set_at_ms=set_at, until_ms=until,
                       iat=payload["iat"], exp=payload["exp"], kid=kid)


def verify_debug(token: object, keys: Mapping[str, Ed25519PublicKey], *, channel_id: str,
                 now: float | None = None) -> tuple[DebugRecord | None, str]:
    """The registrar's debug record for the home `channel_id` when it is sound
    (RT-6.31 step 2): a pinned key signed it, EdDSA and the debug type (a mode
    or mandate record is refused here), not expired, issued not later than now,
    for this home, and its setting not ended (`until_ms`), each with SKEW_SEC.
    Else (None, why). Never raises."""
    now = time.time() if now is None else now
    try:
        record, why = _verify_jws(token, keys, typ=DEBUG_TYP, now=now, parse=_debug_record)
    except Exception as exc:  # noqa: BLE001 — strict: malformed is invalid, never an error
        log.debug("debug record not verified: %s", type(exc).__name__)
        return None, "malformed"
    if record is not None and record.channel_id != channel_id:
        return None, "channel_id mismatch"
    if record is not None and record.until_ms + SKEW_SEC * 1000 <= now * 1000:
        return None, "ended"
    return record, why


# --- the release record (RT-6.32, ADR-011 phase 3) ------------------------------

@dataclass(frozen=True)
class ReleaseRecord:
    """A verified release record: the release keys the registrar offers
    (`<type> <base64>` each, in its order) and the version it names: x.y.z,
    RELEASE_LATEST, or '' for keys only."""

    signers: tuple[str, ...]
    version: str  # x.y.z without the v, 'latest', or ''
    set_at_ms: int
    iat: int
    exp: int
    kid: str


def release_key(line: object) -> str | None:
    """`<type> <base64>` of one release key line when it is one: the shape of
    mm_release_parse_keys, and a base64 that decodes to an SSH key blob of
    that type, re-encoded in standard base64 so one key has one spelling. None
    otherwise. `set_at_ms` of the record is the registrar's start time and keys
    nothing here."""
    if not isinstance(line, str) or len(line) > 1024 or any(c < " " and c != "\t" for c in line):
        return None
    m = RELEASE_KEY.fullmatch(line.strip())
    if m is None:
        return None
    try:
        blob = base64.b64decode(m[2], validate=True)
    except (binascii.Error, ValueError):
        return None
    name = m[1].encode()
    if blob[:4] != len(name).to_bytes(4, "big") or blob[4:4 + len(name)] != name:
        return None
    return f"{m[1]} {base64.b64encode(blob).decode('ascii')}"  # canonical: one spelling per key


def _release_record(payload: Mapping[str, Any], kid: str) -> ReleaseRecord | str:
    """The record's claims, typed; why not when one is missing or malformed:
    a non-empty list of distinct release keys, an optional `v<x.y.z>` or
    `latest` (that lowercase word exactly, RT-6.32 step 2)."""
    signers, version, set_at = payload.get("signers"), payload.get("version"), _int(payload.get("set_at_ms"))
    if set_at is None or set_at <= 0:
        return "bad claims"
    if not isinstance(signers, list) or not 0 < len(signers) <= RELEASE_SIGNERS_MAX:
        return "bad signers"
    keys = [release_key(s) for s in signers]
    if any(k is None for k in keys) or len(set(keys)) != len(keys):
        return "bad signers"
    if version is not None and not (isinstance(version, str) and (
            version == RELEASE_LATEST or _RELEASE_VERSION.fullmatch(version))):
        return "bad version"
    named = RELEASE_LATEST if version == RELEASE_LATEST else (version or "")[1:]
    return ReleaseRecord(signers=tuple(k for k in keys if k), version=named,
                         set_at_ms=set_at, iat=payload["iat"], exp=payload["exp"], kid=kid)


def verify_release(token: object, keys: Mapping[str, Ed25519PublicKey],
                   now: float | None = None) -> tuple[ReleaseRecord | None, str]:
    """The registrar's release record when it is sound (RT-6.32 step 2): a
    pinned key signed it, EdDSA and the release type (any other record is
    refused here), not expired and issued not later than now with SKEW_SEC,
    and its claims in shape. Else (None, why). Never raises."""
    now = time.time() if now is None else now
    try:
        return _verify_jws(token, keys, typ=RELEASE_TYP, now=now, parse=_release_record)
    except Exception as exc:  # noqa: BLE001 — strict: malformed is invalid, never an error
        log.debug("release record not verified: %s", type(exc).__name__)
        return None, "malformed"


def _unverified_payload(token: str) -> dict[str, Any] | None:
    """The payload as it is, unchecked: only to tell whether an attestation
    that cannot be verified here names this bot at all."""
    parts = token.split(".") if len(token) <= MAX_LEN else []
    return _json(parts[1]) if len(parts) == 3 and all(_SEGMENT.fullmatch(p) for p in parts) else None


# --- the own session (RT-6.24 steps 1–2) ---------------------------------------

@dataclass(frozen=True)
class Own:
    """What this session's own attestation is worth, and what to warn about."""

    status: str  # VALID, INVALID, UNVERIFIED or NONE
    reason: str = ""
    claim: Claim | None = None
    warnings: tuple[str, ...] = ()
    publish: bool = False  # worth carrying on the HELLO card

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
    """At session start (RT-6.24 steps 1–2): check the own attestation and log
    what differs. It is not written onto the bot's account: Mattermost refuses
    a bot's patch of its own user props with a 403 (measured 2026-11-05), so
    the only carrier left is the HELLO card (a2a). Once per bot and
    attestation; never raises."""
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
        _started.add(key)
        return own
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — the bus works without it
        log.warning("own attestation not checked: %s", exc)
        return Own(NONE)

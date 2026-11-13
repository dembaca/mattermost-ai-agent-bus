"""The registrar's release record, read on the host (ADR-011 phase 3, RT-6.32
in docs/arc42/06-runtime-view.md).

`GET <registrar>/register/v1/release` answers `{"record": "<compact JWS>"}`
or 404 with exactly `{"error":"no release set"}`: no target and no keys
offered. The record (`typ` mm-agent-release+jwt, the registrar's key) offers
the release keys (`signers`) and may name the version hosts update themselves
to (`version`). It counts only verified with the pinned registrar keys
(MM_REGISTRAR_KEYS, attest.verify_release).

The MCP server never reads it: the host copy's installer (`--auto`, at the
end of a corral session) and the release check run

    <release>/mcp/.venv/bin/python -I -m mattermost_ai_agent_bus.selfupdate

with a clean environment and parse its output, fixed lines that bash checks
again (mm_registrar_release in bin/mm-agent-lib.sh):

    STATUS=ok|none|failed|nokeys
    WHY=<printable text, at most 200 characters>
    KID=<the registrar key's kid>          (ok)
    VERSION=<x.y.z>                        (ok, when the record names one)
    SIGNER=<type> <base64>                 (ok, one per key)

Any other 404, a timeout, a redirect, an answer over RECORD_MAX and a record
that does not verify are `failed`, which changes nothing on the host.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Mapping

import httpx

from . import attest

NONE_ERROR = "no release set"
RECORD_MAX = 8192
TIMEOUT = 5.0
WHY_MAX = 200


class Failed(Exception):
    """The record could not be read: why, said once on the host."""


def release_url(environ: Mapping[str, str]) -> str | None:
    """Where the registrar publishes its release record, found like
    client.modes_url: MM_REGISTER_URL `/agents` → `/release`, else
    MM_CHAT_URL + `/register/v1/release`. None without either, or for a URL
    that is no plain http(s) one."""
    reg = environ.get("MM_REGISTER_URL", "").rstrip("/")
    chat = (environ.get("MM_CHAT_URL") or environ.get("MATTERMOST_URL") or "").rstrip("/")
    root = reg.removesuffix("/agents") if reg else (f"{chat}/register/v1" if chat else "")
    if not re.fullmatch(r"https?://[A-Za-z0-9.:\[\]-]+(/[A-Za-z0-9._~%+-]+)*", root):
        return None
    return f"{root}/release"


def fetch(url: str, timeout: float = TIMEOUT, transport: httpx.BaseTransport | None = None) -> str | None:
    """The record's JWS; None for the registrar's own `no release set`. No
    Authorization header, no redirect, at most RECORD_MAX bytes. Raises
    Failed for anything else."""
    body = bytearray()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as http, \
                http.stream("GET", url, headers={"Accept": "application/json"}) as resp:
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > RECORD_MAX:
                    raise Failed(f"answer over {RECORD_MAX} bytes")
            status = resp.status_code
    except httpx.HTTPError as exc:
        raise Failed(f"not read ({type(exc).__name__})") from None
    try:
        data = json.loads(bytes(body).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        data = None
    if status == 404:
        if data == {"error": NONE_ERROR}:
            return None
        raise Failed("the registrar has no release endpoint (a 404 without `no release set`)")
    if status != 200:
        raise Failed(f"HTTP {status}")
    record = data.get("record") if isinstance(data, dict) else None
    if not isinstance(record, str) or not record:
        raise Failed("the answer holds no record")
    return record


def read(environ: Mapping[str, str] | None = None, now: float | None = None,
         ) -> tuple[str, attest.ReleaseRecord | None, str]:
    """(status, record, why): `ok` with the verified record, `none` (no release
    set), `nokeys` (no registrar key pinned: nothing is read), `failed`."""
    environ = os.environ if environ is None else environ
    keys = attest.pinned(environ.get(attest.KEYS_ENV, ""))
    if not keys:
        return "nokeys", None, "no registrar keys pinned (MM_REGISTRAR_KEYS): nothing taken from the registrar"
    url = release_url(environ)
    if url is None:
        return "failed", None, "no registrar URL (MM_REGISTER_URL or MM_CHAT_URL)"
    try:
        token = fetch(url)
    except Failed as exc:
        return "failed", None, f"the registrar's release record: {exc}"
    if token is None:
        return "none", None, "the registrar names no release (no release set)"
    record, why = attest.verify_release(token, keys, now=time.time() if now is None else now)
    if record is None:
        return "failed", None, f"the registrar's release record refused: {why}"
    return "ok", record, ""


def _clean(text: str) -> str:
    return "".join(c for c in text if " " <= c <= "~")[:WHY_MAX]


def render(status: str, record: attest.ReleaseRecord | None, why: str) -> str:
    lines = [f"STATUS={status}", f"WHY={_clean(why)}"]
    if record is not None:
        lines.append(f"KID={_clean(record.kid)}")
        if record.version:
            lines.append(f"VERSION={record.version}")
        lines += [f"SIGNER={s}" for s in record.signers]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    """Read and verify the record once; print the lines above; exit 0."""
    del argv
    try:
        status, record, why = read()
    except Exception as exc:  # noqa: BLE001 — a failed read changes nothing
        status, record, why = "failed", None, f"not read ({type(exc).__name__})"
    sys.stdout.write(render(status, record, why))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

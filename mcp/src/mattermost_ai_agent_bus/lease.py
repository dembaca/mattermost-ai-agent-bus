"""Presence lease and coordinator crown (RT-6.6 in docs/arc42/06-runtime-view.md).

The rules live in one place, bin/mm-agent-lease.sh, shared with the hooks and
the CLI. This module runs it: every MM_HEARTBEAT_SEC (default 300 s) while the
session lives, faster while a take is pending. A session that dies stops
renewing, so its status — and a crown — run out on their own.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from . import session_file

if TYPE_CHECKING:
    from .client import MattermostClient

log = logging.getLogger(__name__)

LEASE_SCRIPT = Path(__file__).resolve().parents[3] / "bin" / "mm-agent-lease.sh"
CROWN = "👑"
PENDING_POLL_SEC = 30.0


def heartbeat_sec() -> float:
    try:
        return max(30.0, float(os.environ.get("MM_HEARTBEAT_SEC", "300")))
    except ValueError:
        return 300.0


def _coord(user_id: str) -> dict[str, int]:
    f = session_file.state_dir() / f"coord-{user_id}.env"
    out: dict[str, int] = {}
    try:
        for line in f.read_text().splitlines():
            k, _, v = line.partition("=")
            if v.isdigit():
                out[k] = int(v)
    except OSError:
        pass
    return out


def crown_held(user_id: str) -> bool:
    return _coord(user_id).get("CROWN_UNTIL", 0) > time.time()


def take_pending(user_id: str) -> bool:
    return _coord(user_id).get("PENDING", 0) == 1


def with_crown(message: str, user_id: str) -> str:
    """Prefix the coordinator's posts: bot posts never show a custom status."""
    if user_id and crown_held(user_id) and not message.startswith(CROWN):
        return f"{CROWN} {message}"
    return message


async def _user_id(client: MattermostClient) -> str:
    if client.session:
        return client.session.user_id
    return os.environ.get("MM_BOT_USER_ID") or (await client.get_me())["id"]


def _env(client: MattermostClient, user_id: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update(
        MM_CHAT_URL=client.base_url,
        MM_BOT_TOKEN=client.token or "",
        MM_BOT_USER_ID=user_id,
        MM_TEAM=client.team_name,
        MM_CHANNEL=client.channel_name,
    )
    if client.project_channel_name:
        env["MM_PROJECT_CHANNEL"] = client.project_channel_name
    return env


async def run(client: MattermostClient, command: str) -> str:
    """Run one mm-agent-lease.sh command; '' when it cannot run."""
    if not client.token:
        return ""
    user_id = await _user_id(client)
    proc = await asyncio.create_subprocess_exec(
        "bash", str(LEASE_SCRIPT), command,
        env=_env(client, user_id),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        proc.kill()
        return ""
    return out.decode().strip()


async def heartbeat(get_client: Callable[[], Awaitable[MattermostClient | None]]) -> None:
    """Renew the lease until cancelled. Never raises: presence is cosmetic."""
    while True:
        delay = heartbeat_sec()
        try:
            client = await get_client()
            if client is not None and client.token:
                await run(client, "tick")
                if take_pending(await _user_id(client)):
                    delay = PENDING_POLL_SEC
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.debug("lease heartbeat skipped: %s", exc)
        await asyncio.sleep(delay)

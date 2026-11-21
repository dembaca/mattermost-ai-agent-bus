"""Whether this process runs inside a corral sandbox, and why nothing
self-registers there (RT-6.18). client.py re-exports these."""

from __future__ import annotations

from .config import env

# RT-6.18, RT-6.10: what marks a sandbox corral set up. There the host's
# session hook registered the bot and keeps its token, and the bus reaches
# the agent through the relay; a bot registered from inside would put a token
# into the sandbox. CORRAL_SANDBOX is corral's own, the others its preStart
# contribution. Not CORRAL_SESSION_ID: corral's host hooks have it as well.
# Defense in depth only: an agent that clears them gets past it; the real
# boundary is a sandbox that cannot read the registration secret.
SANDBOX_MARKERS = ("CORRAL_SANDBOX", "MM_BUS_MCP_ENDPOINT", "MM_BUS_ENDPOINT")


def corral_sandbox() -> str:
    """The first marker of a corral sandbox that is set; '' outside one."""
    return next((m for m in SANDBOX_MARKERS if env(m).strip()), "")


def sandbox_refusal(marker: str) -> str:
    """Why nothing self-registers inside a corral sandbox (RT-6.18)."""
    return (f"self-registration refused: this runs inside a corral sandbox ({marker} is set). "
            "There the host's session hook registers the bot and its token stays on the host; "
            "the bus tools reach it through the relay (RT-6.18). No bus tools in this session "
            "means the harness does not load the bus plugin: tell your human "
            "(adapters/opencode.md, adapters/pi.md).")

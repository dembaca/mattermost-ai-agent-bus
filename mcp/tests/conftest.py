"""Shared test setup."""

import os

import pytest

# Every variable from_env() reads. Tests must not inherit these from the shell:
# under a host-managed session the real ones are present, which silently changed
# what from_env() returned and made assertions pass or fail by accident.
_BUS_ENV = (
    "MM_CHAT_URL",
    "MATTERMOST_URL",
    "MM_REGISTER_URL",
    "MM_REG_SECRET",
    "MM_BOT_TOKEN",
    "MATTERMOST_TOKEN",
    "MM_REG_BOT_NAME",
    "MM_BOT_USERNAME",
    "MM_AGENT_PREFIX",
    "MM_BOT_USER_ID",
    "MM_BOT_ATTESTATION",
    "MM_REGISTRAR_KEYS",
    "MM_PROPS_PREFIX",
    "MM_TEAM",
    "MM_TEAM_ID",
    "MM_CHANNEL",
    "MM_CHANNEL_ID",
    "MM_PROJECT_CHANNEL",
    "MM_PROJECT_CHANNEL_ID",
    "MM_BUS_ENDPOINT",
    "MM_BUS_MCP_ENDPOINT",
    "MM_BUS_KEY",
    "MM_BUS_UNAVAILABLE",
    "MM_WORKSPACE",
)


@pytest.fixture(autouse=True)
def _isolate_bus_env(monkeypatch, tmp_path):
    """Start every test from a clean bus environment and a private state dir."""
    for name in _BUS_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    # Only the host process of a keyless session sets it (RT-6.18).
    from mattermost_ai_agent_bus import session_file

    monkeypatch.setattr(session_file, "_hosted_dir", None)
    # What the attestation code remembers per process (RT-6.24).
    from mattermost_ai_agent_bus import askroute, attest, lease, life, mandate, owners, receipt, usage

    attest._started.clear()
    usage.reset()  # the last usage report of the session (ADR-008)
    life.reset()  # the agent's last sign (ADR-007 amendment step 1)
    # ADR-007 phase E: receipts owed and watched, the holder seen, a takeover.
    receipt.reset()
    monkeypatch.setattr(receipt, "pushes", lambda: False)
    askroute._receipt_looked.clear()
    for name, value in (("_holder_id", ""), ("_holder_name", ""), ("_holder_marked", False),
                        ("_took_from", ""), ("_crown_since", 0.0), ("_ousted", None)):
        monkeypatch.setattr(lease, name, value)
    owners._noted.clear()
    # What the mandate reader keeps per process (RT-6.26).
    for kept in (mandate._reads, mandate._fetched, mandate._judged, mandate._noted):
        kept.clear()
    yield
    # monkeypatch restores the real environment itself; session_start also writes
    # into os.environ, so make sure nothing leaks into the next test.
    for name in _BUS_ENV:
        os.environ.pop(name, None)

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
    yield
    # monkeypatch restores the real environment itself; session_start also writes
    # into os.environ, so make sure nothing leaks into the next test.
    for name in _BUS_ENV:
        os.environ.pop(name, None)

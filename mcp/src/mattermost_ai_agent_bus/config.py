"""The host's settings as the harness passes them: environment variables,
a plugin manifest's unexpanded ``${VAR}`` read as unset. client.py
re-exports env."""

from __future__ import annotations

import os
import re

_PLACEHOLDER_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


def env(name: str, default: str = "") -> str:
    """Read an env var, treating an unexpanded ``${VAR}`` as unset.

    A plugin manifest declares env as ``"MM_PROJECT_CHANNEL": "${MM_PROJECT_CHANNEL}"``.
    When the variable is not set in the environment, the placeholder can arrive
    verbatim — and a literal "${MM_PROJECT_CHANNEL}" is not a channel name.
    """
    value = os.environ.get(name, "")
    if not value or _PLACEHOLDER_RE.match(value):
        return default
    return value

"""Whom the bot's profile says it trusts, against whom the bus trusts (RT-6.8).

The registrar writes the trusted names a registration carries into the bot's
description (``trusts @a, @b``), but not necessarily all of them: it caps the
list and keeps human accounts only. The bus trusts everyone in
MM_TRUSTED_HUMANS, so the profile can under-report whom the agent trusts. The
bus assumes nothing about the registrar's cap: it compares what the profile
actually carries. bin/mm-agent-lib.sh (mm_warn_profile_trusted) does the same
for host sessions.
"""

from __future__ import annotations

import re


def listed(description: str) -> set[str]:
    """The names after ``trusts @`` in a bot's description."""
    m = re.search(r"trusts @([^·]*)", description or "")
    return {n.strip().lower() for n in re.split(r",\s*@", m[1])} if m else set()


def warning(bot: str, sent: list[str], description: str | None) -> str:
    """What to tell about the profile of `bot`, '' when it carries every name
    sent, or when it could not be read (`description` None)."""
    if description is None:
        return ""
    missing = [n for n in sent if n.lower() not in listed(description)]
    if not missing:
        return ""
    return (f"the profile of @{bot} lacks {len(missing)} of {len(sent)} trusted names "
            f"({', '.join(missing)}), but the bus trusts all of them: the registrar likely "
            f"writes only part of the list (its maxTrusted), human accounts only")

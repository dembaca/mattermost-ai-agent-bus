"""Whom the bot's profile says it trusts, against whom the bus trusts (RT-6.8).

The registrar writes the trusted names a registration carries into the bot's
description (``trusts @a, @b``), but not necessarily all of them: it caps the
list and keeps human accounts only. The bus trusts everyone in
MM_TRUSTED_HUMANS, so the profile can under-report whom the agent trusts. The
bus assumes nothing about the registrar's cap: it compares what the profile
actually carries. bin/mm-agent-lib.sh (mm_warn_profile_trusted) does the same
for host sessions.

The same description names the bot's owner (``for @owner``): whose agent it
is, which ADR-001 compares with whom this session trusts (owners.py). Nothing
beyond ``for @…`` and ``trusts @…`` is read; a ``+N`` in the trusted list says
the registrar cut it (ADR-003, WP3).
"""

from __future__ import annotations

import re


_CUT = re.compile(r"\+\d+")


def _trusts(description: str | None) -> str:
    m = re.search(r"trusts @([^·]*)", description or "")
    return m[1] if m else ""


def listed(description: str | None) -> set[str]:
    """The names after ``trusts @`` in a bot's description; a ``+N`` that
    marks a cut list names nobody."""
    names = re.split(r"[,\s]+@?", _CUT.sub(" ", _trusts(description)))
    return {n.lower() for n in names if re.fullmatch(r"[\w.-]+", n)}


def cut(description: str | None) -> bool:
    """The registrar wrote only part of the trusted list, marked ``+N`` (WP3)."""
    return _CUT.search(_trusts(description)) is not None


def owner(description: str | None) -> str:
    """The owner after ``for @`` in a bot's description, lower case; '' without one."""
    m = re.search(r"(?:^|·)\s*for @([^\s·,]+)", description or "")
    return m[1].lower() if m else ""


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

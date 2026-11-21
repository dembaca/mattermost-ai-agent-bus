"""Ids and names checked before they go into a chat API path (#81).

The shape checks of the adapter's ids and names, kept apart from client.py
so an entry point checks what an agent or hook passes without the adapter;
client.py re-exports them."""

from __future__ import annotations

import re

_ID = re.compile(r"[a-z0-9]{26}")
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


class InvalidId(ValueError):
    """An id or name that must not go into a Mattermost API path (#81)."""


def mm_id(value: object, what: str) -> str:
    """`value` if it is a Mattermost id (26 lowercase letters and digits), else
    InvalidId: nothing an agent or hook passes reaches an API path unchecked."""
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise InvalidId(f"invalid {what} id")
    return value


def mm_name(value: object, what: str) -> str:
    """`value` if it is a safe team, channel, user or agent name for an API
    path: letters, digits, '.', '_', '-', no '..', at most 64; else InvalidId.
    A DM channel's name "<id>__<id>" passes."""
    if not isinstance(value, str) or not _NAME.fullmatch(value) or ".." in value:
        raise InvalidId(f"invalid {what} name")
    return value

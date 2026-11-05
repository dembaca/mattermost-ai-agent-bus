"""Mattermost's symbols for the bus's meanings (arc42 §5.1).

The core speaks in meanings (model.Mark, Answer, StatusMark) and gets the
characters from the adapter through the port; this is the only Python module
that knows which emoji or shortcode stands for which meaning. The characters
are written as escapes with their name beside them, so no source line carries
an emoji. What the channel shows is unchanged, byte for byte.
"""

from __future__ import annotations

import re

from .model import Answer, Head, Mark, StatusMark

_SELECTOR = "️"  # VARIATION SELECTOR-16: emoji presentation
_KEYCAP = "⃣"    # COMBINING ENCLOSING KEYCAP

_MARKS: dict[Mark, str] = {
    Mark.CROWN: "\U0001F451",         # crown
    Mark.ASK: "\U0001F64B",           # person raising hand
    Mark.QUESTION: "❓",          # red question mark
    Mark.REMINDER: "\U0001F514",      # bell
    Mark.BUS: "\U0001F68C",           # bus
    Mark.INBOX: "\U0001F4E8",         # incoming envelope
    Mark.SIGNOFF: "✋",           # raised hand
    Mark.LOCK: "\U0001F510",          # locked with key
    Mark.DESKTOP: "\U0001F5A5" + _SELECTOR,  # desktop computer
    Mark.ALLOWED: "✅",           # white heavy check mark
    Mark.DENIED: "⛔",            # no entry
    Mark.YES: "\U0001F44D",           # thumbs up
    Mark.NO: "\U0001F44E",            # thumbs down
    Mark.ONE: "1" + _SELECTOR + _KEYCAP,
    Mark.TWO: "2" + _SELECTOR + _KEYCAP,
    Mark.THREE: "3" + _SELECTOR + _KEYCAP,
    Mark.FOUR: "4" + _SELECTOR + _KEYCAP,
}
CHOICE_MARKS = (Mark.ONE, Mark.TWO, Mark.THREE, Mark.FOUR)

# Reaction names (shortcodes) that answer an ask, and the one that closes it.
_YES = frozenset({"+1", "thumbsup", "white_check_mark", "heavy_check_mark", "ballot_box_with_check"})
_NO = frozenset({"-1", "thumbsdown", "x", "no_entry", "no_entry_sign"})
_CHOICES = ("one", "two", "three", "four")
_DONE = "white_check_mark"

# The custom status emoji (a shortcode) of each state.
_STATUS: dict[StatusMark, str] = {
    StatusMark.WORKING: "hammer_and_wrench",
    StatusMark.LISTENING: "ear",
    StatusMark.IDLE: "zzz",
    StatusMark.BUS: "satellite_antenna",
    StatusMark.CROWN: "crown",
}

_CROWN = _MARKS[Mark.CROWN]
_ASK, _QUESTION = _MARKS[Mark.ASK], _MARKS[Mark.QUESTION]
# A leading mark an agent put in front of an answer (`<thumbs up> = yes`): the
# server gives the marks by position (RT-6.11 step 1).
_TONE = "[\U0001F3FB-\U0001F3FF]?"
_ANSWER_MARK = re.compile(
    rf"^\s*(?:(?:{_MARKS[Mark.YES]}|{_MARKS[Mark.NO]}){_TONE}|✅|❌|✔{_SELECTOR}?"
    rf"|☑{_SELECTOR}?|[1-4]{_SELECTOR}?{_KEYCAP})\s*[=:\-–]?\s*")
_ASK_LINK = re.compile(rf"{_ASK}\s*asked\s+(?:[a-z0-9]{{26}}|\[[^\]]*\]\([^)\s]*/[a-z0-9]{{26}}\))\s*")
_ASK_HEAD = re.compile(rf"({_ASK}|{_QUESTION})\s*@([\w.-]*\w)")
_STATUS_HEAD = re.compile(rf"^({_CROWN}\s*)?(`?)([A-Z]{{2,11}})(`?)(?![A-Za-z])")
_MANDATE_CROWN = rf"(?:{_CROWN}|:crown:)"


def symbol(mark: Mark) -> str:
    """The characters Mattermost shows for a meaning."""
    return _MARKS[mark]


def with_crown(message: str) -> str:
    """The message with the crown in front, once."""
    return message if message.startswith(_CROWN) else f"{_CROWN} {message}"


def strip_crown(message: str) -> str:
    """The message without a leading crown (and the space after it)."""
    return message.removeprefix(_CROWN).lstrip(" ") if message.startswith(_CROWN) else message


def parse_head(message: str) -> Head:
    """What a post's text opens with: the crown, an ask head (`<hand> @name`),
    a question head, an ask link line."""
    bare = message.strip().removeprefix(_CROWN).lstrip()
    return Head(crowned=message.lstrip().startswith(_CROWN), body=bare,
                ask=bare.startswith(_ASK), ask_at=bare.startswith(f"{_ASK} @"),
                ask_link=_ASK_LINK.fullmatch(bare) is not None)


def ask_head(message: str) -> tuple[str, str] | None:
    """The first ask head in the text, (kind, name): `ask` or `question`."""
    m = _ASK_HEAD.search(message)
    return None if m is None else ("question" if m[1] == _QUESTION else "ask", m[2])


def status_head(message: str) -> re.Match[str] | None:
    """A status post's bus word at the very start, after an optional crown."""
    return _STATUS_HEAD.match(message)


def mandate_crown() -> str:
    """The regex alternation for the crown in a pinned mandate post (RT-6.26)."""
    return _MANDATE_CROWN


def status_emoji(mark: StatusMark) -> str:
    return _STATUS[mark]


def status_of(emoji: str) -> StatusMark | None:
    return next((m for m, e in _STATUS.items() if e == emoji), None)


def done_reaction() -> str:
    """The reaction that closes an ask."""
    return _DONE


def is_done(emoji: str) -> bool:
    return emoji == _DONE


def answer_of(emoji: str) -> Answer | None:
    """yes / no for the agreed reaction, None for anything else (a remark)."""
    if emoji in _YES:
        return Answer.YES
    return Answer.NO if emoji in _NO else None


def choice_of(emoji: str) -> int | None:
    """The 1-based alternative a keycap reaction picks, None for anything else."""
    return _CHOICES.index(emoji) + 1 if emoji in _CHOICES else None


def clean_answer_mark(text: str) -> str:
    """The text without a leading answer mark."""
    return _ANSWER_MARK.sub("", text, count=1)


def answer_marks(count: int) -> list[str]:
    """The characters by position for `count` answers: yes (the go) and no for
    two, the keycaps for three or four."""
    if count == 2:
        return [_MARKS[Mark.YES], _MARKS[Mark.NO]]
    return [_MARKS[m] for m in CHOICE_MARKS[:count]]


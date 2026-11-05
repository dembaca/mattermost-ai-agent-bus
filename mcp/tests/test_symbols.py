"""One meaning, one name (model.Mark, Answer, StatusMark), and the characters byte for byte (arc42 §5.1)."""

import pytest

from mattermost_ai_agent_bus import a2a, symbols
from mattermost_ai_agent_bus.model import Answer, Mark, StatusMark

KEYCAP = chr(0xFE0F) + chr(0x20E3)


def test_no_meaning_has_two_names():
    """An answer is no text mark, a coordinator state is no crown mark: the enums share no member name."""
    names = [{m.name for m in e} for e in (Mark, Answer, StatusMark)]
    assert not (names[0] & names[1]) and not (names[0] & names[2])
    assert names[1] & names[2] == set()


def test_every_member_has_its_characters():
    assert {m for m in Mark} == set(symbols._MARKS)
    assert {a for a in Answer} == set(symbols._ANSWERS)
    assert {s for s in StatusMark} == set(symbols._STATUS)


def test_a_choice_is_an_answer():
    assert [Answer.choice(n) for n in (1, 2, 3, 4)] == [Answer.ONE, Answer.TWO, Answer.THREE, Answer.FOUR]
    with pytest.raises(IndexError):
        Answer.choice(5)


def test_the_wire_form_is_unchanged():
    assert a2a.symbol(Mark.CROWN) == "\U0001F451" and a2a.symbol(Mark.HEADER) == "\U0001F68C"
    assert a2a.answer_symbol(Answer.YES) == "\U0001F44D" and a2a.answer_symbol(Answer.NO) == "\U0001F44E"
    assert a2a.answer_marks(2) == ["\U0001F44D", "\U0001F44E"]
    assert a2a.answer_marks(4) == [f"{n}{KEYCAP}" for n in "1234"]
    assert a2a.status_emoji(StatusMark.COORDINATOR) == "crown" and a2a.status_emoji(StatusMark.BUS) == "satellite_antenna"

"""RT-6.14 step 2: a status post's bus word in backticks, a blank line after
its headline; every reader of the first word takes both forms."""

import pytest

from mattermost_ai_agent_bus import a2a, ownpost, tasks, turnend


@pytest.mark.parametrize("given, written", [
    ("IDLE · done: x", "`IDLE` · done: x"),
    ("👑 IDLE · done: x", "👑 `IDLE` · done: x"),
    ("IDLE · gist\ndone: x\nnext: y", "`IDLE` · gist\n\ndone: x\nnext: y"),
    ("IDLE · gist\n\ndone: x", "`IDLE` · gist\n\ndone: x"),          # already blank
    ("`IDLE` · gist\ndone: x", "`IDLE` · gist\n\ndone: x"),          # backticked by the agent
    ("DONE", "`DONE`"),
    ("NO – not trusted by me", "`NO` – not trusted by me"),
    ("ASSIGNMENTS · x\n| Agent |\n|---|", "`ASSIGNMENTS` · x\n\n| Agent |\n|---|"),
    ("IDLE · gist\n", "`IDLE` · gist\n"),                            # nothing follows
])
def test_the_bus_word_goes_in_backticks(given, written):
    assert a2a.styled(given) == written
    assert a2a.styled(written) == written  # once is enough


@pytest.mark.parametrize("text", [
    "No worries\nsee below", "PR #57 merged", "IDLES are fine", "LGTM · ship it",
    "`IDLE · half a backtick", "freeze lifted\nfor all", "- IDLE · in a list", "",
])
def test_other_text_stays_as_it_is(text):
    assert a2a.styled(text) == text


@pytest.mark.parametrize("old, new", [
    ("IDLE · done: x", "`IDLE` · done: x"),
    ("👑 BYE · task done", "👑 `BYE` · task done"),
    ("HELLO · **x**", "`HELLO` · **x**"),
    ("TASK · relay", "`TASK` · relay"),
])
def test_the_first_word_reads_the_same_in_both_forms(old, new):
    assert a2a.word(old) == a2a.word(new) is not None


def test_sign_off_and_listen_gates_take_both_forms():
    """RT-6.2 steps 2-3 with RT-6.14 step 2: `IDLE` and `BYE` sign off, a
    crowned `BYE` ends the listen window."""
    for msg in ("IDLE · x", "`IDLE` · x", "👑 `IDLE` · x", "`BYE` · done", "BYE"):
        assert ownpost.signed_off(msg), msg
        assert not turnend.needs_signoff(msg, asked=False, by_bus=False, push=False), msg
    assert not ownpost.signed_off("`PROGRESS` · x")
    assert turnend.needs_signoff("`PROGRESS` · x", asked=False, by_bus=False, push=False)
    for msg in ("BYE · done", "`BYE` · done", "👑 `BYE` · done"):
        assert not turnend.listens(msg, project=True), msg
    assert turnend.listens("`IDLE` · x", project=True)


def test_titles_and_digest_lines_take_both_forms():
    """RT-6.14 steps 3 and 6: a task's title drops `TASK` with its backticks;
    a bystander's digest keeps the headline only, the blank line aside."""
    assert tasks.title("`TASK` · relay prompts\n\nspec: RT-6.8") == "relay prompts"
    assert tasks.title("TASK · relay prompts") == "relay prompts"
    assert tasks.compact("`IDLE` · waiting\n\ndone: x") == "`IDLE` · waiting…"


def test_the_handoff_reads_a_post_with_a_blank_line_as_one_line():
    """RT-6.7 step 1: own posts come back on one line each, blank lines aside."""
    uid = "u" * 26
    posts = [{"id": "a", "user_id": uid, "root_id": "h", "create_at": 2000,
              "message": "`IDLE` · gist\n\ndone: x"}]
    assert "] `IDLE` · gist done: x" in turnend.handoff(posts, uid)

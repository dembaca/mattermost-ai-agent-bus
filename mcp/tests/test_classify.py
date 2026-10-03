"""Tests for classify_post."""

from mattermost_ai_agent_bus.websocket_inbox import classify_post

BOT_ID = "botuserid123"
BOT_USER = "agent-cursor-1"


def _post(message: str, *, user_id: str = "human1", channel_id: str = "ch1"):
    return {
        "id": "p1",
        "user_id": user_id,
        "channel_id": channel_id,
        "message": message,
    }


def test_ignore_own_posts():
    assert (
        classify_post(
            _post("hello", user_id=BOT_ID),
            bot_user_id=BOT_ID,
            bot_username=BOT_USER,
            channel_type="O",
            watched_channel_ids={"ch1"},
        )
        is None
    )


def test_mention_by_user_id():
    kind = classify_post(
        _post(f"hey <@{BOT_ID}> please look"),
        bot_user_id=BOT_ID,
        bot_username=BOT_USER,
        channel_type="O",
    )
    assert kind == "mention"


def test_mention_by_username():
    kind = classify_post(
        _post(f"@{BOT_USER} take this"),
        bot_user_id=BOT_ID,
        bot_username=BOT_USER,
        channel_type="O",
    )
    assert kind == "mention"


def test_mention_username_case_insensitive():
    kind = classify_post(
        _post("@Agent-Cursor-1 go"),
        bot_user_id=BOT_ID,
        bot_username=BOT_USER,
        channel_type="O",
    )
    assert kind == "mention"


def test_username_not_substring():
    kind = classify_post(
        _post("@agent-cursor-12 other"),
        bot_user_id=BOT_ID,
        bot_username=BOT_USER,
        channel_type="O",
        watched_channel_ids=set(),
    )
    assert kind is None


def test_dm_channel():
    kind = classify_post(
        _post("hi"),
        bot_user_id=BOT_ID,
        bot_username=BOT_USER,
        channel_type="D",
    )
    assert kind == "dm"


def test_watched_channel():
    kind = classify_post(
        _post("broadcast", channel_id="agents-ch"),
        bot_user_id=BOT_ID,
        bot_username=BOT_USER,
        channel_type="O",
        watched_channel_ids={"agents-ch"},
    )
    assert kind == "watched_channel"


def test_unrelated_channel():
    kind = classify_post(
        _post("noise", channel_id="other"),
        bot_user_id=BOT_ID,
        bot_username=BOT_USER,
        channel_type="O",
        watched_channel_ids={"agents-ch"},
    )
    assert kind is None


def test_mention_beats_watched():
    kind = classify_post(
        _post(f"<@{BOT_ID}> ping", channel_id="agents-ch"),
        bot_user_id=BOT_ID,
        bot_username=BOT_USER,
        channel_type="O",
        watched_channel_ids={"agents-ch"},
    )
    assert kind == "mention"

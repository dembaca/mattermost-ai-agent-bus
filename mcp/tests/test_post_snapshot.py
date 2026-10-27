"""Typed posts change no output (arc42 §5.1, issue #82 P3b).

The tools' JSON — slim and raw=true — and the events wait_for_events and the
channel pusher hand the agent are byte for byte what they were while posts
were plain dicts. EXPECTED was taken from the dict-based server (0.45.1)
with the same posts and the same fake client; `outputs` runs either.
"""

import json
import time

import pytest

from mattermost_ai_agent_bus import client as client_mod
from mattermost_ai_agent_bus import model, server
from mattermost_ai_agent_bus.websocket_inbox import ClassifiedEvent

ME, HUMAN, AGENT, HOOK = "u" * 26, "h" * 26, "a" * 26, "w" * 26
CH = "c" * 26

POSTS = [
    {"id": "p1" + "0" * 24, "create_at": 1792229058954, "update_at": 1792229058954, "edit_at": 0,
     "delete_at": 0, "is_pinned": False, "user_id": HUMAN, "channel_id": CH, "root_id": "",
     "original_id": "", "message": "@agent-x TASK · ship it 👂", "type": "",
     "props": {"disable_group_highlight": True}, "hashtags": "", "pending_post_id": "",
     "reply_count": 2, "metadata": {"embeds": [], "reactions": [
         {"user_id": AGENT, "post_id": "p1" + "0" * 24, "emoji_name": "+1", "create_at": 1}]}},
    {"id": "p2" + "0" * 24, "create_at": 1792229060000, "edit_at": 1792229070000, "delete_at": 0,
     "user_id": AGENT, "channel_id": CH, "root_id": "p1" + "0" * 24, "message": "ACK · on it",
     "type": "", "props": {"a2a": {"v": 1, "kind": "ACK", "intent": "fyi", "to": "all"}},
     "reply_count": 2},
    {"id": "p3" + "0" * 24, "create_at": 1792229061000, "user_id": HUMAN, "channel_id": CH,
     "root_id": "p1" + "0" * 24, "message": "yes, go", "type": ""},
    {"id": "p4" + "0" * 24, "create_at": 1792229062000, "user_id": HOOK, "channel_id": CH,
     "root_id": "", "message": "deploy finished", "type": "",
     "props": {"from_webhook": "true", "override_username": "ci"}},
    {"id": "s1" + "0" * 24, "create_at": 1792229063000, "user_id": HUMAN, "channel_id": CH,
     "root_id": "", "message": "agent-x added to the channel", "type": "system_add_to_channel"},
]


def _wrap(raw):
    """A post as the client of the running version hands it on."""
    post_of = getattr(client_mod, "post_of", None)
    return post_of(dict(raw)) if post_of else dict(raw)


class _Client:
    async def usernames(self, ids):
        return {k: v for k, v in {HUMAN: "dembaca", AGENT: "agent-y", ME: "agent-x"}.items() if k in ids}

    async def get_channel(self, cid):
        return {"name": "agents"}

    async def trusted_humans(self):
        return {HUMAN: "dembaca"}

    async def agents(self, ids):
        return {AGENT} & set(ids)

    async def get_me(self):
        return {"id": ME, "username": "agent-x"}

    async def list_recent(self, **_):
        return [_wrap(p) for p in POSTS]

    async def thread(self, post_id):
        posts = [_wrap(p) for p in POSTS[:3]]
        order = [p["id"] for p in POSTS[:3]]
        thread = getattr(model, "Thread", None)  # the dict-based server had none
        if thread:
            return thread(POSTS[0]["id"], tuple(order), tuple(posts))
        return {"root_id": POSTS[0]["id"], "order": order, "posts": posts}


def _events():
    reaction = {"id": "r1", "root_id": POSTS[0]["id"], "channel_id": CH, "user_id": HUMAN,
                "create_at": 1792229064000, "message": ":+1:"}
    stall = {"id": "", "message": "Stall: 1 open, nobody working:", "create_at": 1792229065000}
    return [ClassifiedEvent(kind="mention", post=_wrap(POSTS[0])),
            ClassifiedEvent(kind="watched_channel", post=_wrap(POSTS[1]), gated="from x"),
            ClassifiedEvent(kind="watched_channel", post=_wrap(POSTS[2]), departed="agent-z"),
            ClassifiedEvent(kind="dm", post=_wrap(POSTS[3]), channel={"id": CH, "type": "D"}),
            ClassifiedEvent(kind="reaction", post=_wrap(reaction),
                            raw={"emoji": "+1", "ask": POSTS[0]["id"]}),
            ClassifiedEvent(kind="stall", post=_wrap(stall))]


async def outputs(monkeypatch):
    """Every output this covers, as the agent gets it."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    monkeypatch.setattr(server, "_get_client", lambda: _Client())
    out = {}
    for raw in (False, True):
        out[f"list_recent raw={raw}"] = await server.list_recent(raw=raw)
        out[f"get_thread raw={raw}"] = await server.get_thread(POSTS[0]["id"], raw=raw)
        out[f"events raw={raw}"] = server._dump(await server._event_items(_events(), raw))
    return out


EXPECTED = json.loads(r"""
{
 "list_recent raw=False": "[{\"id\":\"p1000000000000000000000000\",\"channel\":\"agents\",\"author\":\"dembaca\",\"at\":\"2026-10-17 09:24\",\"message\":\"@agent-x TASK · ship it 👂\",\"replies\":2},{\"id\":\"p2000000000000000000000000\",\"root_id\":\"p1000000000000000000000000\",\"channel\":\"agents\",\"author\":\"agent-y\",\"at\":\"2026-10-17 09:24\",\"message\":\"ACK · on it\",\"edited\":true,\"replies\":2},{\"id\":\"p3000000000000000000000000\",\"root_id\":\"p1000000000000000000000000\",\"channel\":\"agents\",\"author\":\"dembaca\",\"at\":\"2026-10-17 09:24\",\"message\":\"yes, go\"},{\"id\":\"p4000000000000000000000000\",\"channel\":\"agents\",\"author\":\"wwwwwwwwwwwwwwwwwwwwwwwwww\",\"at\":\"2026-10-17 09:24\",\"message\":\"deploy finished\"}]",
 "get_thread raw=False": "{\"root_id\":\"p1000000000000000000000000\",\"posts\":[{\"id\":\"p1000000000000000000000000\",\"channel\":\"agents\",\"author\":\"dembaca\",\"at\":\"2026-10-17 09:24\",\"message\":\"@agent-x TASK · ship it 👂\",\"replies\":2},{\"id\":\"p2000000000000000000000000\",\"root_id\":\"p1000000000000000000000000\",\"channel\":\"agents\",\"author\":\"agent-y\",\"at\":\"2026-10-17 09:24\",\"message\":\"ACK · on it\",\"edited\":true,\"replies\":2},{\"id\":\"p3000000000000000000000000\",\"root_id\":\"p1000000000000000000000000\",\"channel\":\"agents\",\"author\":\"dembaca\",\"at\":\"2026-10-17 09:24\",\"message\":\"yes, go\"}]}",
 "events raw=False": "[{\"kind\":\"mention\",\"from_trusted_human\":true,\"from_agent\":false,\"trusted_human\":\"dembaca\",\"post\":{\"id\":\"p1000000000000000000000000\",\"channel\":\"agents\",\"author\":\"dembaca\",\"at\":\"2026-10-17 09:24\",\"message\":\"@agent-x TASK · ship it 👂\",\"replies\":2}},{\"kind\":\"watched_channel\",\"from_trusted_human\":false,\"from_agent\":true,\"gated\":\"from x\",\"to\":\"all\",\"intent\":\"fyi\",\"post\":{\"id\":\"p2000000000000000000000000\",\"root_id\":\"p1000000000000000000000000\",\"channel\":\"agents\",\"author\":\"agent-y\",\"at\":\"2026-10-17 09:24\",\"message\":\"ACK · on it\",\"edited\":true,\"replies\":2}},{\"kind\":\"watched_channel\",\"from_trusted_human\":true,\"from_agent\":false,\"trusted_human\":\"dembaca\",\"departed\":\"agent-z\",\"post\":{\"id\":\"p3000000000000000000000000\",\"root_id\":\"p1000000000000000000000000\",\"channel\":\"agents\",\"author\":\"dembaca\",\"at\":\"2026-10-17 09:24\",\"message\":\"yes, go\"}},{\"kind\":\"dm\",\"from_trusted_human\":false,\"from_agent\":false,\"post\":{\"id\":\"p4000000000000000000000000\",\"channel\":\"agents\",\"author\":\"wwwwwwwwwwwwwwwwwwwwwwwwww\",\"at\":\"2026-10-17 09:24\",\"message\":\"deploy finished\"}},{\"kind\":\"reaction\",\"from_trusted_human\":true,\"from_agent\":false,\"trusted_human\":\"dembaca\",\"ask\":\"p1000000000000000000000000\",\"emoji\":\"+1\",\"answer\":\"yes\",\"post\":{\"id\":\"r1\",\"root_id\":\"p1000000000000000000000000\",\"channel\":\"agents\",\"author\":\"dembaca\",\"at\":\"2026-10-17 09:24\",\"message\":\":+1:\"}},{\"kind\":\"stall\",\"from_trusted_human\":false,\"from_agent\":false,\"post\":{\"message\":\"Stall: 1 open, nobody working:\"}}]",
 "list_recent raw=True": "[{\"id\":\"p1000000000000000000000000\",\"create_at\":1792229058954,\"update_at\":1792229058954,\"edit_at\":0,\"delete_at\":0,\"is_pinned\":false,\"user_id\":\"hhhhhhhhhhhhhhhhhhhhhhhhhh\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"\",\"original_id\":\"\",\"message\":\"@agent-x TASK · ship it 👂\",\"type\":\"\",\"props\":{\"disable_group_highlight\":true},\"hashtags\":\"\",\"pending_post_id\":\"\",\"reply_count\":2,\"metadata\":{\"embeds\":[],\"reactions\":[{\"user_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaa\",\"post_id\":\"p1000000000000000000000000\",\"emoji_name\":\"+1\",\"create_at\":1}]}},{\"id\":\"p2000000000000000000000000\",\"create_at\":1792229060000,\"edit_at\":1792229070000,\"delete_at\":0,\"user_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaa\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"p1000000000000000000000000\",\"message\":\"ACK · on it\",\"type\":\"\",\"props\":{\"a2a\":{\"v\":1,\"kind\":\"ACK\",\"intent\":\"fyi\",\"to\":\"all\"}},\"reply_count\":2},{\"id\":\"p3000000000000000000000000\",\"create_at\":1792229061000,\"user_id\":\"hhhhhhhhhhhhhhhhhhhhhhhhhh\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"p1000000000000000000000000\",\"message\":\"yes, go\",\"type\":\"\"},{\"id\":\"p4000000000000000000000000\",\"create_at\":1792229062000,\"user_id\":\"wwwwwwwwwwwwwwwwwwwwwwwwww\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"\",\"message\":\"deploy finished\",\"type\":\"\",\"props\":{\"from_webhook\":\"true\",\"override_username\":\"ci\"}},{\"id\":\"s1000000000000000000000000\",\"create_at\":1792229063000,\"user_id\":\"hhhhhhhhhhhhhhhhhhhhhhhhhh\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"\",\"message\":\"agent-x added to the channel\",\"type\":\"system_add_to_channel\"}]",
 "get_thread raw=True": "{\"root_id\":\"p1000000000000000000000000\",\"order\":[\"p1000000000000000000000000\",\"p2000000000000000000000000\",\"p3000000000000000000000000\"],\"posts\":[{\"id\":\"p1000000000000000000000000\",\"create_at\":1792229058954,\"update_at\":1792229058954,\"edit_at\":0,\"delete_at\":0,\"is_pinned\":false,\"user_id\":\"hhhhhhhhhhhhhhhhhhhhhhhhhh\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"\",\"original_id\":\"\",\"message\":\"@agent-x TASK · ship it 👂\",\"type\":\"\",\"props\":{\"disable_group_highlight\":true},\"hashtags\":\"\",\"pending_post_id\":\"\",\"reply_count\":2,\"metadata\":{\"embeds\":[],\"reactions\":[{\"user_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaa\",\"post_id\":\"p1000000000000000000000000\",\"emoji_name\":\"+1\",\"create_at\":1}]}},{\"id\":\"p2000000000000000000000000\",\"create_at\":1792229060000,\"edit_at\":1792229070000,\"delete_at\":0,\"user_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaa\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"p1000000000000000000000000\",\"message\":\"ACK · on it\",\"type\":\"\",\"props\":{\"a2a\":{\"v\":1,\"kind\":\"ACK\",\"intent\":\"fyi\",\"to\":\"all\"}},\"reply_count\":2},{\"id\":\"p3000000000000000000000000\",\"create_at\":1792229061000,\"user_id\":\"hhhhhhhhhhhhhhhhhhhhhhhhhh\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"p1000000000000000000000000\",\"message\":\"yes, go\",\"type\":\"\"}]}",
 "events raw=True": "[{\"kind\":\"mention\",\"from_trusted_human\":true,\"from_agent\":false,\"trusted_human\":\"dembaca\",\"post\":{\"id\":\"p1000000000000000000000000\",\"create_at\":1792229058954,\"update_at\":1792229058954,\"edit_at\":0,\"delete_at\":0,\"is_pinned\":false,\"user_id\":\"hhhhhhhhhhhhhhhhhhhhhhhhhh\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"\",\"original_id\":\"\",\"message\":\"@agent-x TASK · ship it 👂\",\"type\":\"\",\"props\":{\"disable_group_highlight\":true},\"hashtags\":\"\",\"pending_post_id\":\"\",\"reply_count\":2,\"metadata\":{\"embeds\":[],\"reactions\":[{\"user_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaa\",\"post_id\":\"p1000000000000000000000000\",\"emoji_name\":\"+1\",\"create_at\":1}]}},\"channel\":null},{\"kind\":\"watched_channel\",\"from_trusted_human\":false,\"from_agent\":true,\"gated\":\"from x\",\"to\":\"all\",\"intent\":\"fyi\",\"post\":{\"id\":\"p2000000000000000000000000\",\"create_at\":1792229060000,\"edit_at\":1792229070000,\"delete_at\":0,\"user_id\":\"aaaaaaaaaaaaaaaaaaaaaaaaaa\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"p1000000000000000000000000\",\"message\":\"ACK · on it\",\"type\":\"\",\"props\":{\"a2a\":{\"v\":1,\"kind\":\"ACK\",\"intent\":\"fyi\",\"to\":\"all\"}},\"reply_count\":2},\"channel\":null},{\"kind\":\"watched_channel\",\"from_trusted_human\":true,\"from_agent\":false,\"trusted_human\":\"dembaca\",\"departed\":\"agent-z\",\"post\":{\"id\":\"p3000000000000000000000000\",\"create_at\":1792229061000,\"user_id\":\"hhhhhhhhhhhhhhhhhhhhhhhhhh\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"p1000000000000000000000000\",\"message\":\"yes, go\",\"type\":\"\"},\"channel\":null},{\"kind\":\"dm\",\"from_trusted_human\":false,\"from_agent\":false,\"post\":{\"id\":\"p4000000000000000000000000\",\"create_at\":1792229062000,\"user_id\":\"wwwwwwwwwwwwwwwwwwwwwwwwww\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"root_id\":\"\",\"message\":\"deploy finished\",\"type\":\"\",\"props\":{\"from_webhook\":\"true\",\"override_username\":\"ci\"}},\"channel\":{\"id\":\"cccccccccccccccccccccccccc\",\"type\":\"D\"}},{\"kind\":\"reaction\",\"from_trusted_human\":true,\"from_agent\":false,\"trusted_human\":\"dembaca\",\"ask\":\"p1000000000000000000000000\",\"emoji\":\"+1\",\"answer\":\"yes\",\"post\":{\"id\":\"r1\",\"root_id\":\"p1000000000000000000000000\",\"channel_id\":\"cccccccccccccccccccccccccc\",\"user_id\":\"hhhhhhhhhhhhhhhhhhhhhhhhhh\",\"create_at\":1792229064000,\"message\":\":+1:\"},\"channel\":null},{\"kind\":\"stall\",\"from_trusted_human\":false,\"from_agent\":false,\"post\":{\"message\":\"Stall: 1 open, nobody working:\"}}]"
}
""")


@pytest.mark.asyncio
async def test_outputs_are_byte_for_byte_as_with_dict_posts(monkeypatch):
    got = await outputs(monkeypatch)
    assert got == EXPECTED

"""Errors of the chat port, typed (arc42 §5, NATS phase 2): a channel that is
not there or not the bot's comes over `ChatPort` as `model.NotFound` or
`model.NotAllowed`, never as an HTTP status. The hook API keys its 400 and 403
on those, so a later adapter without httpx keeps them."""

from __future__ import annotations

import httpx
import pytest

from mattermost_ai_agent_bus import hookapi
from mattermost_ai_agent_bus.client import MattermostAdapter
from mattermost_ai_agent_bus.model import NotAllowed, NotFound

TEAM = "t" * 26
HOME = "h" * 26
CHAN = "c" * 26


def _client(status: int) -> tuple[MattermostAdapter, list[str]]:
    """An adapter whose Mattermost answers `status` to every request."""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req.url.path)
        return httpx.Response(status, json={"message": "nope"})

    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._team_id = TEAM
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return c, seen


@pytest.mark.parametrize("status,want,kind", [
    (404, NotFound, "channel guest-x: not there (404)"),
    (400, NotFound, "channel guest-x: not there (400)"),
    (403, NotAllowed, "channel guest-x: the bot may not see it (403)"),
    (401, NotAllowed, "channel guest-x: the bot may not see it (401)")])
async def test_get_channel_id_translates_mattermosts_refusal(status, want, kind):
    """A channel read by name: 404 and 400 (an invalid name) are NotFound, 401
    and 403 NotAllowed, the Mattermost error kept as their cause."""
    c, seen = _client(status)
    with pytest.raises(want) as caught:
        await c.get_channel_id("guest-x")
    assert str(caught.value) == kind
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
    assert seen == [f"/api/v4/teams/{TEAM}/channels/name/guest-x"]


async def test_get_channel_id_keeps_any_other_failure():
    """A 5xx is not about the channel: it stays the httpx error, for the 502
    the hook API's unknown failures become."""
    c, _ = _client(500)
    with pytest.raises(httpx.HTTPStatusError):
        await c.get_channel_id("guest-x")


async def test_get_channel_id_reads_the_channel_when_it_is_there():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": CHAN, "name": "guest-x"})

    c = MattermostAdapter(base_url="https://mm.example", token="t")
    c._team_id = TEAM
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await c.get_channel_id("guest-x") == CHAN


@pytest.mark.parametrize("status,want", [
    (404, NotFound), (400, NotFound), (401, NotAllowed), (403, NotAllowed)])
async def test_get_channel_translates_mattermosts_refusal(status, want):
    """A channel read by id: the same mapping, its Mattermost error the cause,
    and nothing kept of the failed read."""
    c, _ = _client(status)
    with pytest.raises(want) as caught:
        await c.get_channel(CHAN)
    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)
    assert str(caught.value).startswith(f"channel {CHAN}: ")
    assert CHAN not in c._channels


async def test_get_channel_keeps_any_other_failure():
    c, _ = _client(500)
    with pytest.raises(httpx.HTTPStatusError):
        await c.get_channel(CHAN)


async def test_a_join_the_adapter_reports_still_says_which_channel():
    """RT-6.22 step 2: joining a missing or private channel is reported, never
    raised, and the reports read as before: the port's typed errors carry them."""
    c, _ = _client(404)
    assert await c._join_named("missing", "u" * 26) == {
        "name": "missing", "joined": False,
        "error": f"channel 'missing' does not exist in team '{c.team_name}' "
                 "(check the name; this server never creates channels)"}
    c, _ = _client(403)
    assert await c._join_named("private", "u" * 26) == {
        "name": "private", "joined": False,
        "error": "cannot join 'private': private channel (a human must add "
                 "the bot) or joining is denied on this instance"}


async def test_the_hook_api_answers_a_missing_channel_by_its_type():
    """(3) `_channel` turns the port's NotFound into the same 400 as before,
    with no look at an HTTP status, and lets any other failure through."""
    class Missing:
        async def get_channel_id(self, name=None, team_name=None):
            raise NotFound(f"no channel {name}")

    class Broken(Missing):
        async def get_channel_id(self, name=None, team_name=None):
            raise RuntimeError("down")

    with pytest.raises(hookapi.BadRequest, match="no channel guest-x in the team") as caught:
        await hookapi._channel(Missing(), "guest-x")
    assert isinstance(caught.value.__cause__, NotFound)
    with pytest.raises(RuntimeError):
        await hookapi._channel(Broken(), "guest-x")


async def test_the_hook_api_refuses_a_channel_not_the_bots_by_its_type(monkeypatch):
    """(3) `upload_channel` turns NotFound and NotAllowed into the same 403 as
    before, with no look at an HTTP status."""
    async def watched(client):
        return set()

    monkeypatch.setattr(hookapi, "watched", watched)

    class Port:
        def __init__(self, exc):
            self.exc = exc

        async def get_channel(self, channel_id):
            raise self.exc

    for exc in (NotFound("gone"), NotAllowed("denied")):
        with pytest.raises(hookapi.Forbidden, match="not a channel of the bot") as caught:
            await hookapi.upload_channel(Port(exc), CHAN)
        assert caught.value.__cause__ is exc
    with pytest.raises(RuntimeError):
        await hookapi.upload_channel(Port(RuntimeError("down")), CHAN)

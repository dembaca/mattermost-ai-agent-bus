"""The 👑's receipts and the claim over a missing one (ADR-007 steps 3 to 5,
amendment step 2, phase E): RT-6.12 step 7 (the 👀 at the take-up, the ⏳
during a turn), RT-6.6 step 10 (the marker, the watch, the claim, crown_taken,
the yield) and RT-6.25 step 6 (a question without a receipt goes to the human
after MM_ACK_WAIT_SEC)."""

import asyncio
import time

import pytest

from mattermost_ai_agent_bus import a2a, askroute, lease, life, receipt
from mattermost_ai_agent_bus.client import post_of
from mattermost_ai_agent_bus.gate import AGENT, OTHER, TRUSTED
from mattermost_ai_agent_bus.model import ChannelUser, Mark, Reaction

from fakes import WORK, FakeChat

ME = "m" * 26       # this agent
HOLDER = "k" * 26   # the home's 👑
NEXT = "n" * 26     # another member, younger than this one
HUMAN = "h" * 26
PID = "p" * 26
SEEN, WAITS = a2a.receipt_reaction(Mark.SEEN), a2a.receipt_reaction(Mark.WAITS)
MARKED = f"coordinator · working · {lease.RECEIPT_MARK} · proj · ~agents"


class Chat(FakeChat):
    """FakeChat that records the reactions it puts on posts."""

    def __init__(self, **kw):
        super().__init__(uid=ME, username="agent-me", agents={ME, HOLDER, NEXT},
                         names={HUMAN: "dembaca", HOLDER: "agent-coord", ME: "agent-me"}, **kw)
        self.reacted: list[tuple[str, str]] = []

    async def add_reaction(self, post_id, emoji):
        self.reacted.append((post_id, emoji))

    def agent(self, uid, name, created, emoji="hammer_and_wrench", text="working · proj · ~agents",
              expires_in=150):
        status = {"emoji": emoji, "text": text, "duration": "date_and_time",
                  "expires_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + expires_in))}
        self.members = [m for m in self.members if m.id != uid] + [
            ChannelUser(uid, name, created, custom_status=status)]
        self.presence[uid] = "online"


@pytest.fixture
def chat(monkeypatch):
    monkeypatch.setenv("MM_BOT_USER_ID", ME)
    monkeypatch.delenv("MM_ACK_WAIT_SEC", raising=False)
    monkeypatch.delenv("MM_LEASE_SEC", raising=False)
    return Chat()


def _post(message="please look at the build", user=HUMAN, pid=PID, **raw):
    return post_of({"id": pid, "message": message, "user_id": user, "channel_id": WORK,
                    "create_at": int(time.time() * 1000), **raw})


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


def _hold():
    lease._save_coord(ME, int(time.time()) + 150, 0, 0)


def _holder_seen(marked=True):
    lease._holder_id, lease._holder_name, lease._holder_marked = HOLDER, "agent-coord", marked


# --- the holder's side (RT-6.12 step 7) --------------------------------------------------

async def test_the_holder_sets_the_eyes_at_the_take_up_not_at_arrival(chat, monkeypatch):
    """RT-6.12 step 7: a post that wakes the 👑, pushed while no turn runs, gets
    the 👀 when the agent takes it up (a tool call, a turn's start)."""
    monkeypatch.setattr(receipt, "pushes", lambda: True)
    _hold()
    await receipt.arrived(chat, _post(), ME, False)
    await _settle()
    assert chat.reacted == []
    life.take_up()
    await _settle()
    assert chat.reacted == [(PID, SEEN)]


async def test_during_a_turn_the_hourglass_at_once_and_the_eyes_with_the_next_turn(chat, monkeypatch):
    """RT-6.12 step 7 (decided 2026-11-12): every waking post that comes while a
    turn runs gets ⏳ at once; a tool call of that turn does not take it up,
    the turn's end does."""
    monkeypatch.setattr(receipt, "pushes", lambda: True)
    _hold()
    life.turn(True)
    await receipt.arrived(chat, _post(), ME, False)
    await _settle()
    assert chat.reacted == [(PID, WAITS)]
    life.take_up()
    await _settle()
    assert chat.reacted == [(PID, WAITS)]
    life.turn(False)
    await _settle()
    assert chat.reacted == [(PID, WAITS), (PID, SEEN)]


async def test_without_a_channel_the_return_of_wait_for_events_delivers(chat):
    """RT-6.12 step 7: without pushes a take-up delivers nothing; the post
    wait_for_events returns gets the 👀 (the Stop hook's drain, a listen
    window). The ⏳ comes during a turn there too."""
    _hold()
    life.turn(True)
    await receipt.arrived(chat, _post(), ME, False)
    life.turn(False)
    await _settle()
    assert chat.reacted == [(PID, WAITS)]
    receipt.delivered([PID, "x" * 26])
    await _settle()
    assert chat.reacted == [(PID, WAITS), (PID, SEEN)]


async def test_a_failed_receipt_is_tried_once_more(chat, monkeypatch):
    """The amendment's open question: the holder retries the reaction once."""
    calls = []

    async def flaky(post_id, emoji):
        calls.append(emoji)
        if len(calls) == 1:
            raise OSError("socket")
    monkeypatch.setattr(chat, "add_reaction", flaky)
    _hold()
    await receipt.arrived(chat, _post(), ME, False)
    receipt.delivered([PID])
    await _settle()
    assert calls == [SEEN, SEEN]


@pytest.mark.parametrize("message, cls, wakes", [
    ("please look at the build", OTHER, True),        # a human's plain post
    ("@agent-coord which branch?", AGENT, True),     # a mention, a routed question too
    ("@agent-x take the build", TRUSTED, False),     # for the agent it names
    ("@channel standup", TRUSTED, False),            # for everyone
    ("DONE with the build", AGENT, False),           # an agent's post naming nobody
])
def test_what_wakes_the_holder(message, cls, wakes):
    """RT-6.12 step 3, RT-6.6 step 5: the posts that ask for a receipt."""
    assert receipt.wakes_holder(_post(message), cls, HOLDER, "agent-coord", WORK) is wakes


def test_a_quiet_line_and_another_channel_wake_nobody():
    usage = _post("ctx 86%", props={"a2a": {"kind": a2a.CROWN_TAKEN}})
    assert not receipt.wakes_holder(usage, AGENT, HOLDER, "agent-coord", WORK)
    assert not receipt.wakes_holder(_post(channel_id="z" * 26), OTHER, HOLDER, "agent-coord", WORK)


# --- the others' watch (RT-6.6 step 10) ---------------------------------------------------

async def test_a_member_watches_a_post_for_a_marked_holder_only(chat):
    """Decided 2026-11-12: a holder without the marker (an older plugin) is
    judged as before; a post the member gates starts nothing."""
    _holder_seen(marked=False)
    await receipt.arrived(chat, _post(), ME, False)
    assert receipt.due() == 0
    _holder_seen()
    await receipt.arrived(chat, _post(), ME, True)
    assert receipt.due() == 0
    await receipt.arrived(chat, _post(), ME, False)
    assert receipt.due() == pytest.approx(time.time() + 45, abs=2)


async def test_with_trusted_humans_an_untrusted_post_starts_no_watch(chat):
    """RT-6.16: its own trust decides whether the post counts as a wake."""
    chat.trusted = {HUMAN: "dembaca"}
    _holder_seen()
    await receipt.arrived(chat, _post(user="v" * 26), ME, False)
    assert receipt.due() == 0
    await receipt.arrived(chat, _post(), ME, False)
    assert receipt.due() > 0


async def test_the_holders_reaction_heard_ends_the_watch(chat):
    """A post answered with a receipt costs the others no read."""
    _holder_seen()
    await receipt.arrived(chat, _post(), ME, False)
    receipt.heard(Reaction(user_id=HUMAN, post_id=PID, emoji_name=SEEN))
    assert receipt.due() > 0
    receipt.heard(Reaction(user_id=HOLDER, post_id=PID, emoji_name=WAITS))
    assert receipt.due() == 0


async def test_without_a_receipt_two_looks_then_the_claim_at_the_slot(chat, monkeypatch):
    """ADR-007 step 3: a look after the wait, one more 10 s later, then the
    claim at this agent's slot; a receipt found at a look ends it."""
    claims, slot = [], [2]

    async def take_over(client, holder, pid):
        claims.append((holder, pid))
        return True

    async def slot_over(client, holder):
        return slot[0]
    monkeypatch.setattr(lease, "take_over", take_over)
    monkeypatch.setattr(lease, "slot_over", slot_over)
    _holder_seen()
    await receipt.arrived(chat, _post(), ME, False)
    w = receipt._watch[PID]
    w.due = 0
    assert not await receipt.check(chat)
    assert w.step == 1 and w.due == pytest.approx(w.at + 55)
    w.due = 0
    assert not await receipt.check(chat)
    assert w.step == 2 and w.due == pytest.approx(w.at + 45 + 2 * lease.SLOT_SEC)
    w.due = 0
    assert await receipt.check(chat)
    assert claims == [(HOLDER, PID)] and receipt.due() == 0
    # A late 👀 found at the slot's look ends the watch without a claim.
    await receipt.arrived(chat, _post(), ME, False)
    w = receipt._watch[PID]
    w.step, w.slot, w.due = 2, 2, 0
    chat.reacts[PID] = [{"user_id": HOLDER, "post_id": PID, "emoji_name": SEEN}]
    assert not await receipt.check(chat) and len(claims) == 1 and receipt.due() == 0


async def test_a_receipt_at_the_first_look_starts_nothing(chat, monkeypatch):
    async def never(*a):
        raise AssertionError("no claim")
    monkeypatch.setattr(lease, "take_over", never)
    _holder_seen()
    await receipt.arrived(chat, _post(), ME, False)
    chat.reacts[PID] = [{"user_id": HOLDER, "post_id": PID, "emoji_name": WAITS}]
    receipt._watch[PID].due = 0
    assert not await receipt.check(chat) and receipt.due() == 0


# --- the claim, crown_taken and the yield (RT-6.6 step 10) -----------------------------------

def _home(chat, holder_text=MARKED):
    chat.agent(HOLDER, "agent-coord", 1000, "crown", holder_text)
    chat.agent(ME, "agent-me", 2000)
    chat.agent(NEXT, "agent-next", 3000)


async def test_the_holders_status_carries_the_marker(chat):
    _hold()
    await lease.set_status(chat, "working")
    assert lease.sets_receipts(chat.status["text"])
    assert lease.entry({"text": chat.status["text"]}).status == "working"


async def test_the_claim_over_a_missing_receipt_posts_crown_taken(chat):
    """ADR-007 steps 3 and 4: the oldest that stands claims although the
    holder's crown is live, and posts the fyi line naming the post."""
    _home(chat)
    assert await lease.slot_over(chat, HOLDER) == 1
    assert await lease.take_over(chat, HOLDER, PID)
    assert lease.crown_held(ME) and chat.status["emoji"] == "crown"
    line = chat.posted[-1]
    assert line["message"].endswith(f"taken over by `agent-me`: no {lease.RECEIPT_MARK} for `{PID}` within 45 s")
    assert line["props"]["a2a"]["kind"] == a2a.CROWN_TAKEN and line["props"]["a2a"]["holder"] == HOLDER
    assert line["props"]["a2a"]["intent"] == a2a.FYI and line["channel_id"] == WORK
    # The new holder does not yield to the older holder it took the crown from.
    await lease.command(chat, "tick")
    assert lease.crown_held(ME)


async def test_no_claim_over_an_unmarked_holder_or_beside_a_second_crown(chat):
    """Decided 2026-11-12: an older plugin's holder is judged as before; and a
    crown an earlier slot took leaves nothing to claim."""
    _home(chat, holder_text="coordinator · working · proj · ~agents")
    assert await lease.slot_over(chat, HOLDER) == 0
    assert not await lease.take_over(chat, HOLDER, PID)
    _home(chat)
    chat.agent(NEXT, "agent-next", 3000, "crown", MARKED)
    assert not await lease.take_over(chat, HOLDER, PID)
    assert not lease.crown_held(ME) and chat.posted == []


async def test_the_old_holder_yields_to_a_crown_taken_heard(chat):
    """ADR-007 step 4: a crown_taken naming this holder, from an agent that
    wears a crown, makes it yield at its next tick and not claim for two leases."""
    chat.agent(HOLDER, "agent-new", 3000, "crown", MARKED)
    chat.agent(ME, "agent-me", 1000, "crown", MARKED)
    _hold()
    taken = _post("taken over", user=HOLDER, props={"a2a": {"kind": a2a.CROWN_TAKEN, "holder": ME}})
    await receipt.arrived(chat, taken, ME, False)
    assert lease.ousted_pending()
    await lease.command(chat, "tick")
    assert not lease.crown_held(ME)
    assert lease._coord(ME)["YIELD_UNTIL"] >= time.time() + 2 * 900 - 5


async def test_a_holder_with_a_dead_socket_reads_crown_taken_from_the_home(chat):
    """ADR-007 step 4, decided 2026-11-12: a second crown shows, so the holder
    reads the home's posts; only a crowned agent's line counts."""
    chat.agent(HOLDER, "agent-new", 3000, "crown", MARKED)
    chat.agent(ME, "agent-me", 1000, "crown", MARKED)
    _hold()
    chat.posts = [{"id": "q" * 26, "user_id": NEXT, "channel_id": WORK, "create_at": int(time.time() * 1000),
                   "message": "taken over", "props": {"a2a": {"kind": a2a.CROWN_TAKEN, "holder": ME}}}]
    await lease.command(chat, "tick")
    assert lease.crown_held(ME)  # NEXT wears no crown: the older keeps it
    chat.posts[0]["user_id"] = HOLDER
    await lease.command(chat, "tick")
    assert not lease.crown_held(ME)


def _own_line(holder=HOLDER):
    return {"id": "r" * 26, "user_id": ME, "channel_id": WORK, "create_at": int(time.time() * 1000) + 1000,
            "message": "taken over", "props": {"a2a": {"kind": a2a.CROWN_TAKEN, "holder": holder}}}


async def test_a_crown_taken_by_an_agent_that_does_not_stand_ousts_nobody(chat):
    """ADR-001, RT-6.6 step 10: an unbound bot wearing a crown cannot make a
    bound holder yield with a crown_taken; two crowns are judged as before."""
    chat.trusted = {HUMAN: "dembaca"}
    chat.descriptions = {ME: "for @dembaca · trusts @dembaca · via agent-registrar",
                         HOLDER: "via agent-registrar"}
    chat.native_owners = {ME: "dembaca", HOLDER: ""}
    chat.agent(HOLDER, "agent-new", 3000, "crown", MARKED)
    chat.agent(ME, "agent-me", 1000, "crown", MARKED)
    _hold()
    rows = lease._at_home(await lease._rows(chat, WORK), "agents")
    assert (await lease._election(chat, rows)).ids == [ME]  # the unbound crown does not stand
    line = {"id": "q" * 26, "user_id": HOLDER, "channel_id": WORK, "create_at": int(time.time() * 1000),
            "message": "taken over", "props": {"a2a": {"kind": a2a.CROWN_TAKEN, "holder": ME}}}
    chat.posts = [line]
    await receipt.arrived(chat, post_of(line), ME, False)
    await lease.command(chat, "tick")
    assert lease.crown_held(ME)


async def test_a_failed_crown_taken_is_posted_again_while_the_old_crown_shows(chat, monkeypatch):
    """RT-6.6 step 10: the claimer's tick finds no line of its own naming the
    old holder, whose crown still shows: it posts the line again, once."""
    _home(chat)
    real, fails = chat.post, [OSError("down")]

    async def flaky(message, **kw):
        if fails:
            raise fails.pop()
        return await real(message, **kw)
    monkeypatch.setattr(chat, "post", flaky)
    assert await lease.take_over(chat, HOLDER, PID)
    assert chat.posted == []
    await lease.command(chat, "tick")
    assert lease.crown_held(ME) and len(chat.posted) == 1
    assert chat.posted[0]["props"]["a2a"]["holder"] == HOLDER and PID in chat.posted[0]["message"]
    chat.posts = [_own_line()]
    await lease.command(chat, "tick")
    assert len(chat.posted) == 1


async def test_the_exemption_from_the_two_crown_rule_is_bounded(chat):
    """RT-6.6 step 10: an old holder that did not yield within a tick and a
    crown lease is judged by the two-crown rule again: the older keeps it."""
    _home(chat)
    assert await lease.take_over(chat, HOLDER, PID)
    chat.posts = [_own_line()]
    await lease.command(chat, "tick")
    assert lease.crown_held(ME)
    lease._crown_since = time.time() - lease.heartbeat_sec() - lease.crown_lease_sec() - 1
    await lease.command(chat, "tick")
    assert not lease.crown_held(ME)


async def test_a_crown_taken_heard_wakes_the_heartbeat_at_once():
    """RT-6.6 step 10: ousted() ends the heartbeat's wait, so the tick that
    yields comes at once, not after a beat."""
    pause = asyncio.create_task(lease._pause(60))
    await asyncio.sleep(0)
    lease.ousted(HOLDER, time.time())
    await asyncio.wait_for(pause, 1)


async def test_the_successor_of_a_silent_holder_names_the_silence(chat):
    """ADR-007 amendment step 1: the claim at the slot after a silent holder
    dropped its crown posts crown_taken naming the silence."""
    _home(chat, holder_text="crown")
    await lease.command(chat, "tick")
    chat.agent(HOLDER, "agent-coord", 1000, "zzz", "silent · no sign since 14:05Z · ~agents")
    lease._look_at = time.time() - 1
    assert await lease.look(chat)
    line = chat.posted[-1]
    assert line["message"].endswith("taken over by `agent-me`: `agent-coord` silent since 14:05Z")
    assert line["props"]["a2a"]["holder"] == HOLDER


# --- a question to the 👑 (RT-6.25 step 6, ADR-007 step 5) --------------------------------

@pytest.mark.parametrize("state, reactions, missing", [
    ("marked", [], True),
    ("marked", [WAITS], False),
    ("marked", [SEEN], False),
    ("silent", [WAITS], True),   # a silent holder's ⏳ counts no more
    ("silent", [SEEN], False),
    ("", [], False),             # an older plugin's holder, or gone: as before
])
async def test_a_question_without_a_receipt_is_missing(chat, monkeypatch, state, reactions, missing):
    async def holder(client, name):
        return (HOLDER if state else ""), state
    monkeypatch.setattr(lease, "receipt_holder", holder)
    chat.reacts[PID] = [{"user_id": HOLDER, "post_id": PID, "emoji_name": e} for e in reactions]
    chat.reacts[PID].append({"user_id": HUMAN, "post_id": PID, "emoji_name": SEEN})
    assert await receipt.missing(chat, PID, "agent-coord") is missing


async def test_the_question_goes_to_the_human_after_the_ack_wait(chat, monkeypatch):
    """RT-6.25 step 6: no receipt after MM_ACK_WAIT_SEC, the human now; looked
    at once, before that the next look is at the wait's end."""
    called = []

    async def fallback(client, question, human, coord):
        called.append(question)
        return "f" * 26

    async def holder(client, name):
        return HOLDER, "marked"
    monkeypatch.setattr(askroute, "fallback", fallback)
    monkeypatch.setattr(lease, "receipt_holder", holder)
    monkeypatch.setattr(askroute, "_answered", lambda *a: asyncio.sleep(0, result=False))
    monkeypatch.setattr(askroute.asks, "questions_open", lambda uid: [(PID, 1000.0, "agent-coord", "dembaca")])
    monkeypatch.setattr(askroute.asks, "open_to", lambda uid, human: False)
    assert await askroute.tick(chat, ME, 1010) == pytest.approx(35)
    assert called == []
    await askroute.tick(chat, ME, 1046)
    assert called == [PID]

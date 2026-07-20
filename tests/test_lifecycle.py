"""End-to-end lifecycle tests against a real SQLite db and a fake publisher,
covering the race and crash-recovery guarantees:

- vote vs close, scheduler vs /next, double close
- restart halfway through publishing (no double-advance, no rerolled ties)
- restart halfway through posting a round (only the missing posts repeat)
"""

import itertools
import random

import pytest

from bracketbot import db, lifecycle


class Boom(RuntimeError):
    """Simulated crash mid-publish."""


class FakePublisher:
    def __init__(self):
        self.events: list[tuple] = []
        self.crash_on: dict[str, int] = {}  # kind -> 1-based call number to die on
        self._ids = itertools.count(1000)

    def _record(self, kind, *data):
        # Record first, then maybe crash: simulates dying right after the
        # Discord call succeeded but before the marker was persisted.
        self.events.append((kind, *data))
        if self.crash_on.get(kind) == self.count(kind):
            del self.crash_on[kind]  # crash once, then behave
            raise Boom(kind)

    def count(self, kind):
        return sum(1 for e in self.events if e[0] == kind)

    async def post_round_open(self, bracket, round_no, closes_at):
        self._record("round_open", round_no, closes_at)

    async def post_matchup(self, bracket, match, a_name, b_name):
        self._record("matchup", match.id)
        return next(self._ids)

    async def reveal_result(self, bracket, result):
        self._record("reveal", result.match.id)

    async def post_round_summary(self, bracket, round_no, results):
        self._record("summary", round_no)

    async def post_champion(self, bracket, result):
        self._record("champion", result.winner_name)


@pytest.fixture
def publisher():
    return FakePublisher()


@pytest.fixture
def rng():
    return random.Random(42)


async def setup_items(conn, bracket_id, names):
    return {name: await db.add_item(conn, bracket_id, name) for name in names}


async def tick(conn, publisher, bracket_id, now=0, rng=None):
    await lifecycle.tick(conn, publisher, bracket_id, now=now, rng=rng or random.Random(42))


async def open_matches(conn, bracket_id):
    bracket = await db.get_bracket(conn, bracket_id)
    return [
        m
        for m in await db.round_matches(conn, bracket_id, bracket.current_round)
        if m.winner is None
    ]


async def test_full_manual_bracket(conn, bracket_id, publisher, rng):
    await setup_items(conn, bracket_id, ["A", "B", "C", "D"])
    await lifecycle.start_bracket(conn, bracket_id, rng)
    await tick(conn, publisher, bracket_id)

    assert publisher.count("round_open") == 1
    assert publisher.count("matchup") == 2
    round1 = await db.round_matches(conn, bracket_id, 1)
    assert all(m.message_id is not None for m in round1)

    # Seeds: A(1) vs D(4), B(2) vs C(3). Vote A and C through.
    await db.cast_vote(conn, round1[0].id, 1, "a", now=0)
    await db.cast_vote(conn, round1[0].id, 2, "a", now=0)
    await db.cast_vote(conn, round1[1].id, 1, "b", now=0)

    assert await lifecycle.close_round(conn, bracket_id, rng)
    await tick(conn, publisher, bracket_id)

    assert publisher.count("reveal") == 2
    assert publisher.count("summary") == 1
    bracket = await db.get_bracket(conn, bracket_id)
    assert bracket.current_round == 2 and bracket.round_state == "open"
    final = (await db.round_matches(conn, bracket_id, 2))[0]
    names = await db.item_names(conn, bracket_id)
    assert {names[final.item_a], names[final.item_b]} == {"A", "C"}

    await db.cast_vote(conn, final.id, 1, "b", now=0)
    assert await lifecycle.close_round(conn, bracket_id, rng)
    await tick(conn, publisher, bracket_id)

    assert publisher.count("champion") == 1
    bracket = await db.get_bracket(conn, bracket_id)
    assert bracket.status == "finished"
    final = (await db.round_matches(conn, bracket_id, 2))[0]
    assert names[final.winner_item_id] == "C" and final.decided_by == "votes"


async def test_byes_with_three_items(conn, bracket_id, publisher, rng):
    ids = await setup_items(conn, bracket_id, ["A", "B", "C"])
    await lifecycle.start_bracket(conn, bracket_id, rng)
    await tick(conn, publisher, bracket_id)

    round1 = await db.round_matches(conn, bracket_id, 1)
    byes = [m for m in round1 if m.is_bye]
    real = [m for m in round1 if not m.is_bye]
    assert len(byes) == 1 and len(real) == 1
    assert byes[0].item_a == ids["A"] and byes[0].decided_by == "bye"  # top seed gets the bye
    assert publisher.count("matchup") == 1  # no vote message for the bye

    assert await lifecycle.close_round(conn, bracket_id, rng)
    await tick(conn, publisher, bracket_id)
    final = (await db.round_matches(conn, bracket_id, 2))[0]
    assert final.item_a == ids["A"]  # bye winner advanced
    assert publisher.count("reveal") == 1  # byes have nothing to reveal


async def test_timed_round_closes_via_tick(conn, bracket_id, publisher, rng):
    await setup_items(conn, bracket_id, ["A", "B"])
    await db.set_round_seconds(conn, bracket_id, 60)
    await lifecycle.start_bracket(conn, bracket_id, rng)
    await tick(conn, publisher, bracket_id, now=1000)

    bracket = await db.get_bracket(conn, bracket_id)
    assert bracket.round_closes_at == 1060
    match = (await db.round_matches(conn, bracket_id, 1))[0]
    assert await db.cast_vote(conn, match.id, 1, "a", now=1059)

    await tick(conn, publisher, bracket_id, now=1059)  # not due yet
    assert (await db.get_bracket(conn, bracket_id)).status == "running"
    assert (await db.get_bracket(conn, bracket_id)).round_state == "open"

    await tick(conn, publisher, bracket_id, now=1060)
    bracket = await db.get_bracket(conn, bracket_id)
    assert bracket.status == "finished"
    assert publisher.count("champion") == 1


async def test_double_close_and_next_vs_scheduler(conn, bracket_id, publisher, rng):
    await setup_items(conn, bracket_id, ["A", "B"])
    await lifecycle.start_bracket(conn, bracket_id, rng)
    await tick(conn, publisher, bracket_id)

    assert await lifecycle.close_round(conn, bracket_id, rng)
    # /next and the scheduler racing: the second close is a no-op
    assert not await lifecycle.close_round(conn, bracket_id, rng)


async def test_tie_coinflip_persisted_not_rerolled(conn, bracket_id, publisher):
    await setup_items(conn, bracket_id, ["A", "B"])
    await lifecycle.start_bracket(conn, bracket_id, random.Random(1))
    await tick(conn, publisher, bracket_id)

    assert await lifecycle.close_round(conn, bracket_id, random.Random(1))  # 0-0 tie
    match = (await db.round_matches(conn, bracket_id, 1))[0]
    assert match.decided_by == "coinflip" and match.winner is not None
    before = match.winner

    # Publishing (even repeatedly, with whatever RNG) never rerolls the flip
    for seed in (2, 3, 4):
        await tick(conn, publisher, bracket_id, rng=random.Random(seed))
    assert (await db.round_matches(conn, bracket_id, 1))[0].winner == before


async def test_crash_mid_publish_recovers_without_double_advance(conn, bracket_id, publisher, rng):
    await setup_items(conn, bracket_id, ["A", "B", "C", "D"])
    await lifecycle.start_bracket(conn, bracket_id, rng)
    await tick(conn, publisher, bracket_id)
    round1 = await db.round_matches(conn, bracket_id, 1)
    await db.cast_vote(conn, round1[0].id, 1, "a", now=0)

    assert await lifecycle.close_round(conn, bracket_id, rng)
    winners_before = [
        (m.id, m.winner, m.decided_by) for m in await db.round_matches(conn, bracket_id, 1)
    ]

    publisher.crash_on = {"summary": 1}
    with pytest.raises(Boom):
        await tick(conn, publisher, bracket_id)

    # Crashed after the summary went out but before the marker was saved:
    # still in 'closing', results revealed, nothing advanced.
    bracket = await db.get_bracket(conn, bracket_id)
    assert bracket.round_state == "closing" and bracket.current_round == 1

    # "Restart": tick again with a different RNG — winners must not change.
    await tick(conn, publisher, bracket_id, rng=random.Random(999))
    winners_after = [
        (m.id, m.winner, m.decided_by) for m in await db.round_matches(conn, bracket_id, 1)
    ]
    assert winners_after == winners_before

    bracket = await db.get_bracket(conn, bracket_id)
    assert bracket.current_round == 2 and bracket.round_state == "open"
    assert publisher.count("reveal") == 2  # not re-revealed after the crash
    assert publisher.count("summary") == 2  # the one duplicate a crash can cost
    assert len(await db.round_matches(conn, bracket_id, 2)) == 1  # no double-advance


async def test_crash_mid_posting_round_repairs_missing_only(conn, bracket_id, publisher, rng):
    await setup_items(conn, bracket_id, ["A", "B", "C", "D", "E", "F", "G", "H"])
    await lifecycle.start_bracket(conn, bracket_id, rng)

    publisher.crash_on = {"matchup": 2}  # dies posting the second matchup
    with pytest.raises(Boom):
        await tick(conn, publisher, bracket_id, now=1000)

    round1 = await db.round_matches(conn, bracket_id, 1)
    posted = [m for m in round1 if m.message_id is not None]
    assert len(posted) == 1  # the one that got out before the crash was persisted

    await db.set_round_seconds(conn, bracket_id, 300)
    await tick(conn, publisher, bracket_id, now=2000)  # "restart" later
    round1 = await db.round_matches(conn, bracket_id, 1)
    assert all(m.message_id is not None for m in round1)
    assert publisher.count("matchup") == 5  # 4 matchups + the 1 duplicate from the crash
    assert publisher.count("round_open") == 1  # header not repeated on repair
    # The repaired round gets a fresh full timer from the repair, not the crash
    assert (await db.get_bracket(conn, bracket_id)).round_closes_at == 2300


async def test_cancel(conn, bracket_id, publisher, rng):
    await setup_items(conn, bracket_id, ["A", "B"])
    await lifecycle.start_bracket(conn, bracket_id, rng)
    assert await lifecycle.cancel_bracket(conn, bracket_id)
    assert not await lifecycle.cancel_bracket(conn, bracket_id)
    assert (await db.get_bracket(conn, bracket_id)).status == "cancelled"
    await tick(conn, publisher, bracket_id)  # ticking a cancelled bracket is a no-op
    assert publisher.events == []


async def test_channel_gone_cancels_bracket(conn, bracket_id, rng):
    class GonePublisher(FakePublisher):
        async def post_round_open(self, bracket, round_no, closes_at):
            raise lifecycle.ChannelUnavailable("deleted")

    await setup_items(conn, bracket_id, ["A", "B"])
    await lifecycle.start_bracket(conn, bracket_id, rng)
    await tick(conn, GonePublisher(), bracket_id)
    assert (await db.get_bracket(conn, bracket_id)).status == "cancelled"

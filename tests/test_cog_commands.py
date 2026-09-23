"""Command-level tests for the races and failure reporting the cog guards
against: concurrent /start, setup edits vs. start, permission flips, stale
/next confirmations, honest outcomes when publishing fails, and the bounded
render cache / weak lock registry."""

import asyncio
import gc
import itertools
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bracketbot import cog as cog_module
from bracketbot import db, lifecycle
from bracketbot.cog import RENDER_CACHE_MAX, BracketCog

OWNER_ID = 10  # matches the conftest bracket fixture


class NullPublisher:
    """Publisher that succeeds silently."""

    matchup_batch_size = 1

    def __init__(self):
        self._ids = itertools.count(1000)

    async def post_round_open(self, bracket, round_no, closes_at):
        pass

    async def post_matchups(self, bracket, matches, names):
        return {match.id: next(self._ids) for match in matches}

    async def reveal_board(self, bracket, message_id, results):
        pass

    async def post_round_summary(self, bracket, round_no, results):
        pass

    async def post_champion(self, bracket, result):
        pass


class BoomPublisher(NullPublisher):
    async def post_round_open(self, bracket, round_no, closes_at):
        raise RuntimeError("discord said no")


class GonePublisher(NullPublisher):
    async def post_round_open(self, bracket, round_no, closes_at):
        raise lifecycle.ChannelUnavailable("channel deleted")


class FakeResponse:
    def __init__(self):
        self.messages = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self.messages.append((content, kwargs))

    async def defer(self, **kwargs):
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))


class FakeInteraction:
    def __init__(self, user_id=OWNER_ID, channel_id=100):
        self.user = SimpleNamespace(
            id=user_id,
            guild_permissions=SimpleNamespace(manage_channels=False, administrator=False),
        )
        self.channel_id = channel_id
        self.response = FakeResponse()
        self.followup = FakeFollowup()

    def all_messages(self):
        return [m[0] for m in self.response.messages + self.followup.messages]


class AutoConfirm:
    """Stand-in for ConfirmView: confirms immediately, optionally running a
    hook first to simulate things happening while the dialog sat open."""

    hook = None

    def __init__(self):
        self.value = True

    async def wait(self):
        if AutoConfirm.hook is not None:
            await AutoConfirm.hook()


def make_cog(conn, max_items=32, publisher=None):
    cog = BracketCog(SimpleNamespace(db=conn, config=SimpleNamespace(max_items=max_items)))
    cog.publisher = publisher or NullPublisher()
    return cog


async def add(cog, interaction, item):
    await BracketCog.add.callback(cog, interaction, item)


async def start(cog, interaction, round_minutes=None):
    await BracketCog.start.callback(cog, interaction, round_minutes)


# --- setup/start races -------------------------------------------------------


async def test_concurrent_start_single_winner(conn, bracket_id):
    cog = make_cog(conn)
    await db.add_item(conn, bracket_id, "A")
    await db.add_item(conn, bracket_id, "B")
    first, second = FakeInteraction(), FakeInteraction()

    await asyncio.gather(start(cog, first, round_minutes=5), start(cog, second, round_minutes=10))

    bracket = await db.get_bracket(conn, bracket_id)
    assert bracket.status == "running"
    outcomes = {5: first.all_messages(), 10: second.all_messages()}
    winners = [
        minutes
        for minutes, messages in outcomes.items()
        if any("round 1 is live" in m for m in messages)
    ]
    assert len(winners) == 1  # exactly one /start succeeded
    # ...and the loser did not overwrite the winner's duration
    assert bracket.round_seconds == winners[0] * 60
    loser = 10 if winners[0] == 5 else 5
    assert any("No bracket in setup" in m for m in outcomes[loser])


async def test_concurrent_adds_respect_max_items(conn, bracket_id):
    cog = make_cog(conn, max_items=4)
    for name in ("A", "B", "C"):
        await db.add_item(conn, bracket_id, name)
    first, second = FakeInteraction(user_id=1), FakeInteraction(user_id=2)

    await asyncio.gather(add(cog, first, "D"), add(cog, second, "E"))

    items = await db.list_items(conn, bracket_id)
    assert len(items) == 4  # never over the limit
    messages = first.all_messages() + second.all_messages()
    assert sum("Added" in m for m in messages) == 1
    assert sum("full" in m for m in messages) == 1


async def test_add_racing_start_cannot_land_late(conn, bracket_id):
    cog = make_cog(conn)
    await db.add_item(conn, bracket_id, "A")
    await db.add_item(conn, bracket_id, "B")
    interaction = FakeInteraction(user_id=1)

    lock = cog._lock(bracket_id)
    async with lock:
        late_add = asyncio.create_task(add(cog, interaction, "Late"))
        for _ in range(10):  # let the add pass its pre-checks and hit the lock
            await asyncio.sleep(0)
        await lifecycle.start_bracket(conn, bracket_id, random.Random(0))
    await late_add

    assert all(i.name != "Late" for i in await db.list_items(conn, bracket_id))
    assert any("locked in" in m for m in interaction.all_messages())


async def test_editmode_flip_blocks_in_flight_add(conn, bracket_id):
    cog = make_cog(conn)
    outsider = FakeInteraction(user_id=999)  # not the owner, no permissions

    lock = cog._lock(bracket_id)
    async with lock:
        blocked_add = asyncio.create_task(add(cog, outsider, "Sneaky"))
        for _ in range(10):
            await asyncio.sleep(0)
        await db.set_edit_mode(conn, bracket_id, "restricted")
    await blocked_add

    assert await db.list_items(conn, bracket_id) == []
    assert any("restricted" in m for m in outsider.all_messages())


# --- stale /next confirmations ----------------------------------------------


async def test_stale_next_confirmation_does_not_close_new_round(conn, bracket_id, monkeypatch):
    cog = make_cog(conn)
    monkeypatch.setattr(cog_module, "ConfirmView", AutoConfirm)
    for name in ("A", "B", "C", "D"):
        await db.add_item(conn, bracket_id, name)
    await start(cog, FakeInteraction())

    async def timer_fires_during_confirmation():
        # Simulates the scheduler closing round 1 and opening round 2 while
        # the confirmation dialog sat open.
        assert await lifecycle.close_round(conn, bracket_id, random.Random(0), expected_round=1)
        await cog._tick(bracket_id)

    monkeypatch.setattr(AutoConfirm, "hook", staticmethod(timer_fires_during_confirmation))
    interaction = FakeInteraction()
    try:
        await BracketCog.next_round.callback(cog, interaction)
    finally:
        monkeypatch.setattr(AutoConfirm, "hook", None)

    bracket = await db.get_bracket(conn, bracket_id)
    assert bracket.current_round == 2 and bracket.round_state == "open"  # round 2 untouched
    assert any("already closed" in m for m in interaction.all_messages())


# --- honest outcomes when publishing fails ----------------------------------


async def test_start_reports_publish_failure(conn, bracket_id):
    cog = make_cog(conn, publisher=BoomPublisher())
    await db.add_item(conn, bracket_id, "A")
    await db.add_item(conn, bracket_id, "B")
    interaction = FakeInteraction()

    await start(cog, interaction)

    assert (await db.get_bracket(conn, bracket_id)).status == "running"  # transition saved
    messages = interaction.all_messages()
    assert not any("live" in m for m in messages)
    assert any("failed" in m and "retry" in m for m in messages)


async def test_start_reports_auto_cancel(conn, bracket_id):
    cog = make_cog(conn, publisher=GonePublisher())
    await db.add_item(conn, bracket_id, "A")
    await db.add_item(conn, bracket_id, "B")
    interaction = FakeInteraction()

    await start(cog, interaction)

    assert (await db.get_bracket(conn, bracket_id)).status == "cancelled"
    assert any("cancelled" in m for m in interaction.all_messages())


async def test_next_reports_publish_failure(conn, bracket_id, monkeypatch):
    cog = make_cog(conn)
    monkeypatch.setattr(cog_module, "ConfirmView", AutoConfirm)
    for name in ("A", "B", "C", "D"):  # >2 so closing opens a next round to post
        await db.add_item(conn, bracket_id, name)
    await start(cog, FakeInteraction())

    cog.publisher = BoomPublisher()  # summary/next-round posting now fails
    interaction = FakeInteraction()
    await BracketCog.next_round.callback(cog, interaction)

    messages = interaction.all_messages()
    assert not any("results are up" in m for m in messages)
    assert any("failed" in m and "retry" in m for m in messages)


# --- bounded caches ----------------------------------------------------------


async def test_render_cache_is_bounded(conn, monkeypatch):
    import bracketbot.render as render_module

    monkeypatch.setattr(render_module, "render_bracket", lambda *a, **k: b"png")
    cog = make_cog(conn)
    for i in range(RENDER_CACHE_MAX + 8):
        bracket_id = await db.create_bracket(
            conn,
            guild_id=1,
            channel_id=1000 + i,
            owner_id=OWNER_ID,
            name=f"Bracket {i}",
            edit_mode="open",
            seeding="order",
            created_at=0,
        )
        await cog.render_png(bracket_id)
    assert len(cog._render_cache) == RENDER_CACHE_MAX


async def test_lock_registry_drops_unreferenced_locks(conn):
    cog = make_cog(conn)
    lock = cog._lock(42)
    assert cog._lock(42) is lock  # stable while referenced
    del lock
    gc.collect()
    assert 42 not in cog._locks


# --- champion image ----------------------------------------------------------


async def test_champion_image_includes_champion_cell(conn, bracket_id):
    import io

    from PIL import Image

    cog = make_cog(conn)
    await db.add_item(conn, bracket_id, "A")
    await db.add_item(conn, bracket_id, "B")
    await lifecycle.start_bracket(conn, bracket_id, random.Random(0))
    assert await lifecycle.close_round(conn, bracket_id, random.Random(0))

    # The final is decided but status='finished' is not persisted yet — the
    # champion announcement renders at exactly this point.
    normal = Image.open(io.BytesIO(await cog.render_png(bracket_id)))
    finished = Image.open(io.BytesIO(await cog.render_png(bracket_id, as_finished=True)))
    assert finished.width > normal.width  # extra column = the champion cell


# --- replying before slow work -------------------------------------------------


async def test_next_while_closing_replies_before_ticking(conn, bracket_id):
    for name in ("A", "B", "C", "D"):
        await db.add_item(conn, bracket_id, name)
    cog = make_cog(conn)
    await start(cog, FakeInteraction())
    assert await lifecycle.close_round(conn, bracket_id, random.Random(0))

    interaction = FakeInteraction()
    replied_before_publish = []

    class RecordingPublisher(NullPublisher):
        async def post_round_summary(self, bracket, round_no, results):
            replied_before_publish.append(interaction.response.is_done())

    cog.publisher = RecordingPublisher()
    await BracketCog.next_round.callback(cog, interaction)

    assert replied_before_publish == [True]
    assert any("already being closed" in m for m in interaction.all_messages())


async def test_transfer_acknowledges_while_bracket_lock_is_held(conn, bracket_id):
    cog = make_cog(conn)
    interaction = FakeInteraction()
    target = SimpleNamespace(id=20, mention="<@20>")

    lock = cog._lock(bracket_id)
    async with lock:  # e.g. a tick publishing a whole round
        task = asyncio.create_task(BracketCog.transfer.callback(cog, interaction, target))

        # The pre-lock DB lookup runs on aiosqlite's worker thread, so a fixed
        # number of event-loop yields isn't enough; poll until the defer lands.
        async def acknowledged():
            while not interaction.response.is_done():
                await asyncio.sleep(0.001)

        await asyncio.wait_for(acknowledged(), 1)
        assert not task.done()
    await task

    assert (await db.get_bracket(conn, bracket_id)).owner_id == 20
    announcement = interaction.followup.messages[-1]
    assert "now belongs to" in announcement[0]
    assert announcement[1]["ephemeral"] is False


# --- scheduler -----------------------------------------------------------------


async def _due_bracket(conn, cog, channel_id):
    bracket_id = await db.create_bracket(
        conn,
        guild_id=1,
        channel_id=channel_id,
        owner_id=OWNER_ID,
        name=f"Bracket {channel_id}",
        edit_mode="open",
        seeding="order",
        created_at=0,
    )
    for name in ("A", "B", "C", "D"):
        await db.add_item(conn, bracket_id, name)
    await lifecycle.start_bracket(conn, bracket_id, random.Random(0))
    await db.set_round_seconds(conn, bracket_id, 60)
    assert await cog._tick(bracket_id)  # posts round 1 and arms the timer
    await db.execute(conn, "UPDATE brackets SET round_closes_at = 1 WHERE id = ?", (bracket_id,))
    return bracket_id


async def test_scheduler_does_not_wait_on_a_stuck_bracket(conn):
    cog = make_cog(conn)
    stuck = await _due_bracket(conn, cog, 700)
    healthy = await _due_bracket(conn, cog, 701)

    lock = cog._lock(stuck)
    async with lock:
        await asyncio.wait_for(cog.scheduler(), 1)  # returns without waiting on ticks
        await asyncio.wait_for(cog._tick_tasks[healthy], 1)
        assert (await db.get_bracket(conn, healthy)).current_round == 2
        assert (await db.get_bracket(conn, stuck)).current_round == 1
        stuck_task = cog._tick_tasks[stuck]
        await cog.scheduler()  # a second pass must not stack another tick
        assert cog._tick_tasks[stuck] is stuck_task
    await asyncio.wait_for(stuck_task, 1)
    assert (await db.get_bracket(conn, stuck)).current_round == 2
    assert not cog._tick_tasks


# --- channel errors --------------------------------------------------------------


def _forbidden(code):
    response = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(response, {"code": code, "message": "no"})


@pytest.mark.parametrize(("code", "status"), [(50013, "running"), (50001, "cancelled")])
async def test_only_lost_channel_access_cancels(conn, bracket_id, code, status):
    class Channel:
        async def send(self, **kwargs):
            raise _forbidden(code)

    cog = BracketCog(SimpleNamespace(db=conn, get_channel=lambda _: Channel()))
    cog.render_png = AsyncMock(return_value=b"png")
    for name in ("A", "B"):
        await db.add_item(conn, bracket_id, name)
    await lifecycle.start_bracket(conn, bracket_id, random.Random(0))

    posted = await cog._tick(bracket_id)

    assert (await db.get_bracket(conn, bracket_id)).status == status
    assert posted is (status == "cancelled")  # a cancel is a completed tick


# --- render cache ----------------------------------------------------------------


async def test_render_cache_hit_skips_vote_queries(conn, bracket_id, monkeypatch):
    import bracketbot.render as render_module

    monkeypatch.setattr(render_module, "render_bracket", lambda *a, **k: b"png")
    calls = []
    real_tallies = db.tallies

    async def counting_tallies(*args):
        calls.append(args)
        return await real_tallies(*args)

    monkeypatch.setattr(db, "tallies", counting_tallies)
    cog = make_cog(conn)
    for name in ("A", "B", "C", "D"):
        await db.add_item(conn, bracket_id, name)
    await lifecycle.start_bracket(conn, bracket_id, random.Random(0))
    assert await lifecycle.close_round(conn, bracket_id, random.Random(0))
    calls.clear()

    await cog.render_png(bracket_id)
    assert len(calls) == 1
    await cog.render_png(bracket_id)
    assert len(calls) == 1  # served from cache without touching votes

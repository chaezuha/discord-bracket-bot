import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bracketbot import board_updates, db, lifecycle
from bracketbot.cog import BracketCog
from tests.test_cog_votes import FakeMessage, interaction


class Channel:
    def __init__(self):
        self.messages = {}

    async def send(self, **kwargs):
        message = FakeMessage(kwargs.get("content"))
        message.id = 9001 + len(self.messages)
        message.kwargs = kwargs
        self.messages[message.id] = message
        return message

    def get_partial_message(self, message_id):
        return self.messages[message_id]


async def setup_board(conn, bracket_id, count=32, context="guild", heavy_names=False):
    await db.execute(
        conn, "UPDATE brackets SET context_type = ? WHERE id = ?", (context, bracket_id)
    )
    for i in range(count):
        name = f"{i:02d}" + "*" * 78 if heavy_names else f"Item {i}"
        await db.add_item(conn, bracket_id, name)
    channel = Channel()
    cog = BracketCog(SimpleNamespace(db=conn, get_channel=lambda _: channel))
    cog.render_png = AsyncMock(return_value=b"png")
    await lifecycle.start_bracket(conn, bracket_id, random.Random(0))
    assert await cog._tick(bracket_id)
    matches = await db.round_matches(conn, bracket_id, 1)
    return cog, channel, matches


@pytest.mark.parametrize("context", ["guild", "bot_dm"])
@pytest.mark.parametrize(
    ("count", "sizes"), [(2, [1]), (3, [1]), (16, [8]), (20, [4]), (64, [10, 10, 10, 2])]
)
async def test_board_counts_and_component_limits(conn, bracket_id, context, count, sizes):
    cog, channel, matches = await setup_board(conn, bracket_id, count, context)
    assert len(channel.messages) == 1 + len(sizes)
    boards = [message for message in channel.messages.values() if "view" in message.kwargs]
    assert [sum(m.message_id == board.id for m in matches) for board in boards] == sizes
    for board, size in zip(boards, sizes):
        view = board.kwargs["view"]
        assert len(view.children) == size * 2
        assert max(item.row for item in view.children) <= 4
        assert len(board.content) <= 2000
        assert board.content.count("0 votes counted") == size
        assert len({item.custom_id for item in view.children}) == size * 2
    await cog.cog_unload()


async def test_names_fit_after_escaping_and_results_reveal_once(conn, bracket_id):
    cog, channel, matches = await setup_board(conn, bracket_id, heavy_names=True)
    message = channel.messages[matches[0].message_id]
    assert len(message.content) <= 2000
    assert message.content.count("**Matchup ") == 10
    # The ellipsis follows complete escaped stars, not a dangling escape.
    assert "\\…" not in message.content
    for match in matches:
        await db.cast_vote(conn, match.id, 1, "a", now=0)
    assert await lifecycle.close_round(conn, bracket_id, random.Random(0))
    assert await cog._tick(bracket_id)
    assert len(message.edits) == 1
    assert len(message.content) <= 2000
    assert "\\…" not in message.content
    assert message.content.count("**Matchup ") == 10
    assert message.content.count("beats") == 10
    assert message.edits[0]["view"] is None
    assert all(m.published for m in await db.round_matches(conn, bracket_id, 1))


async def test_one_match_legacy_message_normalizes_after_restart(conn, bracket_id, monkeypatch):
    monkeypatch.setattr(board_updates, "BOARD_UPDATE_INTERVAL_SECONDS", 0.001)
    cog, channel, matches = await setup_board(conn, bracket_id, count=2)
    message = channel.messages[matches[0].message_id]
    message.content = "**Final — Matchup 1**\n**Item 0** vs **Item 1**\n🗳️ **999 votes counted**"
    await cog.cog_unload()
    restarted = BracketCog(cog.bot)
    await restarted.handle_vote(interaction(10, message), matches[0].id, "b")
    await restarted.board_updates._workers[message.id].task
    assert "**Final — Matchups 1**" in message.content
    assert "1 vote counted" in message.content and "999" not in message.content
    assert [button.item.label for button in message.edits[-1]["view"].children] == [
        "1A · Item 0",
        "1B · Item 1",
    ]
    assert await db.tally(conn, matches[0].id) == (0, 1)


@pytest.mark.parametrize("close_round", [False, True])
async def test_hundreds_of_votes_during_throttling(conn, bracket_id, monkeypatch, close_round):
    monkeypatch.setattr(board_updates, "BOARD_UPDATE_INTERVAL_SECONDS", 0.001)
    cog, channel, matches = await setup_board(conn, bracket_id)
    message = channel.messages[matches[0].message_id]
    matches = [match for match in matches if match.message_id == message.id]
    started, release = asyncio.Event(), asyncio.Event()
    original_edit = message.edit
    attempts = 0

    async def throttled_edit(**kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            started.set()
            await release.wait()  # HTTP client retry delay, not a failed request
        await original_edit(**kwargs)

    message.edit = throttled_edit
    first = interaction(0, message)
    await cog.handle_vote(first, matches[0].id, "a")
    await asyncio.wait_for(started.wait(), 1)
    worker = cog.board_updates._workers[message.id].task
    voters = [interaction(i + 1, message) for i in range(300)]
    await asyncio.wait_for(
        asyncio.gather(
            *[cog.handle_vote(vote, matches[i % 10].id, "a") for i, vote in enumerate(voters)]
        ),
        15,
    )
    assert attempts == 1  # all additional edits coalesced during throttling
    assert all(vote.followup.receipts for vote in voters)
    assert [sum(await db.tally(conn, match.id)) for match in matches] == [31] + [30] * 9

    if close_round:

        async def close():
            async with cog._lock(bracket_id):
                return await lifecycle.close_round(conn, bracket_id, random.Random(0))

        assert await asyncio.wait_for(close(), 1)
        assert (await db.get_bracket(conn, bracket_id)).round_state == "closing"
        publish = asyncio.create_task(cog._tick(bracket_id))

        # Wait until terminal results have superseded queued counts.
        async def wait_terminal():
            while not cog.board_updates._workers[message.id].terminal:
                await asyncio.sleep(0)

        await asyncio.wait_for(wait_terminal(), 1)
        release.set()
        assert await asyncio.wait_for(publish, 2)
        assert message.content.count("beats") == 10
        assert message.edits[-1]["view"] is None
        assert "counted" not in message.content
    else:
        release.set()
        await asyncio.wait_for(worker, 2)
        assert message.content.count("31 votes counted") == 1
        assert message.content.count("30 votes counted") == 9
        assert "beats" not in message.content
    assert attempts == 2
    assert not cog.board_updates._workers


async def test_cancellation_cannot_be_overwritten_by_inflight_update(conn, bracket_id, monkeypatch):
    monkeypatch.setattr(board_updates, "BOARD_UPDATE_INTERVAL_SECONDS", 0.001)
    cog, channel, matches = await setup_board(conn, bracket_id)
    message = channel.messages[matches[0].message_id]
    started, release = asyncio.Event(), asyncio.Event()
    original_edit = message.edit

    async def blocked_edit(**kwargs):
        if kwargs.get("view") is not None:
            started.set()
            await release.wait()
        await original_edit(**kwargs)

    message.edit = blocked_edit
    await cog.handle_vote(interaction(1, message), matches[0].id, "a")
    await asyncio.wait_for(started.wait(), 1)
    await cog.handle_vote(interaction(2, message), matches[1].id, "a")
    async with cog._lock(bracket_id):
        await lifecycle.cancel_bracket(conn, bracket_id)
    disable = asyncio.create_task(
        cog._disable_open_matchups(await db.get_bracket(conn, bracket_id))
    )
    release.set()
    await asyncio.wait_for(disable, 1)
    assert message.edits[-1]["view"] is None
    assert not cog.board_updates._workers


async def test_publication_markers_are_atomic_and_retry_renders_whole_board(
    conn, bracket_id, monkeypatch
):
    cog, channel, matches = await setup_board(conn, bracket_id)
    message = channel.messages[matches[0].message_id]
    await lifecycle.close_round(conn, bracket_id, random.Random(0))
    original_mark = db.mark_published
    calls = 0

    async def crash_during_mark(conn, match_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("crash after Discord edit, during marker transaction")
        await original_mark(conn, match_id)

    monkeypatch.setattr(db, "mark_published", crash_during_mark)
    assert not await cog._tick(bracket_id)
    assert not any(m.published for m in await db.round_matches(conn, bracket_id, 1))
    first_content = message.content
    restarted = BracketCog(cog.bot)
    restarted.render_png = cog.render_png
    assert await restarted._tick(bracket_id)
    assert message.content == first_content
    assert len(message.edits) == 2
    assert all(m.published for m in await db.round_matches(conn, bracket_id, 1))


async def test_partial_legacy_markers_still_reveal_all_board_matches(conn, bracket_id):
    cog, channel, matches = await setup_board(conn, bracket_id)
    message = channel.messages[matches[0].message_id]
    await lifecycle.close_round(conn, bracket_id, random.Random(0))
    await db.mark_published(conn, matches[0].id)
    assert await cog._tick(bracket_id)
    assert len(message.edits) == 1
    assert message.content.count("**Matchup ") == 10


@pytest.mark.parametrize("fail_at", ["send", "delete"])
async def test_confirmation_failure_keeps_vote(conn, bracket_id, fail_at):
    cog, channel, matches = await setup_board(conn, bracket_id, count=2)
    message = channel.messages[matches[0].message_id]
    vote = interaction(1, message)
    error = discord.HTTPException(SimpleNamespace(status=500, reason="failure"), "failure")
    if fail_at == "send":
        vote.followup.send = AsyncMock(side_effect=error)
    else:
        vote.followup.send = AsyncMock(
            return_value=SimpleNamespace(delete=AsyncMock(side_effect=error))
        )
    await cog.handle_vote(vote, matches[0].id, "a")
    assert await db.tally(conn, matches[0].id) == (1, 0)
    await cog.cog_unload()


async def test_result_retry_after_transient_discord_failure(conn, bracket_id):
    cog, channel, matches = await setup_board(conn, bracket_id)
    message = channel.messages[matches[0].message_id]
    original_edit = message.edit
    message.edit = AsyncMock(
        side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="failure"), "failure")
    )
    await lifecycle.close_round(conn, bracket_id, random.Random(0))
    assert not await cog._tick(bracket_id)
    assert not any(m.published for m in await db.round_matches(conn, bracket_id, 1))
    message.edit = original_edit
    assert await cog._tick(bracket_id)
    assert message.content.count("**Matchup ") == 10


async def test_shared_board_changed_vote_preserves_other_totals(conn, bracket_id, monkeypatch):
    monkeypatch.setattr(board_updates, "BOARD_UPDATE_INTERVAL_SECONDS", 0.001)
    cog, channel, matches = await setup_board(conn, bracket_id)
    message = channel.messages[matches[0].message_id]
    await cog.handle_vote(interaction(1, message), matches[0].id, "a")
    await cog.handle_vote(interaction(2, message), matches[1].id, "b")
    await cog.handle_vote(interaction(1, message), matches[0].id, "b")
    await cog.board_updates._workers[message.id].task
    assert message.content.count("1 vote counted") == 2
    assert message.content.count("0 votes counted") == 8
    assert await db.tally(conn, matches[0].id) == (0, 1)
    assert await db.tally(conn, matches[1].id) == (0, 1)


async def test_board_message_associations_are_atomic(conn, bracket_id, monkeypatch):
    for i in range(16):
        await db.add_item(conn, bracket_id, f"Item {i}")
    channel = Channel()
    cog = BracketCog(SimpleNamespace(db=conn, get_channel=lambda _: channel))
    cog.render_png = AsyncMock(return_value=b"png")
    await lifecycle.start_bracket(conn, bracket_id, random.Random(0))
    original_set = db.set_message_id
    calls = 0

    async def crash_during_mapping(conn, match_id, message_id):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("crash during board association")
        await original_set(conn, match_id, message_id)

    monkeypatch.setattr(db, "set_message_id", crash_during_mapping)
    assert not await cog._tick(bracket_id)
    assert all(m.message_id is None for m in await db.round_matches(conn, bracket_id, 1))
    assert await cog._tick(bracket_id)
    matches = await db.round_matches(conn, bracket_id, 1)
    assert len({m.message_id for m in matches}) == 1
    assert matches[0].message_id is not None


async def test_rejected_vote_error_survives_board_edit_failure(conn, bracket_id):
    cog, channel, matches = await setup_board(conn, bracket_id, count=2)
    message = channel.messages[matches[0].message_id]
    await lifecycle.close_round(conn, bracket_id, random.Random(0))
    vote = interaction(1, message)
    vote.edit_original_response = AsyncMock(
        side_effect=discord.HTTPException(SimpleNamespace(status=500, reason="failure"), "failure")
    )
    await cog.handle_vote(vote, matches[0].id, "a")
    assert vote.followup.messages == [("Voting for this matchup is closed.", {"ephemeral": True})]
    vote.followup.receipts[0].delete.assert_not_awaited()
    assert await db.tally(conn, matches[0].id) == (0, 0)

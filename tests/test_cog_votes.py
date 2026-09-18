import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bracketbot import db, lifecycle
from bracketbot.cog import (
    EMBED_DESCRIPTION_LIMIT,
    VOTE_CONFIRMATION_SECONDS,
    BracketCog,
    DiscordPublisher,
)
from bracketbot.models import Match


class FakeFollowup:
    def __init__(self):
        self.messages = []
        self.receipts = []

    async def send(self, content, **kwargs):
        self.messages.append((content, kwargs))
        receipt = SimpleNamespace(delete=AsyncMock())
        self.receipts.append(receipt)
        return receipt


class FakeMessage:
    def __init__(self, content="matchup"):
        self.id = 9001
        self.content = content
        self.edits = []
        self.delete = AsyncMock()

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.content = kwargs.get("content", self.content)


def interaction(user_id, message):
    return SimpleNamespace(
        user=SimpleNamespace(id=user_id),
        message=message,
        followup=FakeFollowup(),
    )


async def open_match(conn, bracket_id):
    await db.add_item(conn, bracket_id, "Pizza")
    await db.add_item(conn, bracket_id, "Tacos")
    await lifecycle.start_bracket(conn, bracket_id, random.Random(0))
    match = (await db.round_matches(conn, bracket_id, 1))[0]
    await db.set_message_id(conn, match.id, 9001)
    return await db.get_match(conn, match.id)


async def test_post_single_match_board_starts_with_zero_votes(conn, bracket_id):
    match = await open_match(conn, bracket_id)
    publisher = BracketCog(SimpleNamespace(db=conn)).publisher
    publisher._round_label = AsyncMock(return_value="Final")
    publisher._send = AsyncMock(return_value=SimpleNamespace(id=1234))

    message_ids = await publisher.post_matchups(
        await db.get_bracket(conn, bracket_id), [match], await db.item_names(conn, bracket_id)
    )

    assert message_ids == {match.id: 1234}
    content = publisher._send.await_args.kwargs["content"]
    assert "**Final — Matchups 1**" in content
    assert "**Matchup 1:** **Pizza** vs **Tacos** · 🗳️ **0 votes counted**" in content


async def test_round_summary_chunks_stay_under_embed_limit(conn, bracket_id):
    """32 max-length escaped names must not produce a >4096-char embed
    description (Discord rejects the whole message, wedging the round)."""
    publisher = DiscordPublisher(SimpleNamespace())
    publisher._round_label = AsyncMock(return_value="Round 1")
    publisher._image = AsyncMock(return_value="IMAGE")
    sends = []

    async def record_send(bracket, **kwargs):
        sends.append(kwargs)

    publisher._send = record_send

    name = "*" * 80  # markdown-escapes to 160 characters
    results = []
    for slot in range(1, 17):
        match = Match(
            id=slot,
            bracket_id=bracket_id,
            round=1,
            slot=slot,
            item_a=1,
            item_b=2,
            winner="a",
            decided_by="votes",
            message_id=None,
            published=True,
        )
        results.append(
            lifecycle.MatchResult(match=match, a_name=name, b_name=name, votes_a=1, votes_b=0)
        )

    bracket = await db.get_bracket(conn, bracket_id)
    await publisher.post_round_summary(bracket, 1, results)

    assert len(sends) > 1  # too big for one embed, so it was split
    assert all(len(kwargs["embed"].description) <= EMBED_DESCRIPTION_LIMIT for kwargs in sends)
    assert "file" in sends[0]  # bracket image rides on the first message only
    assert all("file" not in kwargs for kwargs in sends[1:])
    joined = "\n".join(kwargs["embed"].description for kwargs in sends)
    assert joined.count("beats") == len(results)  # no result line lost


async def test_vote_total_counts_unique_voters(conn, bracket_id):
    match = await open_match(conn, bracket_id)
    cog = BracketCog(SimpleNamespace(db=conn))
    message = FakeMessage("**Pizza**  vs  **Tacos**\n🗳️ **0 votes counted**")

    first = interaction(1, message)
    await cog.handle_vote(first, match.id, "a")
    await cog.board_updates._workers[message.id].task
    assert message.content.endswith("🗳️ **1 vote counted**")
    assert "Voted for **Pizza**" in first.followup.messages[0][0]

    changed = interaction(1, message)
    await cog.handle_vote(changed, match.id, "b")
    await cog.board_updates._workers[message.id].task
    assert message.content.endswith("🗳️ **1 vote counted**")
    assert await db.tally(conn, match.id) == (0, 1)

    second = interaction(2, message)
    await cog.handle_vote(second, match.id, "a")
    await cog.board_updates._workers[message.id].task
    assert message.content.endswith("🗳️ **2 votes counted**")
    assert await db.tally(conn, match.id) == (1, 1)


@pytest.mark.parametrize("closed_by", ["round", "deadline"])
async def test_rejected_vote_does_not_update_total(conn, bracket_id, closed_by):
    match = await open_match(conn, bracket_id)
    if closed_by == "round":
        await lifecycle.close_round(conn, bracket_id, random.Random(0))
    else:
        await conn.execute("UPDATE brackets SET round_closes_at = 1 WHERE id = ?", (bracket_id,))
    cog = BracketCog(SimpleNamespace(db=conn))
    message = FakeMessage("🗳️ **0 votes counted**")
    vote = interaction(1, message)

    await cog.handle_vote(vote, match.id, "a")

    assert message.edits == []
    assert await db.tally(conn, match.id) == (0, 0)
    assert vote.followup.messages == [("Voting for this matchup is closed.", {"ephemeral": True})]


async def test_message_edit_failure_keeps_accepted_vote(conn, bracket_id, caplog):
    match = await open_match(conn, bracket_id)
    cog = BracketCog(SimpleNamespace(db=conn))

    class BrokenMessage(FakeMessage):
        async def edit(self, **kwargs):
            response = SimpleNamespace(status=500, reason="Internal Server Error")
            raise discord.HTTPException(response, {"message": "boom", "code": 0})

    message = BrokenMessage()
    vote = interaction(1, message)

    await cog.handle_vote(vote, match.id, "a")

    assert await db.tally(conn, match.id) == (1, 0)
    assert "Voted for **Pizza**" in vote.followup.messages[0][0]
    await cog.board_updates._workers[message.id].task
    assert "Could not update voting board" in caplog.text


async def test_round_reveal_cannot_be_overwritten_by_vote_total(conn, bracket_id):
    match = await open_match(conn, bracket_id)
    cog = BracketCog(SimpleNamespace(db=conn))
    count_edit_started = asyncio.Event()
    release_count_edit = asyncio.Event()

    class BlockingMessage(FakeMessage):
        async def edit(self, **kwargs):
            if "counted" in kwargs.get("content", ""):
                count_edit_started.set()
                await release_count_edit.wait()
            await super().edit(**kwargs)

    message = BlockingMessage()
    cog.publisher._channel = AsyncMock(
        return_value=SimpleNamespace(get_partial_message=lambda message_id: message)
    )
    cog.publisher.post_champion = AsyncMock()
    await cog.handle_vote(interaction(1, message), match.id, "a")
    await asyncio.wait_for(count_edit_started.wait(), timeout=2)

    # This transition must succeed while Discord's count edit is still blocked.
    async def close():
        async with cog._lock(bracket_id):
            await lifecycle.close_round(conn, bracket_id, random.Random(0))

    await asyncio.wait_for(close(), timeout=2)
    close_task = asyncio.create_task(cog._tick(bracket_id))
    await asyncio.sleep(0)
    assert not close_task.done()
    release_count_edit.set()
    assert await asyncio.wait_for(close_task, timeout=2)
    assert "Results" in message.content
    assert "Pizza** beats Tacos (1–0)" in message.content
    assert message.edits[-1]["view"] is None
    assert not cog.board_updates._workers


async def test_receipt_cleanup_never_deletes_the_public_board(conn, bracket_id):
    match = await open_match(conn, bracket_id)
    cog = BracketCog(SimpleNamespace(db=conn))
    message = FakeMessage()
    vote = interaction(1, message)
    vote.edit_original_response = message.edit
    vote.delete_original_response = AsyncMock()
    await cog.handle_vote(vote, match.id, "a")
    assert vote.followup.messages == [
        (
            "🗳️ Voted for **Pizza**",
            {
                "ephemeral": True,
                "wait": True,
            },
        )
    ]
    vote.followup.receipts[0].delete.assert_awaited_once_with(delay=VOTE_CONFIRMATION_SECONDS)
    assert VOTE_CONFIRMATION_SECONDS == 8
    vote.delete_original_response.assert_not_awaited()
    message.delete.assert_not_awaited()
    await cog.board_updates.close()

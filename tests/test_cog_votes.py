import asyncio
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bracketbot import db, lifecycle
from bracketbot.cog import BracketCog, DiscordPublisher


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, **kwargs):
        self.messages.append((content, kwargs))


class FakeMessage:
    def __init__(self, content="matchup"):
        self.id = 9001
        self.content = content
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        self.content = kwargs["content"]


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
    return (await db.round_matches(conn, bracket_id, 1))[0]


async def test_post_matchup_starts_with_zero_votes(conn, bracket_id):
    match = await open_match(conn, bracket_id)
    publisher = DiscordPublisher(SimpleNamespace())
    publisher._round_label = AsyncMock(return_value="Final")
    publisher._send = AsyncMock(return_value=SimpleNamespace(id=1234))

    message_id = await publisher.post_matchup(
        await db.get_bracket(conn, bracket_id), match, "Pizza", "Tacos"
    )

    assert message_id == 1234
    content = publisher._send.await_args.kwargs["content"]
    assert content == ("**Final — Matchup 1**\n**Pizza**  vs  **Tacos**\n🗳️ **0 votes counted**")


async def test_vote_total_counts_unique_voters(conn, bracket_id):
    match = await open_match(conn, bracket_id)
    cog = BracketCog(SimpleNamespace(db=conn))
    message = FakeMessage("**Pizza**  vs  **Tacos**\n🗳️ **0 votes counted**")

    first = interaction(1, message)
    await cog.handle_vote(first, match.id, "a")
    assert message.content.endswith("🗳️ **1 vote counted**")
    assert "You voted for **Pizza**" in first.followup.messages[0][0]

    changed = interaction(1, message)
    await cog.handle_vote(changed, match.id, "b")
    assert message.content.endswith("🗳️ **1 vote counted**")
    assert await db.tally(conn, match.id) == (0, 1)

    second = interaction(2, message)
    await cog.handle_vote(second, match.id, "a")
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
    assert "You voted for **Pizza**" in vote.followup.messages[0][0]
    assert "Could not update vote total" in caplog.text


async def test_round_reveal_cannot_be_overwritten_by_vote_total(conn, bracket_id):
    match = await open_match(conn, bracket_id)
    await db.set_message_id(conn, match.id, 9001)
    cog = BracketCog(SimpleNamespace(db=conn))
    count_edit_started = asyncio.Event()
    release_count_edit = asyncio.Event()

    class BlockingMessage(FakeMessage):
        async def edit(self, **kwargs):
            if "counted" in kwargs["content"]:
                count_edit_started.set()
                await release_count_edit.wait()
            await super().edit(**kwargs)

    message = BlockingMessage("🗳️ **0 votes counted**")
    vote = interaction(1, message)

    class ResultPublisher:
        async def reveal_result(self, bracket, result):
            await message.edit(content="RESULT REVEALED", view=None)

        async def post_champion(self, bracket, result):
            pass

    async def close_and_publish():
        async with cog._locks[bracket_id]:
            await lifecycle.close_round(conn, bracket_id, random.Random(0))
            await lifecycle.publish_round(conn, ResultPublisher(), bracket_id, now=0)

    vote_task = asyncio.create_task(cog.handle_vote(vote, match.id, "a"))
    await asyncio.wait_for(count_edit_started.wait(), timeout=1)
    close_task = asyncio.create_task(close_and_publish())
    await asyncio.sleep(0)
    assert not close_task.done()

    release_count_edit.set()
    await asyncio.gather(vote_task, close_task)

    assert message.content == "RESULT REVEALED"

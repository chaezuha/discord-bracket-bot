import itertools
import random
from types import SimpleNamespace
from unittest.mock import AsyncMock

from bracketbot import cog as cog_module
from bracketbot import db, lifecycle
from bracketbot.cog import BracketCog, help_command


class FakeMessage:
    def __init__(self, message_id, content=None, **kwargs):
        self.id = message_id
        self.content = content
        self.kwargs = kwargs
        self.edits = []


class FakeResponse:
    def __init__(self, interaction):
        self.interaction = interaction
        self._done = False
        self.messages = []
        self.defers = []

    def is_done(self):
        return self._done

    async def defer(self, **kwargs):
        self._done = True
        self.defers.append(kwargs)

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self.messages.append((content, kwargs))


class FakeFollowup:
    def __init__(self, interaction, fail_on=()):
        self.interaction = interaction
        self.fail_on = set(fail_on)
        self.calls = 0
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.calls += 1
        if self.calls in self.fail_on:
            raise RuntimeError("simulated interaction failure")
        message = FakeMessage(next(self.interaction.ids), content, **kwargs)
        self.messages.append(message)
        return message


class FakeInteraction:
    def __init__(
        self,
        *,
        channel_id=500,
        user_id=10,
        private=True,
        guild_id=None,
        guild_install=True,
        fail_followups=(),
    ):
        self.ids = itertools.count(1000)
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.context = SimpleNamespace(
            guild=guild_id is not None,
            dm_channel=guild_id is None and not private,
            private_channel=private,
        )
        self.user = SimpleNamespace(
            id=user_id,
            mention=f"<@{user_id}>",
            guild_permissions=SimpleNamespace(manage_channels=False, administrator=False),
        )
        self.response = FakeResponse(self)
        self.followup = FakeFollowup(self, fail_followups)
        self.message = None
        self.original = None
        self.original_edits = []
        self._guild_install = guild_install

    def is_guild_integration(self):
        return self._guild_install

    async def edit_original_response(self, **kwargs):
        self.original_edits.append(kwargs)
        if self.message is not None:
            if "content" in kwargs:
                self.message.content = kwargs["content"]
            self.message.edits.append(kwargs)
            return self.message
        if self.original is None:
            self.original = FakeMessage(next(self.ids), kwargs.pop("content", None), **kwargs)
        else:
            if "content" in kwargs:
                self.original.content = kwargs["content"]
            self.original.kwargs.update(kwargs)
        return self.original


def make_cog(conn, max_items=64):
    cog = BracketCog(SimpleNamespace(db=conn, config=SimpleNamespace(max_items=max_items)))
    cog.render_png = AsyncMock(return_value=b"png")
    return cog


async def private_bracket(conn, *, channel_id=500, item_count=0):
    bracket_id = await db.create_bracket(
        conn,
        guild_id=0,
        channel_id=channel_id,
        owner_id=10,
        name="Private Picks",
        edit_mode="open",
        seeding="order",
        created_at=0,
        context_type="private",
    )
    for number in range(item_count):
        await db.add_item(conn, bracket_id, f"Item {number + 1}")
    return bracket_id


async def test_command_metadata_allows_guild_dm_and_user_installs(conn):
    cog = make_cog(conn)
    for command in (cog.app_command, help_command):
        assert command.allowed_contexts.guild
        assert command.allowed_contexts.dm_channel
        assert command.allowed_contexts.private_channel
        assert command.allowed_installs.guild
        assert command.allowed_installs.user

    user_only_guild = FakeInteraction(guild_id=123, private=False, guild_install=False)
    assert not await cog.interaction_check(user_only_guild)
    assert "Add the bot" in user_only_guild.response.messages[0][0]


async def test_create_records_private_context_and_zero_guild_id(conn):
    cog = make_cog(conn)
    interaction = FakeInteraction(channel_id=501)

    await BracketCog.create.callback(cog, interaction, "DM bracket", "open", "order")

    bracket = await db.get_active_bracket(conn, 501)
    assert bracket.context_type == "private"
    assert bracket.guild_id == 0

    bot_dm = FakeInteraction(channel_id=502, private=False)
    await BracketCog.create.callback(cog, bot_dm, "Bot DM bracket", "open", "order")
    bracket = await db.get_active_bracket(conn, 502)
    assert bracket.context_type == "bot_dm"
    assert bracket.guild_id == 0


async def test_private_timer_is_rejected_without_starting(conn):
    bracket_id = await private_bracket(conn, item_count=2)
    cog = make_cog(conn)
    interaction = FakeInteraction()

    await BracketCog.start.callback(cog, interaction, 5)

    assert (await db.get_bracket(conn, bracket_id)).status == "setup"
    assert "Timers aren't available" in interaction.response.messages[0][0]


async def test_private_editor_transfer_autocomplete_and_help(conn):
    bracket_id = await private_bracket(conn)
    cog = make_cog(conn)
    target = SimpleNamespace(id=20, mention="<@20>")

    await BracketCog.editor_add.callback(cog, FakeInteraction(), target)
    assert await db.list_editors(conn, bracket_id) == [20]

    add = FakeInteraction(user_id=20)
    await BracketCog.add.callback(cog, add, "Pizza")
    choices = await cog._item_autocomplete(FakeInteraction(user_id=20), "piz")
    assert [choice.name for choice in choices] == ["Pizza"]

    await BracketCog.transfer.callback(cog, FakeInteraction(), target)
    assert (await db.get_bracket(conn, bracket_id)).owner_id == 20

    help_interaction = FakeInteraction(user_id=20)
    await help_command.callback(help_interaction)
    embed = help_interaction.response.messages[0][1]["embed"]
    assert "friend and group DMs use manual rounds" in embed.description


async def test_private_cancel_announces_through_confirmation_interaction(conn, monkeypatch):
    bracket_id = await private_bracket(conn, item_count=2)
    cog = make_cog(conn)
    confirmation = FakeInteraction()
    confirmation.response._done = True

    class AutoConfirm:
        def __init__(self):
            self.value = True
            self.interaction = confirmation

        async def wait(self):
            pass

    monkeypatch.setattr(cog_module, "ConfirmView", AutoConfirm)
    command = FakeInteraction()
    await BracketCog.cancel.callback(cog, command)

    assert (await db.get_bracket(conn, bracket_id)).status == "cancelled"
    assert "was cancelled" in confirmation.followup.messages[0].content
    assert command.followup.messages[-1].content == "Bracket cancelled."


async def test_scheduler_excludes_private_but_keeps_bot_dm(conn):
    private_id = await private_bracket(conn, channel_id=510, item_count=2)
    bot_dm_id = await db.create_bracket(
        conn,
        guild_id=0,
        channel_id=511,
        owner_id=10,
        name="Bot DM",
        edit_mode="open",
        seeding="order",
        created_at=0,
        context_type="bot_dm",
    )
    for name in ("A", "B"):
        await db.add_item(conn, bot_dm_id, name)
    await lifecycle.start_bracket(conn, private_id, random.Random(0))
    await lifecycle.start_bracket(conn, bot_dm_id, random.Random(0))

    assert {b.id for b in await db.list_running_brackets(conn)} == {private_id, bot_dm_id}
    assert [b.id for b in await db.list_schedulable_brackets(conn)] == [bot_dm_id]


async def test_private_64_item_start_batches_within_interaction_limits(conn):
    bracket_id = await private_bracket(conn, item_count=64)
    cog = make_cog(conn)
    interaction = FakeInteraction()

    await BracketCog.start.callback(cog, interaction, None)

    assert interaction.response.defers == [{"ephemeral": False}]
    assert interaction.original is not None  # round header uses the original response
    boards = [m for m in interaction.followup.messages if "view" in m.kwargs]
    assert len(boards) == 4
    assert interaction.followup.calls == 4
    assert all(len(message.kwargs["view"].children) <= 20 for message in boards)
    assert all(
        max(child.row for child in message.kwargs["view"].children) <= 4 for message in boards
    )
    assert all(len(message.content) <= 2000 for message in boards)

    matches = await db.round_matches(conn, bracket_id, 1)
    message_sizes = {}
    for match in matches:
        message_sizes[match.message_id] = message_sizes.get(match.message_id, 0) + 1
    assert sorted(message_sizes.values()) == [2, 10, 10, 10]


async def test_private_shared_board_vote_updates_and_closed_click_disables(conn):
    bracket_id = await private_bracket(conn, item_count=4)
    cog = make_cog(conn)
    start = FakeInteraction()
    await BracketCog.start.callback(cog, start, None)
    board = next(message for message in start.followup.messages if "view" in message.kwargs)
    matches = await db.round_matches(conn, bracket_id, 1)
    assert matches[0].message_id == matches[1].message_id == board.id

    vote = FakeInteraction(user_id=20)
    vote.message = board
    vote.response._done = True  # VoteButton defers before delegating to the cog.
    await cog.handle_vote(vote, matches[0].id, "a")

    assert "**Matchup 1:**" in board.content and "🗳️ **1 vote counted**" in board.content
    assert "**Matchup 2:**" in board.content and "🗳️ **0 votes counted**" in board.content
    assert "You voted" in vote.followup.messages[0].content

    await lifecycle.close_round(conn, bracket_id, random.Random(0))
    late = FakeInteraction(user_id=21)
    late.message = board
    late.response._done = True
    await cog.handle_vote(late, matches[0].id, "b")

    assert board.edits[-1]["view"] is None
    assert "closed" in late.followup.messages[0].content


async def test_private_next_uses_confirmation_interaction_to_publish(conn, monkeypatch):
    bracket_id = await private_bracket(conn, item_count=4)
    cog = make_cog(conn)
    await BracketCog.start.callback(cog, FakeInteraction(), None)

    confirmation = FakeInteraction()
    confirmation.response._done = True

    class AutoConfirm:
        def __init__(self):
            self.value = True
            self.interaction = confirmation

        async def wait(self):
            pass

    monkeypatch.setattr(cog_module, "ConfirmView", AutoConfirm)
    command = FakeInteraction()
    await BracketCog.next_round.callback(cog, command)

    bracket = await db.get_bracket(conn, bracket_id)
    assert bracket.current_round == 2 and bracket.round_state == "open"
    assert len(confirmation.followup.messages) == 3  # summary, header, final matchup
    assert command.response.messages[0][1]["ephemeral"] is True


async def test_private_64_item_next_rotates_after_five_followups(conn, monkeypatch):
    bracket_id = await private_bracket(conn)
    for number in range(64):
        await db.add_item(conn, bracket_id, "*" * 76 + f"{number:02}")
    cog = make_cog(conn)
    await BracketCog.start.callback(cog, FakeInteraction(), None)
    for match in await db.round_matches(conn, bracket_id, 1):
        await db.cast_vote(conn, match.id, 99, "a", now=0)

    confirmation = FakeInteraction()
    confirmation.response._done = True

    class AutoConfirm:
        def __init__(self):
            self.value = True
            self.interaction = confirmation

        async def wait(self):
            pass

    monkeypatch.setattr(cog_module, "ConfirmView", AutoConfirm)
    command = FakeInteraction()
    await BracketCog.next_round.callback(cog, command)

    bracket = await db.get_bracket(conn, bracket_id)
    assert bracket.current_round == 2
    assert confirmation.followup.calls == 5
    assert command.followup.calls == 1


async def test_private_partial_start_is_repaired_by_show(conn):
    bracket_id = await private_bracket(conn, item_count=64)
    cog = make_cog(conn)
    broken = FakeInteraction(fail_followups={2})

    await BracketCog.start.callback(cog, broken, None)

    matches = await db.round_matches(conn, bracket_id, 1)
    assert sum(match.message_id is not None for match in matches) == 10
    assert "saved" in broken.followup.messages[-1].content

    retry = FakeInteraction()
    await BracketCog.show.callback(cog, retry)

    matches = await db.round_matches(conn, bracket_id, 1)
    assert all(match.message_id is not None for match in matches)
    assert retry.original is not None
    assert len([m for m in retry.followup.messages if "view" in m.kwargs]) == 2

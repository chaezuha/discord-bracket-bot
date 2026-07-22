"""Slash commands, the round scheduler, and the Discord-side publisher."""

from __future__ import annotations

import asyncio
import dataclasses
import io
import logging
import random
import time
import weakref
from collections import OrderedDict
from typing import Literal

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import db, lifecycle, logic, render
from .lifecycle import ChannelUnavailable, MatchResult
from .models import CANCELLED, FINISHED, RUNNING, SETUP, Bracket, Match
from .views import ConfirmView, vote_board_view, vote_count_line, vote_view, with_vote_count

log = logging.getLogger(__name__)

# OS-entropy randomness for coin flips and shuffles — unbiased and unseedable.
_rng = random.SystemRandom()

SCHEDULER_INTERVAL_SECONDS = 10

# Discord size limits and how many rendered PNGs to keep around.
EMBED_DESCRIPTION_LIMIT = 4096
MESSAGE_CONTENT_LIMIT = 2000
RENDER_CACHE_MAX = 32
PRIVATE_BOARD_MATCHES = 10
PRIVATE_FOLLOWUP_LIMIT = 5

BAD_NAME_MESSAGE = (
    f"That name is empty, longer than {logic.MAX_NAME_LENGTH} characters, "
    "or contains unsupported characters."
)


def _esc(name: str | None) -> str:
    return discord.utils.escape_markdown(name or "?")


def _context_type(interaction: discord.Interaction) -> str:
    if getattr(interaction, "guild_id", None) is not None:
        return "guild"
    context = getattr(interaction, "context", None)
    if context is not None and context.private_channel:
        return "private"
    return "bot_dm"


def _is_manager(bracket: Bracket, user: discord.abc.User) -> bool:
    """Bracket owner, or a moderator (Manage Channels / Administrator)."""
    if user.id == bracket.owner_id:
        return True
    perms = getattr(user, "guild_permissions", None)
    return bool(perms and (perms.manage_channels or perms.administrator))


class DiscordPublisher:
    """lifecycle.Publisher backed by a real Discord channel.

    Per-message failures (deleted message, missing permission on one edit) are
    logged and swallowed; only an unusable channel raises ChannelUnavailable,
    which makes the lifecycle auto-cancel the bracket.
    """

    matchup_batch_size = 1

    def __init__(self, cog: BracketCog) -> None:
        self.cog = cog

    async def _channel(self, bracket: Bracket) -> discord.abc.Messageable:
        channel = self.cog.bot.get_channel(bracket.channel_id)
        if channel is None:
            try:
                channel = await self.cog.bot.fetch_channel(bracket.channel_id)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise ChannelUnavailable(str(exc)) from exc
        return channel

    async def _send(self, bracket: Bracket, **kwargs) -> discord.Message:
        channel = await self._channel(bracket)
        try:
            return await channel.send(**kwargs)
        except discord.Forbidden as exc:
            raise ChannelUnavailable(str(exc)) from exc

    async def _image(self, bracket: Bracket, *, as_finished: bool = False) -> discord.File:
        data = await self.cog.render_png(bracket.id, as_finished=as_finished)
        return discord.File(io.BytesIO(data), filename=f"bracket-{bracket.id}.png")

    async def _round_label(self, bracket: Bracket, round_no: int) -> str:
        first_round = await db.round_matches(self.cog.bot.db, bracket.id, 1)
        total = logic.round_count(2 * len(first_round))
        return logic.round_label(round_no, total)

    def _result_line(self, result: MatchResult) -> str:
        m = result.match
        if m.decided_by == "bye":
            return f"⏩ **{_esc(result.winner_name)}** advances (bye)"
        winner = _esc(result.winner_name)
        a, b = _esc(result.a_name), _esc(result.b_name)
        score = f"{result.votes_a}–{result.votes_b}"
        if m.decided_by == "coinflip":
            return f"🪙 Tied {score} — **{winner}** advances by coin flip"
        loser = b if m.winner == "a" else a
        return f"✅ **{winner}** beats {loser} ({score})"

    async def post_round_open(self, bracket: Bracket, round_no: int, closes_at: int | None) -> None:
        label = await self._round_label(bracket, round_no)
        lines = ["Vote in the matchups below!"]
        if closes_at is not None:
            lines.append(f"Voting closes <t:{closes_at}:R>.")
        else:
            lines.append("The round ends when the owner runs `/bracket next`.")
        embed = discord.Embed(
            title=f"{_esc(bracket.name)} — {label}",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_image(url=f"attachment://bracket-{bracket.id}.png")
        await self._send(bracket, embed=embed, file=await self._image(bracket))

    async def post_matchups(
        self, bracket: Bracket, matches: list[Match], names: dict[int, str]
    ) -> dict[int, int]:
        message_ids = {}
        for match in matches:
            a_name = names.get(match.item_a, "?")
            b_name = names.get(match.item_b, "?")
            message_ids[match.id] = await self.post_matchup(bracket, match, a_name, b_name)
        return message_ids

    async def post_matchup(self, bracket: Bracket, match: Match, a_name: str, b_name: str) -> int:
        label = await self._round_label(bracket, match.round)
        message = await self._send(
            bracket,
            content=with_vote_count(
                f"**{label} — Matchup {match.slot}**\n**{_esc(a_name)}**  vs  **{_esc(b_name)}**",
                0,
            ),
            view=vote_view(match.id, a_name, b_name),
        )
        return message.id

    async def reveal_result(self, bracket: Bracket, result: MatchResult) -> None:
        match = result.match
        label = await self._round_label(bracket, match.round)
        content = f"**{label} — Matchup {match.slot}**\n{self._result_line(result)}"
        channel = await self._channel(bracket)
        try:
            await channel.get_partial_message(match.message_id).edit(content=content, view=None)
        except discord.HTTPException as exc:
            log.warning("Could not reveal result on message %s: %s", match.message_id, exc)

    async def post_round_summary(
        self, bracket: Bracket, round_no: int, results: list[MatchResult]
    ) -> None:
        label = await self._round_label(bracket, round_no)
        # Escaped 80-char names make long summaries; embed descriptions cap at
        # 4096 chars and Discord rejects the whole message past that.
        chunks = logic.chunk_lines([self._result_line(r) for r in results], EMBED_DESCRIPTION_LIMIT)
        for i, chunk in enumerate(chunks):
            part = f" ({i + 1}/{len(chunks)})" if len(chunks) > 1 else ""
            embed = discord.Embed(
                title=f"{_esc(bracket.name)} — {label} results{part}",
                description=chunk,
                color=discord.Color.green(),
            )
            if i == 0:
                embed.set_image(url=f"attachment://bracket-{bracket.id}.png")
                await self._send(bracket, embed=embed, file=await self._image(bracket))
            else:
                await self._send(bracket, embed=embed)

    async def post_champion(self, bracket: Bracket, result: MatchResult) -> None:
        embed = discord.Embed(
            title=f"🏆 {_esc(result.winner_name)} wins {_esc(bracket.name)}!",
            description=self._result_line(result),
            color=discord.Color.gold(),
        )
        embed.set_image(url=f"attachment://bracket-{bracket.id}.png")
        # The announcement goes out before status='finished' is persisted (a
        # crash in between must duplicate the announcement, never lose it), so
        # ask for the finished look explicitly to get the champion cell.
        await self._send(bracket, embed=embed, file=await self._image(bracket, as_finished=True))


class InteractionPublisher(DiscordPublisher):
    """Publisher for user-installed apps in DMs/GDMs the bot cannot join.

    Discord permits one response and five follow-ups per interaction. A /next
    confirmation supplies a second token, so the publisher rotates tokens if a
    worst-case 64-item results post needs a sixth public message.
    """

    matchup_batch_size = PRIVATE_BOARD_MATCHES

    def __init__(
        self,
        cog: BracketCog,
        interactions: list[discord.Interaction],
        *,
        use_original: bool,
    ) -> None:
        super().__init__(cog)
        self.interactions = interactions
        self.use_original = use_original
        self._original_used = False
        self._followups = [0] * len(interactions)

    async def _send(self, bracket: Bracket, **kwargs) -> discord.Message:  # noqa: ARG002
        if self.use_original and not self._original_used:
            self._original_used = True
            interaction = self.interactions[0]
            file = kwargs.pop("file", None)
            if file is not None:
                kwargs["attachments"] = [file]
            return await interaction.edit_original_response(**kwargs)

        for index, interaction in enumerate(self.interactions):
            if self._followups[index] >= PRIVATE_FOLLOWUP_LIMIT:
                continue
            self._followups[index] += 1
            return await interaction.followup.send(wait=True, **kwargs)
        raise RuntimeError("private interaction exhausted its Discord follow-up limit")

    async def post_matchups(
        self, bracket: Bracket, matches: list[Match], names: dict[int, str]
    ) -> dict[int, int]:
        message_ids = {}
        for offset in range(0, len(matches), PRIVATE_BOARD_MATCHES):
            board = matches[offset : offset + PRIVATE_BOARD_MATCHES]
            message = await self._send(
                bracket,
                content=await self.cog.vote_board_content(bracket, board),
                view=vote_board_view(board, names),
            )
            message_ids.update((match.id, message.id) for match in board)
        return message_ids

    async def reveal_result(self, bracket: Bracket, result: MatchResult) -> None:
        # Interaction response messages cannot be edited with a later token.
        # Results are posted as a fresh summary; stale boards self-disable if clicked.
        return None


@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
class BracketCog(commands.GroupCog, name="bracket"):
    """Tournament-style voting brackets."""

    editor = app_commands.Group(name="editor", description="Manage who may edit the bracket")

    def __init__(self, bot) -> None:
        self.bot = bot
        self.publisher = DiscordPublisher(self)
        # Entries vanish on their own once no task references them; never
        # evict manually (a waiter could keep an orphaned lock while a new
        # task gets a fresh one, breaking mutual exclusion).
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
        self._render_sem = asyncio.Semaphore(2)
        # LRU of rendered PNGs, bounded so a long-running bot doesn't keep an
        # image for every bracket it ever drew (incl. finished ones re-shown).
        self._render_cache: OrderedDict[int, tuple[tuple, bytes]] = OrderedDict()

    def _lock(self, bracket_id: int) -> asyncio.Lock:
        """Per-bracket lifecycle lock. Callers must keep the returned lock in
        a local variable for the whole `async with` span — the registry holds
        only weak references, so the local is what keeps the entry alive."""
        lock = self._locks.get(bracket_id)
        if lock is None:  # safe get-or-create: no await between lookup and store
            lock = asyncio.Lock()
            self._locks[bracket_id] = lock
        return lock

    async def cog_load(self) -> None:
        self.scheduler.start()

    async def cog_unload(self) -> None:
        self.scheduler.cancel()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """A user install may expose commands in guilds without installing the bot.

        That surface cannot support the channel publisher or server permission
        model, so require the normal guild installation there.
        """
        if interaction.guild_id is not None and not interaction.is_guild_integration():
            await self._fail(
                interaction,
                "Add the bot to this server before using brackets here. "
                "User installs are for DMs and group chats.",
            )
            return False
        return True

    # --- scheduler / lifecycle glue ---------------------------------------

    @tasks.loop(seconds=SCHEDULER_INTERVAL_SECONDS)
    async def scheduler(self) -> None:
        """Closes due rounds and resumes any half-published state; the first
        pass after startup doubles as crash recovery."""
        try:
            brackets = await db.list_schedulable_brackets(self.bot.db)
        except Exception:
            log.exception("Scheduler could not list brackets")
            return
        for bracket in brackets:
            await self._tick(bracket.id)

    @scheduler.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick(self, bracket_id: int, publisher: lifecycle.Publisher | None = None) -> bool:
        """One lifecycle tick; False if it failed (the scheduler will retry).
        Interactive callers use the result to avoid claiming success."""
        lock = self._lock(bracket_id)
        async with lock:
            try:
                await lifecycle.tick(
                    self.bot.db,
                    publisher or self.publisher,
                    bracket_id,
                    now=int(time.time()),
                    rng=_rng,
                )
            except Exception:
                log.exception("Tick failed for bracket %s (will retry)", bracket_id)
                return False
        return True

    async def handle_vote(
        self, interaction: discord.Interaction, match_id: int, choice: str
    ) -> None:
        """Record a vote and update its public total under the lifecycle lock."""
        response = getattr(interaction, "response", None)
        edit_original = getattr(interaction, "edit_original_response", None)
        match = await db.get_match(self.bot.db, match_id)
        if match is None:
            if response is None or response.is_done():
                await interaction.followup.send(
                    "Voting for this matchup is closed.", ephemeral=True
                )
            else:
                await response.send_message("Voting for this matchup is closed.", ephemeral=True)
            return

        lock = self._lock(match.bracket_id)
        async with lock:
            bracket = await db.get_bracket(self.bot.db, match.bracket_id)
            accepted = await db.cast_vote(
                self.bot.db, match_id, interaction.user.id, choice, int(time.time())
            )
            if accepted:
                votes_a, votes_b = await db.tally(self.bot.db, match_id)
                names = await db.item_names(self.bot.db, match.bracket_id)
                item_id = match.item_a if choice == "a" else match.item_b
                name = _esc(names.get(item_id))
                message = interaction.message
                if message is not None:
                    try:
                        if bracket.context_type == "private":
                            board = await db.matches_for_message(self.bot.db, message.id)
                            content = await self.vote_board_content(bracket, board)
                        else:
                            content = with_vote_count(message.content, votes_a + votes_b)
                        if edit_original is None:
                            await message.edit(content=content)
                        else:
                            await edit_original(content=content)
                    except discord.HTTPException as exc:
                        log.warning(
                            "Could not update vote total on message %s: %s",
                            getattr(message, "id", "?"),
                            exc,
                        )

        if not accepted:
            if edit_original is not None and interaction.message is not None:
                try:
                    await edit_original(view=None)
                except discord.HTTPException:
                    pass
            if response is None or response.is_done():
                await interaction.followup.send(
                    "Voting for this matchup is closed.", ephemeral=True
                )
            else:
                await response.send_message("Voting for this matchup is closed.", ephemeral=True)
            return
        confirmation = (
            f"🗳️ You voted for **{name}** — you can change your vote until the round ends."
        )
        if response is None or response.is_done():
            await interaction.followup.send(confirmation, ephemeral=True)
        else:
            await response.send_message(confirmation, ephemeral=True)

    async def vote_board_content(self, bracket: Bracket, matches: list[Match]) -> str:
        """Render compact shared-board totals without repeating long item names."""
        if not matches:
            return "No open matchups."
        first_round = await db.round_matches(self.bot.db, bracket.id, 1)
        total_rounds = logic.round_count(2 * len(first_round))
        label = logic.round_label(matches[0].round, total_rounds)
        first_slot, last_slot = matches[0].slot, matches[-1].slot
        span = str(first_slot) if first_slot == last_slot else f"{first_slot}–{last_slot}"
        lines = [f"**{label} — Matchups {span}**", "Choose a labeled button below:"]
        names = await db.item_names(self.bot.db, bracket.id)
        for match in matches:
            votes_a, votes_b = await db.tally(self.bot.db, match.id)
            a_name = logic.truncate(_esc(names.get(match.item_a)), 60)
            b_name = logic.truncate(_esc(names.get(match.item_b)), 60)
            lines.append(
                f"**Matchup {match.slot}:** **{a_name}** vs **{b_name}** · "
                f"{vote_count_line(votes_a + votes_b)}"
            )
        return "\n".join(lines)

    async def render_png(self, bracket_id: int, *, as_finished: bool = False) -> bytes:
        """Render the bracket; as_finished draws the champion cell even though
        status='finished' isn't persisted yet (the champion announcement)."""
        conn = self.bot.db
        bracket = await db.get_bracket(conn, bracket_id)
        if as_finished:
            bracket = dataclasses.replace(bracket, status=FINISHED)
        items = await db.item_names(conn, bracket_id)
        matches = await db.list_matches(conn, bracket_id)
        votes = {
            m.id: await db.tally(conn, m.id)
            for m in matches
            if m.winner is not None and m.decided_by in ("votes", "coinflip")
        }
        key = (
            bracket.status,
            bracket.current_round,
            bracket.round_state,
            tuple((m.id, m.winner) for m in matches),
        )
        cached = self._render_cache.get(bracket_id)
        if cached and cached[0] == key:
            self._render_cache.move_to_end(bracket_id)
            return cached[1]
        async with self._render_sem:
            data = await asyncio.to_thread(render.render_bracket, bracket, items, matches, votes)
        self._render_cache[bracket_id] = (key, data)
        self._render_cache.move_to_end(bracket_id)
        while len(self._render_cache) > RENDER_CACHE_MAX:
            self._render_cache.popitem(last=False)
        return data

    # --- command helpers ---------------------------------------------------

    async def _active(self, interaction: discord.Interaction) -> Bracket | None:
        return await db.get_active_bracket(self.bot.db, interaction.channel_id)

    def _interaction_publisher(
        self,
        interaction: discord.Interaction,
        *,
        use_original: bool,
        fallback: discord.Interaction | None = None,
    ) -> InteractionPublisher:
        interactions = [interaction]
        if fallback is not None and fallback is not interaction:
            interactions.append(fallback)
        return InteractionPublisher(self, interactions, use_original=use_original)

    async def _needs_post_repair(self, bracket: Bracket) -> bool:
        if bracket.status != RUNNING:
            return False
        if bracket.round_state == "closing":
            return True
        matches = await db.round_matches(self.bot.db, bracket.id, bracket.current_round)
        return any(not match.is_bye and match.message_id is None for match in matches)

    @staticmethod
    async def _fail(interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _can_edit(self, bracket: Bracket, user: discord.abc.User) -> bool:
        if bracket.edit_mode == "open" or _is_manager(bracket, user):
            return True
        return user.id in await db.list_editors(self.bot.db, bracket.id)

    async def _editable_bracket(self, interaction: discord.Interaction) -> Bracket | None:
        """Shared preamble for add/rename/remove: active, in setup, editable."""
        bracket = await self._active(interaction)
        if bracket is None:
            await self._fail(
                interaction, "No bracket in this channel. Start one with `/bracket create`."
            )
            return None
        if bracket.status != SETUP:
            await self._fail(interaction, "The bracket has already started — items are locked in.")
            return None
        if not await self._can_edit(bracket, interaction.user):
            await self._fail(interaction, "Editing is restricted on this bracket.")
            return None
        return bracket

    async def _recheck_editable(
        self, interaction: discord.Interaction, bracket_id: int
    ) -> Bracket | None:
        """Second half of the edit preamble, run under the bracket lock: the
        _editable_bracket checks ran before the lock, so status and edit
        permissions may have changed in between."""
        bracket = await db.get_bracket(self.bot.db, bracket_id)
        if bracket is None or bracket.status != SETUP:
            await self._fail(interaction, "The bracket has already started — items are locked in.")
            return None
        if not await self._can_edit(bracket, interaction.user):
            await self._fail(interaction, "Editing is restricted on this bracket.")
            return None
        return bracket

    async def _recheck_manager(
        self, interaction: discord.Interaction, bracket_id: int
    ) -> Bracket | None:
        """Re-validate an owner/moderator command under the bracket lock, so
        ownership or status changes can't slip between check and write."""
        bracket = await db.get_bracket(self.bot.db, bracket_id)
        if bracket is None or bracket.status not in (SETUP, RUNNING):
            await self._fail(interaction, "That bracket already ended.")
            return None
        if not _is_manager(bracket, interaction.user):
            await self._fail(interaction, "Only the bracket owner or a moderator can do that.")
            return None
        return bracket

    async def _item_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        bracket = await db.get_active_bracket(self.bot.db, interaction.channel_id)
        if bracket is None:
            return []
        needle = current.casefold()
        items = await db.list_items(self.bot.db, bracket.id)
        return [
            app_commands.Choice(name=item.name, value=str(item.id))
            for item in items
            if needle in item.name.casefold()
        ][:25]

    # --- setup commands ----------------------------------------------------

    @app_commands.command(description="Create a new voting bracket in this channel")
    @app_commands.describe(
        name="What the bracket is about",
        edit_mode="Who may add/rename/remove items (default: open to everyone)",
        seeding="Matchup order: as added, or shuffled at start (default: order)",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        edit_mode: Literal["open", "restricted"] = "open",
        seeding: Literal["order", "shuffle"] = "order",
    ) -> None:
        normalized = logic.normalize_name(name)
        if normalized is None:
            await self._fail(interaction, BAD_NAME_MESSAGE)
            return
        try:
            await db.create_bracket(
                self.bot.db,
                guild_id=interaction.guild_id or 0,
                channel_id=interaction.channel_id,
                owner_id=interaction.user.id,
                name=normalized,
                edit_mode=edit_mode,
                seeding=seeding,
                created_at=int(time.time()),
                context_type=_context_type(interaction),
            )
        except aiosqlite.IntegrityError:
            await self._fail(
                interaction,
                "This channel already has an active bracket. Finish it or `/bracket cancel` first.",
            )
            return
        embed = discord.Embed(
            title=f"🗳️ New bracket: {_esc(normalized)}",
            description=(
                f"Created by {interaction.user.mention}.\n"
                f"Add contenders with `/bracket add` "
                f"({'anyone can edit' if edit_mode == 'open' else 'only the owner and editors'}), "
                f"then `/bracket start` to begin voting."
            ),
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(description="Add an item to the bracket")
    @app_commands.describe(item="The contender to add")
    async def add(self, interaction: discord.Interaction, item: str) -> None:
        bracket = await self._editable_bracket(interaction)
        if bracket is None:
            return
        name = logic.normalize_name(item)
        if name is None:
            await self._fail(interaction, BAD_NAME_MESSAGE)
            return
        lock = self._lock(bracket.id)
        async with lock:
            bracket = await self._recheck_editable(interaction, bracket.id)
            if bracket is None:
                return
            items = await db.list_items(self.bot.db, bracket.id)
            if len(items) >= self.bot.config.max_items:
                await self._fail(
                    interaction, f"This bracket is full ({self.bot.config.max_items} items)."
                )
                return
            try:
                await db.add_item(self.bot.db, bracket.id, name)
            except aiosqlite.IntegrityError:
                await self._fail(interaction, f"**{_esc(name)}** is already in the bracket.")
                return
        await interaction.response.send_message(
            f"Added **{_esc(name)}** — {len(items) + 1} item(s) so far.", ephemeral=True
        )

    @app_commands.command(description="Rename an item")
    @app_commands.describe(item="The item to rename", new_name="Its new name")
    @app_commands.autocomplete(item=_item_autocomplete)
    async def rename(self, interaction: discord.Interaction, item: str, new_name: str) -> None:
        bracket = await self._editable_bracket(interaction)
        if bracket is None:
            return
        name = logic.normalize_name(new_name)
        if name is None:
            await self._fail(interaction, BAD_NAME_MESSAGE)
            return
        lock = self._lock(bracket.id)
        async with lock:
            bracket = await self._recheck_editable(interaction, bracket.id)
            if bracket is None:
                return
            # Resolve the target under the lock so a concurrent rename/remove
            # can't swap it out from under us.
            found = await db.find_item(self.bot.db, bracket.id, item)
            if found is None:
                await self._fail(interaction, "No such item in this bracket.")
                return
            try:
                await db.rename_item(self.bot.db, found.id, name)
            except aiosqlite.IntegrityError:
                await self._fail(interaction, f"**{_esc(name)}** is already in the bracket.")
                return
        await interaction.response.send_message(
            f"Renamed **{_esc(found.name)}** to **{_esc(name)}**.", ephemeral=True
        )

    @app_commands.command(description="Remove an item from the bracket")
    @app_commands.describe(item="The item to remove")
    @app_commands.autocomplete(item=_item_autocomplete)
    async def remove(self, interaction: discord.Interaction, item: str) -> None:
        bracket = await self._editable_bracket(interaction)
        if bracket is None:
            return
        lock = self._lock(bracket.id)
        async with lock:
            bracket = await self._recheck_editable(interaction, bracket.id)
            if bracket is None:
                return
            found = await db.find_item(self.bot.db, bracket.id, item)
            if found is None:
                await self._fail(interaction, "No such item in this bracket.")
                return
            await db.remove_item(self.bot.db, found.id)
        await interaction.response.send_message(f"Removed **{_esc(found.name)}**.", ephemeral=True)

    @app_commands.command(description="List the items in this channel's bracket")
    async def items(self, interaction: discord.Interaction) -> None:
        bracket = await self._active(interaction)
        if bracket is None:
            await self._fail(interaction, "No active bracket in this channel.")
            return
        items = await db.list_items(self.bot.db, bracket.id)
        listing = (
            logic.clamp_lines(
                [f"{i}. {_esc(item.name)}" for i, item in enumerate(items, 1)],
                EMBED_DESCRIPTION_LIMIT,
            )
            or "*none yet*"
        )
        embed = discord.Embed(
            title=f"{_esc(bracket.name)} — {len(items)} item(s)",
            description=listing,
            color=discord.Color.blurple(),
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- permission commands ------------------------------------------------

    @app_commands.command(description="Switch between open and restricted editing")
    @app_commands.describe(mode="open: anyone may edit items; restricted: owner + editors only")
    async def editmode(
        self, interaction: discord.Interaction, mode: Literal["open", "restricted"]
    ) -> None:
        bracket = await self._active(interaction)
        if bracket is None:
            await self._fail(interaction, "No active bracket in this channel.")
            return
        lock = self._lock(bracket.id)
        async with lock:
            if await self._recheck_manager(interaction, bracket.id) is None:
                return
            await db.set_edit_mode(self.bot.db, bracket.id, mode)
        await interaction.response.send_message(f"Edit mode is now **{mode}**.", ephemeral=True)

    @editor.command(name="add", description="Allow a user to edit the bracket when restricted")
    async def editor_add(self, interaction: discord.Interaction, user: discord.User) -> None:
        bracket = await self._active(interaction)
        if bracket is None:
            await self._fail(interaction, "No active bracket in this channel.")
            return
        lock = self._lock(bracket.id)
        async with lock:
            if await self._recheck_manager(interaction, bracket.id) is None:
                return
            await db.add_editor(self.bot.db, bracket.id, user.id)
        await interaction.response.send_message(
            f"{user.mention} can now edit items.", ephemeral=True
        )

    @editor.command(name="remove", description="Take a user's edit access away")
    async def editor_remove(self, interaction: discord.Interaction, user: discord.User) -> None:
        bracket = await self._active(interaction)
        if bracket is None:
            await self._fail(interaction, "No active bracket in this channel.")
            return
        lock = self._lock(bracket.id)
        async with lock:
            if await self._recheck_manager(interaction, bracket.id) is None:
                return
            removed = await db.remove_editor(self.bot.db, bracket.id, user.id)
        await interaction.response.send_message(
            f"{user.mention} {'no longer has' if removed else 'did not have'} edit access.",
            ephemeral=True,
        )

    @app_commands.command(description="Hand bracket ownership to someone else")
    async def transfer(self, interaction: discord.Interaction, user: discord.User) -> None:
        bracket = await self._active(interaction)
        if bracket is None:
            await self._fail(interaction, "No active bracket in this channel.")
            return
        lock = self._lock(bracket.id)
        async with lock:
            if await self._recheck_manager(interaction, bracket.id) is None:
                return
            await db.set_owner(self.bot.db, bracket.id, user.id)
        await interaction.response.send_message(
            f"**{_esc(bracket.name)}** now belongs to {user.mention}.",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    # --- round control ------------------------------------------------------

    @app_commands.command(description="Lock the items and start round 1")
    @app_commands.describe(
        round_minutes="Automatic round length in minutes (omit for manual `/bracket next` only)"
    )
    async def start(
        self,
        interaction: discord.Interaction,
        round_minutes: app_commands.Range[int, 1, 10080] | None = None,
    ) -> None:
        bracket = await self._active(interaction)
        if bracket is None or bracket.status != SETUP:
            await self._fail(interaction, "No bracket in setup in this channel.")
            return
        is_private = bracket.context_type == "private"
        if is_private and round_minutes is not None:
            await self._fail(
                interaction,
                "Timers aren't available in friend or group DMs. "
                "Start without `round_minutes`, then use `/bracket next`.",
            )
            return
        if not _is_manager(bracket, interaction.user):
            await self._fail(interaction, "Only the bracket owner or a moderator can start it.")
            return
        items = await db.list_items(self.bot.db, bracket.id)
        if len(items) < 2:
            await self._fail(interaction, "Add at least 2 items before starting.")
            return
        await interaction.response.defer(ephemeral=not is_private)
        lock = self._lock(bracket.id)
        async with lock:
            # Everything above ran outside the lock; a concurrent /start or
            # edit may have won the race, so re-check before writing.
            fresh = await db.get_bracket(self.bot.db, bracket.id)
            if fresh is None or fresh.status != SETUP or not _is_manager(fresh, interaction.user):
                await self._fail(interaction, "No bracket in setup in this channel.")
                return
            if len(await db.list_items(self.bot.db, bracket.id)) < 2:
                await self._fail(interaction, "Add at least 2 items before starting.")
                return
            await db.set_round_seconds(
                self.bot.db, bracket.id, round_minutes * 60 if round_minutes else None
            )
            await lifecycle.start_bracket(self.bot.db, bracket.id, _rng)
        publisher = (
            self._interaction_publisher(interaction, use_original=True) if is_private else None
        )
        posted = await self._tick(bracket.id, publisher)
        if is_private:
            if not posted:
                await interaction.followup.send(
                    "The bracket is saved, but Discord stopped the round from posting. "
                    "Run `/bracket show` to retry.",
                    ephemeral=True,
                )
            return
        await interaction.followup.send(
            await self._publish_outcome(bracket.id, posted, "Bracket started — round 1 is live!"),
            ephemeral=True,
        )

    @app_commands.command(name="next", description="Close the current round now and move on")
    async def next_round(self, interaction: discord.Interaction) -> None:
        bracket = await self._active(interaction)
        if bracket is None or bracket.status != RUNNING:
            await self._fail(interaction, "No running bracket in this channel.")
            return
        if not _is_manager(bracket, interaction.user):
            await self._fail(interaction, "Only the bracket owner or a moderator can do that.")
            return
        is_private = bracket.context_type == "private"
        if is_private and await self._needs_post_repair(bracket):
            await interaction.response.defer()
            posted = await self._tick(
                bracket.id, self._interaction_publisher(interaction, use_original=True)
            )
            if not posted:
                await interaction.followup.send(
                    "The bracket is still saved, but Discord stopped recovery. Try again.",
                    ephemeral=True,
                )
            return
        if bracket.round_state != "open":
            await self._tick(bracket.id)  # nudge a stuck publish along
            await self._fail(interaction, "That round is already being closed.")
            return
        deadline = (
            f"It would otherwise close <t:{bracket.round_closes_at}:R>."
            if bracket.round_closes_at
            else "It has no timer."
        )
        view = ConfirmView()
        await interaction.response.send_message(
            f"Round {bracket.current_round} is still open. {deadline}\n"
            "Close it now and count the votes?",
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if not view.value:
            return
        # The confirmation can sit for up to a minute; by the time it lands the
        # timer may have closed this round (and opened the next one) or the
        # bracket may have changed hands, so re-check everything under the lock.
        expected_round = bracket.current_round
        lock = self._lock(bracket.id)
        async with lock:
            fresh = await db.get_bracket(self.bot.db, bracket.id)
            if fresh is None or fresh.status != RUNNING or fresh.current_round != expected_round:
                await interaction.followup.send(
                    "That round already closed while the confirmation was pending.",
                    ephemeral=True,
                )
                return
            if not _is_manager(fresh, interaction.user):
                await interaction.followup.send(
                    "Only the bracket owner or a moderator can do that.", ephemeral=True
                )
                return
            closed = await lifecycle.close_round(
                self.bot.db, bracket.id, _rng, expected_round=expected_round
            )
        publisher = None
        if is_private:
            confirmed_interaction = view.interaction or interaction
            publisher = self._interaction_publisher(
                confirmed_interaction,
                use_original=False,
                fallback=interaction if confirmed_interaction is not interaction else None,
            )
        posted = await self._tick(bracket.id, publisher)
        if is_private:
            if not posted:
                await interaction.followup.send(
                    "The round result is saved, but Discord stopped it from posting. "
                    "Run `/bracket next` or `/bracket show` to retry.",
                    ephemeral=True,
                )
            return
        if not closed:
            message = "That round was already closed."
        else:
            message = await self._publish_outcome(
                bracket.id, posted, "Round closed — results are up."
            )
        await interaction.followup.send(message, ephemeral=True)

    async def _publish_outcome(self, bracket_id: int, posted: bool, success: str) -> str:
        """Truthful reply for /start and /next: the state transition is saved,
        but posting to the channel may have failed or auto-cancelled it."""
        bracket = await db.get_bracket(self.bot.db, bracket_id)
        if bracket is not None and bracket.status == CANCELLED:
            return (
                "The bracket's channel is unusable (deleted or no access), "
                "so the bracket was cancelled."
            )
        if not posted:
            return (
                "The change is saved, but posting to the channel failed — "
                "the bot will keep retrying automatically."
            )
        return success

    @app_commands.command(description="Re-post the bracket image")
    @app_commands.checks.cooldown(1, 30.0, key=lambda i: i.channel_id)
    async def show(self, interaction: discord.Interaction) -> None:
        bracket = await self._active(interaction)
        if bracket is None:
            bracket = await db.get_latest_finished_bracket(self.bot.db, interaction.channel_id)
        if bracket is None or bracket.status == SETUP:
            items = [] if bracket is None else await db.list_items(self.bot.db, bracket.id)
            if bracket is None:
                await self._fail(interaction, "No bracket to show in this channel.")
            else:
                header = f"**{_esc(bracket.name)}** hasn't started yet. Items so far:\n"
                listing = logic.clamp_lines(
                    [f"{i}. {_esc(x.name)}" for i, x in enumerate(items, 1)],
                    MESSAGE_CONTENT_LIMIT - len(header),
                )
                await interaction.response.send_message(header + (listing or "*none*"))
            return
        if bracket.context_type == "private" and await self._needs_post_repair(bracket):
            await interaction.response.defer()
            posted = await self._tick(
                bracket.id, self._interaction_publisher(interaction, use_original=True)
            )
            if not posted:
                await interaction.followup.send(
                    "The bracket is still saved, but Discord stopped recovery. Try again.",
                    ephemeral=True,
                )
            return
        await interaction.response.defer()
        data = await self.render_png(bracket.id)
        await interaction.edit_original_response(
            attachments=[discord.File(io.BytesIO(data), filename=f"bracket-{bracket.id}.png")]
        )

    @app_commands.command(description="Cancel this channel's bracket")
    async def cancel(self, interaction: discord.Interaction) -> None:
        bracket = await self._active(interaction)
        if bracket is None:
            await self._fail(interaction, "No active bracket in this channel.")
            return
        if not _is_manager(bracket, interaction.user):
            await self._fail(interaction, "Only the bracket owner or a moderator can do that.")
            return
        view = ConfirmView()
        await interaction.response.send_message(
            f"Cancel **{_esc(bracket.name)}**? This can't be undone.", view=view, ephemeral=True
        )
        await view.wait()
        if not view.value:
            return
        # Re-check under the lock: the bracket may have ended, or ownership may
        # have moved, while the confirmation sat unanswered.
        lock = self._lock(bracket.id)
        async with lock:
            fresh = await self._recheck_manager(interaction, bracket.id)
            if fresh is None:
                return
            cancelled = await lifecycle.cancel_bracket(self.bot.db, bracket.id)
        if not cancelled:
            await interaction.followup.send("That bracket already ended.", ephemeral=True)
            return
        self._render_cache.pop(bracket.id, None)
        await self._disable_open_matchups(fresh)
        if fresh.context_type == "private":
            confirmed_interaction = view.interaction or interaction
            await confirmed_interaction.followup.send(
                f"❌ **{_esc(bracket.name)}** was cancelled by {interaction.user.mention}."
            )
        else:
            channel = interaction.channel
            try:
                await channel.send(
                    f"❌ **{_esc(bracket.name)}** was cancelled by {interaction.user.mention}."
                )
            except discord.HTTPException:
                pass
        await interaction.followup.send("Bracket cancelled.", ephemeral=True)

    async def _disable_open_matchups(self, bracket: Bracket) -> None:
        """Best effort: strip vote buttons from still-open matchup messages."""
        if bracket.context_type == "private":
            return
        matches = await db.round_matches(self.bot.db, bracket.id, bracket.current_round)
        try:
            channel = await self.publisher._channel(bracket)
        except ChannelUnavailable:
            return
        seen_messages = set()
        for match in matches:
            if match.message_id is None or match.winner is not None:
                continue
            if match.message_id in seen_messages:
                continue
            seen_messages.add(match.message_id)
            try:
                await channel.get_partial_message(match.message_id).edit(view=None)
            except discord.HTTPException:
                continue

    async def cog_app_command_error(
        self, interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await self._fail(interaction, f"Slow down — try again in {error.retry_after:.0f}s.")
            return
        log.exception("Command %s failed", interaction.command, exc_info=error)
        try:
            await self._fail(interaction, "Something went wrong — check the bot logs.")
        except discord.HTTPException:
            pass


@app_commands.command(name="help", description="How the bracket bot works")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
@app_commands.allowed_installs(guilds=True, users=True)
async def help_command(interaction: discord.Interaction) -> None:
    if interaction.guild_id is not None and not interaction.is_guild_integration():
        await interaction.response.send_message(
            "Add the bot to this server before using brackets here. "
            "User installs are for DMs and group chats.",
            ephemeral=True,
        )
        return
    private_note = (
        "\n\n**Private chats** — friend and group DMs use manual rounds with "
        "`/bracket next`; timed rounds require a server or a DM with the bot."
        if getattr(interaction, "context", None) is not None and interaction.context.private_channel
        else ""
    )
    embed = discord.Embed(
        title="Bracket voting bot",
        description=(
            "Run a tournament bracket where the channel votes each matchup.\n\n"
            "**Setup** — `/bracket create`, then `/bracket add` each contender "
            "(`rename`/`remove`/`items` to manage, `editmode`/`editor` to control who edits).\n"
            "**Rounds** — `/bracket start [round_minutes]` posts each matchup with vote "
            "buttons. Rounds close on the timer, or the owner runs `/bracket next` "
            "(with a confirmation). Ties are settled by coin flip.\n"
            "**Anytime** — `/bracket show` re-posts the bracket image, "
            "`/bracket transfer` hands the bracket over, `/bracket cancel` ends it.\n\n"
            "Choices and contender tallies are private while a round is open; the total "
            "number of votes counted is public. Results and the bracket image are public."
            f"{private_note}"
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

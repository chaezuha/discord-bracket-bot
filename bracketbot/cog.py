"""Slash commands, the round scheduler, and the Discord-side publisher."""

from __future__ import annotations

import asyncio
import io
import logging
import random
import time
from collections import defaultdict
from typing import Literal

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands, tasks

from . import db, lifecycle, logic, render
from .lifecycle import ChannelUnavailable, MatchResult
from .models import RUNNING, SETUP, Bracket, Match
from .views import ConfirmView, vote_view

log = logging.getLogger(__name__)

# OS-entropy randomness for coin flips and shuffles — unbiased and unseedable.
_rng = random.SystemRandom()

SCHEDULER_INTERVAL_SECONDS = 10


def _esc(name: str | None) -> str:
    return discord.utils.escape_markdown(name or "?")


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

    async def _image(self, bracket: Bracket) -> discord.File:
        data = await self.cog.render_png(bracket.id)
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

    async def post_matchup(self, bracket: Bracket, match: Match, a_name: str, b_name: str) -> int:
        label = await self._round_label(bracket, match.round)
        message = await self._send(
            bracket,
            content=(
                f"**{label} — Matchup {match.slot}**\n**{_esc(a_name)}**  vs  **{_esc(b_name)}**"
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
        embed = discord.Embed(
            title=f"{_esc(bracket.name)} — {label} results",
            description="\n".join(self._result_line(r) for r in results),
            color=discord.Color.green(),
        )
        embed.set_image(url=f"attachment://bracket-{bracket.id}.png")
        await self._send(bracket, embed=embed, file=await self._image(bracket))

    async def post_champion(self, bracket: Bracket, result: MatchResult) -> None:
        embed = discord.Embed(
            title=f"🏆 {_esc(result.winner_name)} wins {_esc(bracket.name)}!",
            description=self._result_line(result),
            color=discord.Color.gold(),
        )
        embed.set_image(url=f"attachment://bracket-{bracket.id}.png")
        await self._send(bracket, embed=embed, file=await self._image(bracket))


@app_commands.guild_only()
class BracketCog(commands.GroupCog, name="bracket"):
    """Tournament-style voting brackets."""

    editor = app_commands.Group(name="editor", description="Manage who may edit the bracket")

    def __init__(self, bot) -> None:
        self.bot = bot
        self.publisher = DiscordPublisher(self)
        self._locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._render_sem = asyncio.Semaphore(2)
        self._render_cache: dict[int, tuple[tuple, bytes]] = {}

    async def cog_load(self) -> None:
        self.scheduler.start()

    async def cog_unload(self) -> None:
        self.scheduler.cancel()

    # --- scheduler / lifecycle glue ---------------------------------------

    @tasks.loop(seconds=SCHEDULER_INTERVAL_SECONDS)
    async def scheduler(self) -> None:
        """Closes due rounds and resumes any half-published state; the first
        pass after startup doubles as crash recovery."""
        try:
            brackets = await db.list_running_brackets(self.bot.db)
        except Exception:
            log.exception("Scheduler could not list brackets")
            return
        for bracket in brackets:
            await self._tick(bracket.id)

    @scheduler.before_loop
    async def _wait_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def _tick(self, bracket_id: int) -> None:
        async with self._locks[bracket_id]:
            try:
                await lifecycle.tick(
                    self.bot.db, self.publisher, bracket_id, now=int(time.time()), rng=_rng
                )
            except Exception:
                log.exception("Tick failed for bracket %s (will retry)", bracket_id)

    async def render_png(self, bracket_id: int) -> bytes:
        conn = self.bot.db
        bracket = await db.get_bracket(conn, bracket_id)
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
            return cached[1]
        async with self._render_sem:
            data = await asyncio.to_thread(render.render_bracket, bracket, items, matches, votes)
        self._render_cache[bracket_id] = (key, data)
        return data

    # --- command helpers ---------------------------------------------------

    async def _active(self, interaction: discord.Interaction) -> Bracket | None:
        return await db.get_active_bracket(self.bot.db, interaction.channel_id)

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
            await self._fail(
                interaction,
                f"That name is empty or longer than {logic.MAX_NAME_LENGTH} characters.",
            )
            return
        try:
            await db.create_bracket(
                self.bot.db,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                owner_id=interaction.user.id,
                name=normalized,
                edit_mode=edit_mode,
                seeding=seeding,
                created_at=int(time.time()),
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
            await self._fail(
                interaction,
                f"That name is empty or longer than {logic.MAX_NAME_LENGTH} characters.",
            )
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
        found = await db.find_item(self.bot.db, bracket.id, item)
        if found is None:
            await self._fail(interaction, "No such item in this bracket.")
            return
        name = logic.normalize_name(new_name)
        if name is None:
            await self._fail(
                interaction,
                f"That name is empty or longer than {logic.MAX_NAME_LENGTH} characters.",
            )
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
            "\n".join(f"{i}. {_esc(item.name)}" for i, item in enumerate(items, 1)) or "*none yet*"
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
        if not _is_manager(bracket, interaction.user):
            await self._fail(interaction, "Only the bracket owner or a moderator can do that.")
            return
        await db.set_edit_mode(self.bot.db, bracket.id, mode)
        await interaction.response.send_message(f"Edit mode is now **{mode}**.", ephemeral=True)

    @editor.command(name="add", description="Allow a user to edit the bracket when restricted")
    async def editor_add(self, interaction: discord.Interaction, user: discord.Member) -> None:
        bracket = await self._active(interaction)
        if bracket is None:
            await self._fail(interaction, "No active bracket in this channel.")
            return
        if not _is_manager(bracket, interaction.user):
            await self._fail(interaction, "Only the bracket owner or a moderator can do that.")
            return
        await db.add_editor(self.bot.db, bracket.id, user.id)
        await interaction.response.send_message(
            f"{user.mention} can now edit items.", ephemeral=True
        )

    @editor.command(name="remove", description="Take a user's edit access away")
    async def editor_remove(self, interaction: discord.Interaction, user: discord.Member) -> None:
        bracket = await self._active(interaction)
        if bracket is None:
            await self._fail(interaction, "No active bracket in this channel.")
            return
        if not _is_manager(bracket, interaction.user):
            await self._fail(interaction, "Only the bracket owner or a moderator can do that.")
            return
        removed = await db.remove_editor(self.bot.db, bracket.id, user.id)
        await interaction.response.send_message(
            f"{user.mention} {'no longer has' if removed else 'did not have'} edit access.",
            ephemeral=True,
        )

    @app_commands.command(description="Hand bracket ownership to someone else")
    async def transfer(self, interaction: discord.Interaction, user: discord.Member) -> None:
        bracket = await self._active(interaction)
        if bracket is None:
            await self._fail(interaction, "No active bracket in this channel.")
            return
        if not _is_manager(bracket, interaction.user):
            await self._fail(interaction, "Only the bracket owner or a moderator can do that.")
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
        if not _is_manager(bracket, interaction.user):
            await self._fail(interaction, "Only the bracket owner or a moderator can start it.")
            return
        items = await db.list_items(self.bot.db, bracket.id)
        if len(items) < 2:
            await self._fail(interaction, "Add at least 2 items before starting.")
            return
        await interaction.response.defer(ephemeral=True)
        await db.set_round_seconds(
            self.bot.db, bracket.id, round_minutes * 60 if round_minutes else None
        )
        async with self._locks[bracket.id]:
            await lifecycle.start_bracket(self.bot.db, bracket.id, _rng)
        await self._tick(bracket.id)
        await interaction.followup.send("Bracket started — round 1 is live!", ephemeral=True)

    @app_commands.command(name="next", description="Close the current round now and move on")
    async def next_round(self, interaction: discord.Interaction) -> None:
        bracket = await self._active(interaction)
        if bracket is None or bracket.status != RUNNING:
            await self._fail(interaction, "No running bracket in this channel.")
            return
        if not _is_manager(bracket, interaction.user):
            await self._fail(interaction, "Only the bracket owner or a moderator can do that.")
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
        async with self._locks[bracket.id]:
            closed = await lifecycle.close_round(self.bot.db, bracket.id, _rng)
        await self._tick(bracket.id)
        message = "Round closed — results are up." if closed else "That round was already closed."
        await interaction.followup.send(message, ephemeral=True)

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
                listing = "\n".join(f"{i}. {_esc(x.name)}" for i, x in enumerate(items, 1))
                await interaction.response.send_message(
                    f"**{_esc(bracket.name)}** hasn't started yet. Items so far:\n"
                    f"{listing or '*none*'}"
                )
            return
        await interaction.response.defer()
        data = await self.render_png(bracket.id)
        await interaction.followup.send(
            file=discord.File(io.BytesIO(data), filename=f"bracket-{bracket.id}.png")
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
        async with self._locks[bracket.id]:
            cancelled = await lifecycle.cancel_bracket(self.bot.db, bracket.id)
        if not cancelled:
            await interaction.followup.send("That bracket already ended.", ephemeral=True)
            return
        await self._disable_open_matchups(bracket)
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
        matches = await db.round_matches(self.bot.db, bracket.id, bracket.current_round)
        try:
            channel = await self.publisher._channel(bracket)
        except ChannelUnavailable:
            return
        for match in matches:
            if match.message_id is None or match.winner is not None:
                continue
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
async def help_command(interaction: discord.Interaction) -> None:
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
            "Votes are private while a round is open; results and the bracket image are public."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

"""Interactive components: persistent vote buttons and confirm dialogs."""

from __future__ import annotations

import time

import discord

from . import db
from .logic import truncate

BUTTON_LABEL_LIMIT = 80  # Discord's hard limit


class VoteButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"vote:(?P<match_id>[0-9]+):(?P<choice>[ab])",
):
    """One vote option on a matchup message.

    DynamicItem keys the handler off the custom_id pattern, so buttons keep
    working after a bot restart with no per-message re-registration.
    """

    def __init__(self, match_id: int, choice: str, label: str = "Vote") -> None:
        super().__init__(
            discord.ui.Button(
                label=truncate(label, BUTTON_LABEL_LIMIT),
                style=discord.ButtonStyle.primary,
                custom_id=f"vote:{match_id}:{choice}",
            )
        )
        self.match_id = match_id
        self.choice = choice

    @classmethod
    async def from_custom_id(cls, interaction, item, match):  # noqa: ARG003
        return cls(int(match["match_id"]), match["choice"], label=item.label or "Vote")

    async def callback(self, interaction: discord.Interaction) -> None:
        conn = interaction.client.db  # BracketBot
        accepted = await db.cast_vote(
            conn, self.match_id, interaction.user.id, self.choice, int(time.time())
        )
        if not accepted:
            await interaction.response.send_message(
                "Voting for this matchup is closed.", ephemeral=True
            )
            return
        match = await db.get_match(conn, self.match_id)
        names = await db.item_names(conn, match.bracket_id) if match else {}
        item_id = (match.item_a if self.choice == "a" else match.item_b) if match else None
        name = discord.utils.escape_markdown(names.get(item_id, "?"))
        await interaction.response.send_message(
            f"🗳️ You voted for **{name}** — you can change your vote until the round ends.",
            ephemeral=True,
        )


def vote_view(match_id: int, a_name: str, b_name: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(VoteButton(match_id, "a", label=a_name))
    view.add_item(VoteButton(match_id, "b", label=b_name))
    return view


class ConfirmView(discord.ui.View):
    """Ephemeral yes/no double-check; read .value after wait()."""

    def __init__(self) -> None:
        super().__init__(timeout=60)
        self.value: bool | None = None

    async def _finish(self, interaction: discord.Interaction, value: bool) -> None:
        self.value = value
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    @discord.ui.button(label="Yes, do it", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, True)

    @discord.ui.button(label="Never mind", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self._finish(interaction, False)

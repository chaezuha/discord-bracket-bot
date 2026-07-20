"""Row dataclasses shared across the bot."""

from __future__ import annotations

from dataclasses import dataclass

# Bracket status values
SETUP = "setup"
RUNNING = "running"
FINISHED = "finished"
CANCELLED = "cancelled"

# Round states (of the bracket's current round)
ROUND_OPEN = "open"
ROUND_CLOSING = "closing"


@dataclass(frozen=True)
class Bracket:
    id: int
    guild_id: int
    channel_id: int
    owner_id: int
    name: str
    status: str
    edit_mode: str  # open | restricted
    seeding: str  # order | shuffle
    round_seconds: int | None
    current_round: int  # 0 during setup, 1..k while running
    round_state: str  # open | closing
    round_closes_at: int | None  # unix seconds; None = manual advance only
    last_summary_round: int
    created_at: int


@dataclass(frozen=True)
class Item:
    id: int
    bracket_id: int
    name: str
    position: int


@dataclass(frozen=True)
class Match:
    id: int
    bracket_id: int
    round: int
    slot: int
    item_a: int | None
    item_b: int | None
    winner: str | None  # 'a' | 'b'
    decided_by: str | None  # votes | coinflip | bye
    message_id: int | None
    published: bool

    @property
    def is_bye(self) -> bool:
        return self.item_a is None or self.item_b is None

    @property
    def winner_item_id(self) -> int | None:
        if self.winner == "a":
            return self.item_a
        if self.winner == "b":
            return self.item_b
        return None

    @property
    def loser_item_id(self) -> int | None:
        if self.winner == "a":
            return self.item_b
        if self.winner == "b":
            return self.item_a
        return None

"""Pure bracket logic: name validation, seeding, byes, and match deciding.

Nothing in here touches Discord or the database, so all of it is unit-testable.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from random import Random

MAX_NAME_LENGTH = 80  # also the Discord button-label limit

_WHITESPACE = re.compile(r"\s+")


def normalize_name(raw: str) -> str | None:
    """Collapse whitespace and strip; None if empty, too long, or containing any
    non-printable character (control and bidirectional-formatting characters
    could otherwise smuggle spoofed text into names)."""
    name = _WHITESPACE.sub(" ", raw).strip()
    if not name or len(name) > MAX_NAME_LENGTH:
        return None
    if any(not ch.isprintable() for ch in name):
        return None
    return name


def bracket_size(item_count: int) -> int:
    """Smallest power of two >= item_count."""
    if item_count < 2:
        raise ValueError("a bracket needs at least 2 items")
    size = 1
    while size < item_count:
        size *= 2
    return size


def round_count(size: int) -> int:
    return size.bit_length() - 1


def seed_order(size: int) -> list[int]:
    """Standard single-elimination slot order of seeds 1..size.

    Built so seed 1 meets seed 2 only in the final, and round-1 slot i pairs
    order[2i] vs order[2i+1] (seed s vs seed size+1-s). E.g. size 8 ->
    [1, 8, 4, 5, 2, 7, 3, 6].
    """
    order = [1]
    while len(order) < size:
        doubled = len(order) * 2
        order = [s for x in order for s in (x, doubled + 1 - x)]
    return order


def first_round_pairs(item_ids: Sequence[int]) -> list[tuple[int | None, int | None]]:
    """Pair item ids (given in seed order: index 0 = seed 1) for round 1.

    Seeds beyond len(item_ids) are byes (None). Because byes = size - n and
    n > size/2, standard seeding always pairs every bye against a real item —
    bye-vs-bye is impossible by construction.
    """
    size = bracket_size(len(item_ids))
    order = seed_order(size)

    def item_for(seed: int) -> int | None:
        return item_ids[seed - 1] if seed <= len(item_ids) else None

    return [(item_for(order[i]), item_for(order[i + 1])) for i in range(0, size, 2)]


def decide(votes_a: int, votes_b: int, rng: Random) -> tuple[str, str]:
    """Return (winner 'a'|'b', decided_by 'votes'|'coinflip'). Ties (incl. 0-0) coin-flip."""
    if votes_a > votes_b:
        return "a", "votes"
    if votes_b > votes_a:
        return "b", "votes"
    return rng.choice(["a", "b"]), "coinflip"


def round_label(round_no: int, total_rounds: int) -> str:
    """Human name for a round: 'Round 1', ..., 'Semifinals', 'Final'."""
    remaining = total_rounds - round_no
    if remaining == 0:
        return "Final"
    if remaining == 1:
        return "Semifinals"
    if remaining == 2:
        return "Quarterfinals"
    return f"Round {round_no}"


def truncate(name: str, limit: int) -> str:
    """Trim to limit characters with an ellipsis (for button labels etc.)."""
    if len(name) <= limit:
        return name
    return name[: limit - 1].rstrip() + "…"


def chunk_lines(lines: Sequence[str], limit: int) -> list[str]:
    """Greedily join lines with newlines into strings of at most limit
    characters (for Discord's message/embed size caps). A single line longer
    than limit is truncated rather than split."""
    chunks: list[str] = []
    current = ""
    for line in lines:
        line = truncate(line, limit)
        if not current:
            current = line
        elif len(current) + 1 + len(line) <= limit:
            current += "\n" + line
        else:
            chunks.append(current)
            current = line
    if current:
        chunks.append(current)
    return chunks


def clamp_lines(lines: Sequence[str], limit: int) -> str:
    """Join lines with newlines; if the result exceeds limit, keep whole
    leading lines and end with an '…and N more' marker instead."""
    text = "\n".join(lines)
    if len(text) <= limit:
        return text
    for keep in range(len(lines) - 1, -1, -1):
        clamped = "\n".join([*lines[:keep], f"…and {len(lines) - keep} more"])
        if len(clamped) <= limit:
            return clamped
    return truncate(f"…and {len(lines)} more", limit)

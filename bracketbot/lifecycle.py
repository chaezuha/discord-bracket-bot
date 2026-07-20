"""Round lifecycle: start, atomic close, resume-safe publish.

The flow per round is a small persisted state machine:

    open --(deadline or /bracket next)--> closing --(publish steps)--> open (next round)
                                                   \\--> finished (after the final)

`close_round` is the only open->closing transition and runs in one BEGIN
IMMEDIATE transaction that also persists winners (coin flips included) and the
next round's matches — all before any Discord call, so results are never
rerolled and the bracket never advances twice. `publish_round` and
`ensure_round_posted` are safe to re-run: every Discord side effect is gated
on a persisted marker (matches.published, brackets.last_summary_round,
matches.message_id), which is what makes crash/restart recovery just "run the
tick again".

Discord I/O goes through the Publisher protocol so tests can drive the whole
machine without a gateway connection.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from random import Random
from typing import Protocol

import aiosqlite

from . import db, logic
from .models import RUNNING, SETUP, Bracket, Match

log = logging.getLogger(__name__)


class ChannelUnavailable(Exception):
    """The bracket's channel is gone (deleted or no access); auto-cancel it."""


@dataclass(frozen=True)
class MatchResult:
    match: Match
    a_name: str | None
    b_name: str | None
    votes_a: int
    votes_b: int

    @property
    def winner_name(self) -> str | None:
        return self.a_name if self.match.winner == "a" else self.b_name


class Publisher(Protocol):
    """Discord side effects; implementations must swallow per-message errors
    (deleted message, missing permissions) and raise ChannelUnavailable only
    when the whole channel is unusable."""

    async def post_round_open(self, bracket: Bracket, round_no: int, closes_at: int | None) -> None:
        """Round header + current bracket image."""

    async def post_matchup(self, bracket: Bracket, match: Match, a_name: str, b_name: str) -> int:
        """Post one votable matchup message; return its message id."""

    async def reveal_result(self, bracket: Bracket, result: MatchResult) -> None:
        """Edit a matchup message: disable buttons, show the tally and winner."""

    async def post_round_summary(
        self, bracket: Bracket, round_no: int, results: list[MatchResult]
    ) -> None:
        """Public results post with the updated bracket image."""

    async def post_champion(self, bracket: Bracket, result: MatchResult) -> None:
        """Final result + champion announcement."""


async def start_bracket(conn: aiosqlite.Connection, bracket_id: int, rng: Random) -> None:
    """setup -> running: seed items, create round 1 (byes pre-resolved).

    Posting round 1 happens in the caller's next tick via ensure_round_posted,
    the same path that repairs a crash halfway through posting.
    """
    async with db.transaction(conn):
        bracket = await db.get_bracket(conn, bracket_id)
        if bracket is None or bracket.status != SETUP:
            raise ValueError("bracket is not in setup")
        items = await db.list_items(conn, bracket_id)
        if len(items) < 2:
            raise ValueError("bracket needs at least 2 items")

        item_ids = [item.id for item in items]
        if bracket.seeding == "shuffle":
            rng.shuffle(item_ids)
            await db.shuffle_positions(conn, bracket_id, item_ids)

        for slot, (a, b) in enumerate(logic.first_round_pairs(item_ids), start=1):
            winner = None
            decided_by = None
            published = 0
            if b is None:
                winner, decided_by, published = "a", "bye", 1
            elif a is None:  # cannot happen with standard seeding; keep the db sane anyway
                winner, decided_by, published = "b", "bye", 1
            await db.execute(
                conn,
                "INSERT INTO matches (bracket_id, round, slot, item_a, item_b, winner,"
                " decided_by, published) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (bracket_id, 1, slot, a, b, winner, decided_by, published),
            )
        await db.execute(
            conn,
            "UPDATE brackets SET status = ?, current_round = 1, round_state = 'open',"
            " round_closes_at = NULL WHERE id = ?",
            (RUNNING, bracket_id),
        )


async def close_round(
    conn: aiosqlite.Connection,
    bracket_id: int,
    rng: Random,
    expected_round: int | None = None,
) -> bool:
    """Atomically close the current round; False if it was not open (already
    closing/closed — the scheduler-vs-/next and double-close races land here).

    Callers that decided to close based on an earlier read (a confirmation
    dialog, the scheduler's deadline check) pass that read's round number as
    expected_round so a round that opened in the meantime is never the one
    that gets closed."""
    async with db.transaction(conn):
        sql = (
            "UPDATE brackets SET round_state = 'closing'"
            " WHERE id = ? AND status = ? AND round_state = 'open'"
        )
        params: list = [bracket_id, RUNNING]
        if expected_round is not None:
            sql += " AND current_round = ?"
            params.append(expected_round)
        cur = await db.execute(conn, sql, params)
        if cur.rowcount == 0:
            return False

        bracket = await db.get_bracket(conn, bracket_id)
        matches = await db.round_matches(conn, bracket_id, bracket.current_round)
        for match in matches:
            if match.winner is None:
                votes_a, votes_b = await db.tally(conn, match.id)
                winner, decided_by = logic.decide(votes_a, votes_b, rng)
                await db.execute(
                    conn,
                    "UPDATE matches SET winner = ?, decided_by = ? WHERE id = ?",
                    (winner, decided_by, match.id),
                )
        if len(matches) > 1:
            # Persist next-round pairings now, before any Discord call.
            matches = await db.round_matches(conn, bracket_id, bracket.current_round)
            for slot in range(1, len(matches) // 2 + 1):
                a = matches[2 * slot - 2].winner_item_id
                b = matches[2 * slot - 1].winner_item_id
                await db.execute(
                    conn,
                    "INSERT OR IGNORE INTO matches (bracket_id, round, slot, item_a, item_b)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (bracket_id, bracket.current_round + 1, slot, a, b),
                )
    return True


async def _load_results(
    conn: aiosqlite.Connection, bracket_id: int, matches: list[Match]
) -> list[MatchResult]:
    names = await db.item_names(conn, bracket_id)
    results = []
    for match in matches:
        votes_a, votes_b = await db.tally(conn, match.id)
        results.append(
            MatchResult(
                match=match,
                a_name=names.get(match.item_a),
                b_name=names.get(match.item_b),
                votes_a=votes_a,
                votes_b=votes_b,
            )
        )
    return results


async def publish_round(
    conn: aiosqlite.Connection, publisher: Publisher, bracket_id: int, now: int
) -> None:
    """Resume-safe publish of a round in 'closing': reveal results, post the
    summary (or champion), then advance to the next round or finish."""
    bracket = await db.get_bracket(conn, bracket_id)
    round_no = bracket.current_round
    matches = await db.round_matches(conn, bracket_id, round_no)
    results = await _load_results(conn, bracket_id, matches)
    is_final = len(matches) == 1

    for result in results:
        if not result.match.published:
            if result.match.message_id is not None:
                await publisher.reveal_result(bracket, result)
            await db.mark_published(conn, result.match.id)

    if bracket.last_summary_round < round_no:
        # Posted before the marker is set: a crash in between means a rare
        # duplicate announcement on restart, never a missing one.
        if is_final:
            await publisher.post_champion(bracket, results[0])
            await db.execute(
                conn,
                "UPDATE brackets SET last_summary_round = ?, status = 'finished' WHERE id = ?",
                (round_no, bracket_id),
            )
        else:
            await publisher.post_round_summary(bracket, round_no, results)
            await db.execute(
                conn,
                "UPDATE brackets SET last_summary_round = ? WHERE id = ?",
                (round_no, bracket_id),
            )
    if is_final:
        return

    await db.execute(
        conn,
        "UPDATE brackets SET current_round = ?, round_state = 'open', round_closes_at = NULL"
        " WHERE id = ? AND current_round = ? AND round_state = 'closing'",
        (round_no + 1, bracket_id, round_no),
    )


async def ensure_round_posted(
    conn: aiosqlite.Connection, publisher: Publisher, bracket_id: int, now: int
) -> None:
    """Post whatever of the current open round is missing (all of it after a
    normal advance; the tail end after a crash mid-posting). Whenever anything
    was missing, the deadline is reset to a fresh full duration from now — a
    round never starts with less than its configured time."""
    bracket = await db.get_bracket(conn, bracket_id)
    if bracket.status != RUNNING or bracket.round_state != "open":
        return
    matches = await db.round_matches(conn, bracket_id, bracket.current_round)
    missing = [m for m in matches if not m.is_bye and m.message_id is None]
    if not missing:
        return

    closes_at = now + bracket.round_seconds if bracket.round_seconds else None
    if all(m.message_id is None for m in matches if not m.is_bye):
        # Fresh round (not a partial repair): post the header. A crash right
        # after this line can duplicate the header on restart — harmless.
        await publisher.post_round_open(bracket, bracket.current_round, closes_at)
    names = await db.item_names(conn, bracket_id)
    for match in missing:
        message_id = await publisher.post_matchup(
            bracket, match, names.get(match.item_a, "?"), names.get(match.item_b, "?")
        )
        await db.set_message_id(conn, match.id, message_id)
    await db.execute(
        conn,
        "UPDATE brackets SET round_closes_at = ? WHERE id = ? AND round_state = 'open'",
        (closes_at, bracket_id),
    )


async def cancel_bracket(conn: aiosqlite.Connection, bracket_id: int) -> bool:
    cur = await db.execute(
        conn,
        "UPDATE brackets SET status = 'cancelled' WHERE id = ? AND status IN (?, ?)",
        (bracket_id, SETUP, RUNNING),
    )
    return cur.rowcount > 0


async def tick(
    conn: aiosqlite.Connection,
    publisher: Publisher,
    bracket_id: int,
    *,
    now: int,
    rng: Random,
) -> None:
    """Drive one bracket forward: close a due round, resume publishing,
    repair missing posts. Idempotent; the scheduler, /bracket next, startup
    reconciliation, and /bracket start all funnel through here (callers hold
    the per-bracket lock)."""
    bracket = await db.get_bracket(conn, bracket_id)
    if bracket is None or bracket.status != RUNNING:
        return
    try:
        if (
            bracket.round_state == "open"
            and bracket.round_closes_at is not None
            and now >= bracket.round_closes_at
        ):
            await close_round(conn, bracket_id, rng, expected_round=bracket.current_round)
            bracket = await db.get_bracket(conn, bracket_id)
        if bracket.round_state == "closing":
            await publish_round(conn, publisher, bracket_id, now)
        await ensure_round_posted(conn, publisher, bracket_id, now)
    except ChannelUnavailable:
        log.warning("Channel %s for bracket %s is gone; cancelling", bracket.channel_id, bracket_id)
        await cancel_bracket(conn, bracket_id)

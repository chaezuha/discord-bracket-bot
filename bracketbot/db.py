"""SQLite persistence: schema, migrations, and query helpers.

Invariants live in the schema (CHECKs, foreign keys, unique indexes) rather
than only in application code, so races and bugs surface as IntegrityError
instead of corrupt tournaments. The bot is a single process with one shared
connection; aiosqlite serializes statements on it, and multi-row transitions
use explicit BEGIN IMMEDIATE transactions.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from contextlib import asynccontextmanager

import aiosqlite

from .models import RUNNING, SETUP, Bracket, Item, Match

# Forward-only migration scripts; PRAGMA user_version tracks the last applied.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE brackets (
        id INTEGER PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        owner_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'setup'
            CHECK (status IN ('setup', 'running', 'finished', 'cancelled')),
        edit_mode TEXT NOT NULL DEFAULT 'open' CHECK (edit_mode IN ('open', 'restricted')),
        seeding TEXT NOT NULL DEFAULT 'order' CHECK (seeding IN ('order', 'shuffle')),
        round_seconds INTEGER CHECK (round_seconds IS NULL OR round_seconds > 0),
        current_round INTEGER NOT NULL DEFAULT 0,
        round_state TEXT NOT NULL DEFAULT 'open' CHECK (round_state IN ('open', 'closing')),
        round_closes_at INTEGER,
        last_summary_round INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL
    );

    -- One active bracket per channel, enforced by the database itself.
    CREATE UNIQUE INDEX ux_brackets_active_channel
        ON brackets (channel_id) WHERE status IN ('setup', 'running');

    CREATE TABLE editors (
        bracket_id INTEGER NOT NULL REFERENCES brackets(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        PRIMARY KEY (bracket_id, user_id)
    );

    CREATE TABLE items (
        id INTEGER PRIMARY KEY,
        bracket_id INTEGER NOT NULL REFERENCES brackets(id) ON DELETE CASCADE,
        name TEXT NOT NULL,
        position INTEGER NOT NULL,
        UNIQUE (bracket_id, position),
        UNIQUE (bracket_id, name COLLATE NOCASE)
    );

    CREATE TABLE matches (
        id INTEGER PRIMARY KEY,
        bracket_id INTEGER NOT NULL REFERENCES brackets(id) ON DELETE CASCADE,
        round INTEGER NOT NULL,
        slot INTEGER NOT NULL,
        item_a INTEGER REFERENCES items(id),
        item_b INTEGER REFERENCES items(id),
        winner TEXT CHECK (winner IN ('a', 'b')),
        decided_by TEXT CHECK (decided_by IN ('votes', 'coinflip', 'bye')),
        message_id INTEGER,
        published INTEGER NOT NULL DEFAULT 0 CHECK (published IN (0, 1)),
        UNIQUE (bracket_id, round, slot)
    );

    CREATE TABLE votes (
        match_id INTEGER NOT NULL REFERENCES matches(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL,
        choice TEXT NOT NULL CHECK (choice IN ('a', 'b')),
        PRIMARY KEY (match_id, user_id)
    );
    """,
]


async def connect(path: str) -> aiosqlite.Connection:
    """Open (creating directories as needed), set pragmas, and migrate."""
    if path != ":memory:":
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    # isolation_level=None -> autocommit for single statements; transitions
    # issue BEGIN IMMEDIATE explicitly.
    conn = await aiosqlite.connect(path, isolation_level=None)
    conn.row_factory = aiosqlite.Row
    await conn.execute("PRAGMA foreign_keys = ON")
    await conn.execute("PRAGMA busy_timeout = 5000")
    await conn.execute("PRAGMA journal_mode = WAL")
    async with conn.execute("PRAGMA user_version") as cur:
        version = (await cur.fetchone())[0]
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        await conn.executescript(script)
        await conn.execute(f"PRAGMA user_version = {number}")
    # Serializes explicit transactions: aiosqlite runs statements on one shared
    # connection, so two interleaved BEGINs from different tasks would clash.
    conn._transaction_lock = asyncio.Lock()  # type: ignore[attr-defined]
    return conn


@asynccontextmanager
async def transaction(conn: aiosqlite.Connection):
    """BEGIN IMMEDIATE .. COMMIT (ROLLBACK on error), one at a time per connection."""
    async with conn._transaction_lock:  # type: ignore[attr-defined]
        await conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            await conn.execute("ROLLBACK")
            raise
        else:
            await conn.execute("COMMIT")


def _bracket(row: aiosqlite.Row) -> Bracket:
    return Bracket(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        owner_id=row["owner_id"],
        name=row["name"],
        status=row["status"],
        edit_mode=row["edit_mode"],
        seeding=row["seeding"],
        round_seconds=row["round_seconds"],
        current_round=row["current_round"],
        round_state=row["round_state"],
        round_closes_at=row["round_closes_at"],
        last_summary_round=row["last_summary_round"],
        created_at=row["created_at"],
    )


def _item(row: aiosqlite.Row) -> Item:
    return Item(
        id=row["id"], bracket_id=row["bracket_id"], name=row["name"], position=row["position"]
    )


def _match(row: aiosqlite.Row) -> Match:
    return Match(
        id=row["id"],
        bracket_id=row["bracket_id"],
        round=row["round"],
        slot=row["slot"],
        item_a=row["item_a"],
        item_b=row["item_b"],
        winner=row["winner"],
        decided_by=row["decided_by"],
        message_id=row["message_id"],
        published=bool(row["published"]),
    )


# --- brackets ---------------------------------------------------------------


async def create_bracket(
    conn: aiosqlite.Connection,
    *,
    guild_id: int,
    channel_id: int,
    owner_id: int,
    name: str,
    edit_mode: str,
    seeding: str,
    created_at: int,
) -> int:
    cur = await conn.execute(
        "INSERT INTO brackets (guild_id, channel_id, owner_id, name, edit_mode, seeding,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guild_id, channel_id, owner_id, name, edit_mode, seeding, created_at),
    )
    return cur.lastrowid


async def get_bracket(conn: aiosqlite.Connection, bracket_id: int) -> Bracket | None:
    async with conn.execute("SELECT * FROM brackets WHERE id = ?", (bracket_id,)) as cur:
        row = await cur.fetchone()
    return _bracket(row) if row else None


async def get_active_bracket(conn: aiosqlite.Connection, channel_id: int) -> Bracket | None:
    async with conn.execute(
        "SELECT * FROM brackets WHERE channel_id = ? AND status IN (?, ?)",
        (channel_id, SETUP, RUNNING),
    ) as cur:
        row = await cur.fetchone()
    return _bracket(row) if row else None


async def get_latest_finished_bracket(
    conn: aiosqlite.Connection, channel_id: int
) -> Bracket | None:
    async with conn.execute(
        "SELECT * FROM brackets WHERE channel_id = ? AND status = 'finished'"
        " ORDER BY id DESC LIMIT 1",
        (channel_id,),
    ) as cur:
        row = await cur.fetchone()
    return _bracket(row) if row else None


async def list_running_brackets(conn: aiosqlite.Connection) -> list[Bracket]:
    async with conn.execute("SELECT * FROM brackets WHERE status = ?", (RUNNING,)) as cur:
        rows = await cur.fetchall()
    return [_bracket(r) for r in rows]


async def set_edit_mode(conn: aiosqlite.Connection, bracket_id: int, edit_mode: str) -> None:
    await conn.execute("UPDATE brackets SET edit_mode = ? WHERE id = ?", (edit_mode, bracket_id))


async def set_owner(conn: aiosqlite.Connection, bracket_id: int, owner_id: int) -> None:
    await conn.execute("UPDATE brackets SET owner_id = ? WHERE id = ?", (owner_id, bracket_id))


async def set_round_seconds(
    conn: aiosqlite.Connection, bracket_id: int, round_seconds: int | None
) -> None:
    await conn.execute(
        "UPDATE brackets SET round_seconds = ? WHERE id = ?", (round_seconds, bracket_id)
    )


# --- editors ----------------------------------------------------------------


async def add_editor(conn: aiosqlite.Connection, bracket_id: int, user_id: int) -> None:
    await conn.execute(
        "INSERT OR IGNORE INTO editors (bracket_id, user_id) VALUES (?, ?)", (bracket_id, user_id)
    )


async def remove_editor(conn: aiosqlite.Connection, bracket_id: int, user_id: int) -> bool:
    cur = await conn.execute(
        "DELETE FROM editors WHERE bracket_id = ? AND user_id = ?", (bracket_id, user_id)
    )
    return cur.rowcount > 0


async def list_editors(conn: aiosqlite.Connection, bracket_id: int) -> list[int]:
    async with conn.execute(
        "SELECT user_id FROM editors WHERE bracket_id = ?", (bracket_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [r["user_id"] for r in rows]


# --- items ------------------------------------------------------------------


async def add_item(conn: aiosqlite.Connection, bracket_id: int, name: str) -> int:
    cur = await conn.execute(
        "INSERT INTO items (bracket_id, name, position) VALUES (?, ?,"
        " COALESCE((SELECT MAX(position) FROM items WHERE bracket_id = ?), 0) + 1)",
        (bracket_id, name, bracket_id),
    )
    return cur.lastrowid


async def rename_item(conn: aiosqlite.Connection, item_id: int, name: str) -> None:
    await conn.execute("UPDATE items SET name = ? WHERE id = ?", (name, item_id))


async def remove_item(conn: aiosqlite.Connection, item_id: int) -> None:
    await conn.execute("DELETE FROM items WHERE id = ?", (item_id,))


async def list_items(conn: aiosqlite.Connection, bracket_id: int) -> list[Item]:
    async with conn.execute(
        "SELECT * FROM items WHERE bracket_id = ? ORDER BY position", (bracket_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [_item(r) for r in rows]


async def item_names(conn: aiosqlite.Connection, bracket_id: int) -> dict[int, str]:
    return {item.id: item.name for item in await list_items(conn, bracket_id)}


async def find_item(conn: aiosqlite.Connection, bracket_id: int, ref: str) -> Item | None:
    """Resolve an autocomplete value: item id as digits, else case-insensitive name."""
    if ref.isdigit():
        async with conn.execute(
            "SELECT * FROM items WHERE bracket_id = ? AND id = ?", (bracket_id, int(ref))
        ) as cur:
            row = await cur.fetchone()
        if row:
            return _item(row)
    async with conn.execute(
        "SELECT * FROM items WHERE bracket_id = ? AND name = ? COLLATE NOCASE",
        (bracket_id, ref.strip()),
    ) as cur:
        row = await cur.fetchone()
    return _item(row) if row else None


async def shuffle_positions(
    conn: aiosqlite.Connection, bracket_id: int, new_order: Iterable[int]
) -> None:
    """Persist a shuffled seed order (item ids in their new position order)."""
    # Two passes so the UNIQUE(bracket_id, position) index never sees a clash.
    for position, item_id in enumerate(new_order, start=1):
        await conn.execute("UPDATE items SET position = ? WHERE id = ?", (-position, item_id))
    await conn.execute(
        "UPDATE items SET position = -position WHERE bracket_id = ? AND position < 0",
        (bracket_id,),
    )


# --- matches ----------------------------------------------------------------


async def get_match(conn: aiosqlite.Connection, match_id: int) -> Match | None:
    async with conn.execute("SELECT * FROM matches WHERE id = ?", (match_id,)) as cur:
        row = await cur.fetchone()
    return _match(row) if row else None


async def list_matches(conn: aiosqlite.Connection, bracket_id: int) -> list[Match]:
    async with conn.execute(
        "SELECT * FROM matches WHERE bracket_id = ? ORDER BY round, slot", (bracket_id,)
    ) as cur:
        rows = await cur.fetchall()
    return [_match(r) for r in rows]


async def round_matches(conn: aiosqlite.Connection, bracket_id: int, round_no: int) -> list[Match]:
    async with conn.execute(
        "SELECT * FROM matches WHERE bracket_id = ? AND round = ? ORDER BY slot",
        (bracket_id, round_no),
    ) as cur:
        rows = await cur.fetchall()
    return [_match(r) for r in rows]


async def set_message_id(conn: aiosqlite.Connection, match_id: int, message_id: int) -> None:
    await conn.execute("UPDATE matches SET message_id = ? WHERE id = ?", (message_id, match_id))


async def mark_published(conn: aiosqlite.Connection, match_id: int) -> None:
    await conn.execute("UPDATE matches SET published = 1 WHERE id = ?", (match_id,))


# --- votes ------------------------------------------------------------------


async def cast_vote(
    conn: aiosqlite.Connection, match_id: int, user_id: int, choice: str, now: int
) -> bool:
    """Record (or change) a vote; False if the matchup is no longer open.

    The openness check lives inside the INSERT itself so a vote can never land
    after the round has flipped to 'closing' — the close transaction tallies
    after that flip, so every accepted vote is counted.
    """
    cur = await conn.execute(
        """
        INSERT INTO votes (match_id, user_id, choice)
        SELECT :match_id, :user_id, :choice
        WHERE EXISTS (
            SELECT 1 FROM matches m JOIN brackets b ON b.id = m.bracket_id
            WHERE m.id = :match_id AND m.winner IS NULL
              AND b.status = 'running' AND b.round_state = 'open'
              AND b.current_round = m.round
              AND (b.round_closes_at IS NULL OR :now < b.round_closes_at)
        )
        ON CONFLICT (match_id, user_id) DO UPDATE SET choice = excluded.choice
        """,
        {"match_id": match_id, "user_id": user_id, "choice": choice, "now": now},
    )
    return cur.rowcount > 0


async def tally(conn: aiosqlite.Connection, match_id: int) -> tuple[int, int]:
    async with conn.execute(
        "SELECT choice, COUNT(*) AS n FROM votes WHERE match_id = ? GROUP BY choice",
        (match_id,),
    ) as cur:
        rows = await cur.fetchall()
    counts = {r["choice"]: r["n"] for r in rows}
    return counts.get("a", 0), counts.get("b", 0)

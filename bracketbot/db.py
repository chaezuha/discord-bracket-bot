"""SQLite persistence: schema, migrations, and query helpers.

Invariants live in the schema (CHECKs, foreign keys, unique indexes) rather
than only in application code, so races and bugs surface as IntegrityError
instead of corrupt tournaments. The bot is a single process with one shared
connection; every statement goes through the per-connection gate (`execute`,
`fetchone`, `fetchall`), and multi-row transitions use explicit BEGIN
IMMEDIATE transactions that hold the gate for their whole span — so no other
coroutine's statement can interleave into (and be rolled back with, or read
uncommitted state of) an open transaction.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from contextlib import asynccontextmanager
from contextvars import ContextVar

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
    # Serializes all statements against explicit transactions: aiosqlite runs
    # everything on one shared connection, so a statement issued between another
    # task's BEGIN and COMMIT would join (and roll back with) that transaction.
    conn._db_lock = asyncio.Lock()  # type: ignore[attr-defined]
    return conn


# The (connection, task) that currently holds an open transaction. Both parts
# matter: the connection so multiple connections don't confuse each other, and
# the task because child tasks inherit a copy of the parent's context — a task
# spawned mid-transaction must not skip the gate.
_txn_owner: ContextVar[tuple[aiosqlite.Connection, asyncio.Task] | None] = ContextVar(
    "db_txn_owner", default=None
)


@asynccontextmanager
async def _gate(conn: aiosqlite.Connection):
    """Serialize against transactions; reentrant for the transaction owner."""
    owner = _txn_owner.get()
    if owner is not None and owner[0] is conn and owner[1] is asyncio.current_task():
        yield
        return
    async with conn._db_lock:  # type: ignore[attr-defined]
        yield


async def execute(conn: aiosqlite.Connection, sql: str, params=()) -> aiosqlite.Cursor:
    """Run one WRITE statement under the gate.

    The returned cursor is for rowcount/lastrowid only — never fetch rows from
    it (the gate is released on return, so a fetch would race transactions).
    Reads go through fetchone/fetchall, which hold the gate through the fetch.
    """
    async with _gate(conn):
        return await conn.execute(sql, params)


async def fetchone(conn: aiosqlite.Connection, sql: str, params=()) -> aiosqlite.Row | None:
    async with _gate(conn):
        async with conn.execute(sql, params) as cur:
            return await cur.fetchone()


async def fetchall(conn: aiosqlite.Connection, sql: str, params=()) -> list[aiosqlite.Row]:
    async with _gate(conn):
        async with conn.execute(sql, params) as cur:
            return list(await cur.fetchall())


@asynccontextmanager
async def transaction(conn: aiosqlite.Connection):
    """BEGIN IMMEDIATE .. COMMIT (ROLLBACK on error), one at a time per connection."""
    async with conn._db_lock:  # type: ignore[attr-defined]
        token = _txn_owner.set((conn, asyncio.current_task()))
        try:
            await conn.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                await conn.execute("ROLLBACK")
                raise
            else:
                await conn.execute("COMMIT")
        finally:
            _txn_owner.reset(token)


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
    cur = await execute(
        conn,
        "INSERT INTO brackets (guild_id, channel_id, owner_id, name, edit_mode, seeding,"
        " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (guild_id, channel_id, owner_id, name, edit_mode, seeding, created_at),
    )
    return cur.lastrowid


async def get_bracket(conn: aiosqlite.Connection, bracket_id: int) -> Bracket | None:
    row = await fetchone(conn, "SELECT * FROM brackets WHERE id = ?", (bracket_id,))
    return _bracket(row) if row else None


async def get_active_bracket(conn: aiosqlite.Connection, channel_id: int) -> Bracket | None:
    row = await fetchone(
        conn,
        "SELECT * FROM brackets WHERE channel_id = ? AND status IN (?, ?)",
        (channel_id, SETUP, RUNNING),
    )
    return _bracket(row) if row else None


async def get_latest_finished_bracket(
    conn: aiosqlite.Connection, channel_id: int
) -> Bracket | None:
    row = await fetchone(
        conn,
        "SELECT * FROM brackets WHERE channel_id = ? AND status = 'finished'"
        " ORDER BY id DESC LIMIT 1",
        (channel_id,),
    )
    return _bracket(row) if row else None


async def list_running_brackets(conn: aiosqlite.Connection) -> list[Bracket]:
    rows = await fetchall(conn, "SELECT * FROM brackets WHERE status = ?", (RUNNING,))
    return [_bracket(r) for r in rows]


async def set_edit_mode(conn: aiosqlite.Connection, bracket_id: int, edit_mode: str) -> None:
    await execute(conn, "UPDATE brackets SET edit_mode = ? WHERE id = ?", (edit_mode, bracket_id))


async def set_owner(conn: aiosqlite.Connection, bracket_id: int, owner_id: int) -> None:
    await execute(conn, "UPDATE brackets SET owner_id = ? WHERE id = ?", (owner_id, bracket_id))


async def set_round_seconds(
    conn: aiosqlite.Connection, bracket_id: int, round_seconds: int | None
) -> None:
    await execute(
        conn, "UPDATE brackets SET round_seconds = ? WHERE id = ?", (round_seconds, bracket_id)
    )


# --- editors ----------------------------------------------------------------


async def add_editor(conn: aiosqlite.Connection, bracket_id: int, user_id: int) -> None:
    await execute(
        conn,
        "INSERT OR IGNORE INTO editors (bracket_id, user_id) VALUES (?, ?)",
        (bracket_id, user_id),
    )


async def remove_editor(conn: aiosqlite.Connection, bracket_id: int, user_id: int) -> bool:
    cur = await execute(
        conn, "DELETE FROM editors WHERE bracket_id = ? AND user_id = ?", (bracket_id, user_id)
    )
    return cur.rowcount > 0


async def list_editors(conn: aiosqlite.Connection, bracket_id: int) -> list[int]:
    rows = await fetchall(conn, "SELECT user_id FROM editors WHERE bracket_id = ?", (bracket_id,))
    return [r["user_id"] for r in rows]


# --- items ------------------------------------------------------------------


async def add_item(conn: aiosqlite.Connection, bracket_id: int, name: str) -> int:
    cur = await execute(
        conn,
        "INSERT INTO items (bracket_id, name, position) VALUES (?, ?,"
        " COALESCE((SELECT MAX(position) FROM items WHERE bracket_id = ?), 0) + 1)",
        (bracket_id, name, bracket_id),
    )
    return cur.lastrowid


async def rename_item(conn: aiosqlite.Connection, item_id: int, name: str) -> None:
    await execute(conn, "UPDATE items SET name = ? WHERE id = ?", (name, item_id))


async def remove_item(conn: aiosqlite.Connection, item_id: int) -> None:
    await execute(conn, "DELETE FROM items WHERE id = ?", (item_id,))


async def list_items(conn: aiosqlite.Connection, bracket_id: int) -> list[Item]:
    rows = await fetchall(
        conn, "SELECT * FROM items WHERE bracket_id = ? ORDER BY position", (bracket_id,)
    )
    return [_item(r) for r in rows]


async def item_names(conn: aiosqlite.Connection, bracket_id: int) -> dict[int, str]:
    return {item.id: item.name for item in await list_items(conn, bracket_id)}


async def find_item(conn: aiosqlite.Connection, bracket_id: int, ref: str) -> Item | None:
    """Resolve an autocomplete value: item id as digits, else case-insensitive name."""
    if ref.isdigit():
        row = await fetchone(
            conn, "SELECT * FROM items WHERE bracket_id = ? AND id = ?", (bracket_id, int(ref))
        )
        if row:
            return _item(row)
    row = await fetchone(
        conn,
        "SELECT * FROM items WHERE bracket_id = ? AND name = ? COLLATE NOCASE",
        (bracket_id, ref.strip()),
    )
    return _item(row) if row else None


async def shuffle_positions(
    conn: aiosqlite.Connection, bracket_id: int, new_order: Iterable[int]
) -> None:
    """Persist a shuffled seed order (item ids in their new position order)."""
    # Two passes so the UNIQUE(bracket_id, position) index never sees a clash.
    for position, item_id in enumerate(new_order, start=1):
        await execute(conn, "UPDATE items SET position = ? WHERE id = ?", (-position, item_id))
    await execute(
        conn,
        "UPDATE items SET position = -position WHERE bracket_id = ? AND position < 0",
        (bracket_id,),
    )


# --- matches ----------------------------------------------------------------


async def get_match(conn: aiosqlite.Connection, match_id: int) -> Match | None:
    row = await fetchone(conn, "SELECT * FROM matches WHERE id = ?", (match_id,))
    return _match(row) if row else None


async def list_matches(conn: aiosqlite.Connection, bracket_id: int) -> list[Match]:
    rows = await fetchall(
        conn, "SELECT * FROM matches WHERE bracket_id = ? ORDER BY round, slot", (bracket_id,)
    )
    return [_match(r) for r in rows]


async def round_matches(conn: aiosqlite.Connection, bracket_id: int, round_no: int) -> list[Match]:
    rows = await fetchall(
        conn,
        "SELECT * FROM matches WHERE bracket_id = ? AND round = ? ORDER BY slot",
        (bracket_id, round_no),
    )
    return [_match(r) for r in rows]


async def set_message_id(conn: aiosqlite.Connection, match_id: int, message_id: int) -> None:
    await execute(conn, "UPDATE matches SET message_id = ? WHERE id = ?", (message_id, match_id))


async def mark_published(conn: aiosqlite.Connection, match_id: int) -> None:
    await execute(conn, "UPDATE matches SET published = 1 WHERE id = ?", (match_id,))


# --- votes ------------------------------------------------------------------


async def cast_vote(
    conn: aiosqlite.Connection, match_id: int, user_id: int, choice: str, now: int
) -> bool:
    """Record (or change) a vote; False if the matchup is no longer open.

    The openness check lives inside the INSERT itself so a vote can never land
    after the round has flipped to 'closing' — the close transaction tallies
    after that flip, so every accepted vote is counted.
    """
    cur = await execute(
        conn,
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
    rows = await fetchall(
        conn,
        "SELECT choice, COUNT(*) AS n FROM votes WHERE match_id = ? GROUP BY choice",
        (match_id,),
    )
    counts = {r["choice"]: r["n"] for r in rows}
    return counts.get("a", 0), counts.get("b", 0)

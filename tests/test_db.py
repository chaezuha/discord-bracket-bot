import asyncio
import time

import aiosqlite
import pytest

from bracketbot import db


async def _create(conn, channel_id=100, name="Best Snack"):
    return await db.create_bracket(
        conn,
        guild_id=1,
        channel_id=channel_id,
        owner_id=10,
        name=name,
        edit_mode="open",
        seeding="order",
        created_at=int(time.time()),
    )


async def test_migrations_are_idempotent(tmp_path):
    path = str(tmp_path / "m.db")
    first = await db.connect(path)
    await _create(first)
    await first.close()
    second = await db.connect(path)  # reopening must not re-run migrations
    assert await db.get_active_bracket(second, 100) is not None
    await second.close()


async def test_context_migration_defaults_existing_brackets_to_guild(tmp_path):
    path = str(tmp_path / "legacy.db")
    legacy = await aiosqlite.connect(path, isolation_level=None)
    await legacy.executescript(db.MIGRATIONS[0])
    await legacy.execute("PRAGMA user_version = 1")
    await legacy.execute(
        "INSERT INTO brackets (guild_id, channel_id, owner_id, name, created_at) "
        "VALUES (1, 100, 10, 'Legacy', 0)"
    )
    await legacy.close()

    migrated = await db.connect(path)
    bracket = await db.get_active_bracket(migrated, 100)
    assert bracket.context_type == "guild"
    assert (await db.fetchone(migrated, "PRAGMA user_version"))[0] == len(db.MIGRATIONS)
    await migrated.close()


async def test_one_active_bracket_per_channel(conn):
    first = await _create(conn)
    with pytest.raises(aiosqlite.IntegrityError):
        await _create(conn, name="Another")
    # A different channel is fine, and so is the same channel once resolved
    await _create(conn, channel_id=200)
    await conn.execute("UPDATE brackets SET status = 'cancelled' WHERE id = ?", (first,))
    await _create(conn, name="Another")


async def test_item_constraints(conn, bracket_id):
    await db.add_item(conn, bracket_id, "Pizza")
    with pytest.raises(aiosqlite.IntegrityError):
        await db.add_item(conn, bracket_id, "PIZZA")  # case-insensitive duplicate
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(  # duplicate position
            "INSERT INTO items (bracket_id, name, position) VALUES (?, 'Tacos', 1)", (bracket_id,)
        )


async def test_match_slot_unique(conn, bracket_id):
    await conn.execute(
        "INSERT INTO matches (bracket_id, round, slot) VALUES (?, 1, 1)", (bracket_id,)
    )
    with pytest.raises(aiosqlite.IntegrityError):
        await conn.execute(
            "INSERT INTO matches (bracket_id, round, slot) VALUES (?, 1, 1)", (bracket_id,)
        )


async def test_find_item(conn, bracket_id):
    item_id = await db.add_item(conn, bracket_id, "Pizza Party")
    assert (await db.find_item(conn, bracket_id, str(item_id))).id == item_id
    assert (await db.find_item(conn, bracket_id, "pizza party")).id == item_id
    assert await db.find_item(conn, bracket_id, "nope") is None


async def test_shuffle_positions_keeps_unique(conn, bracket_id):
    ids = [await db.add_item(conn, bracket_id, f"Item {i}") for i in range(5)]
    await db.shuffle_positions(conn, bracket_id, list(reversed(ids)))
    items = await db.list_items(conn, bracket_id)
    assert [item.id for item in items] == list(reversed(ids))
    assert [item.position for item in items] == [1, 2, 3, 4, 5]


async def _running_match(conn, bracket_id, closes_at=None):
    """Minimal running bracket with one open match."""
    cur = await conn.execute(
        "INSERT INTO matches (bracket_id, round, slot) VALUES (?, 1, 1)", (bracket_id,)
    )
    await conn.execute(
        "UPDATE brackets SET status = 'running', current_round = 1, round_state = 'open',"
        " round_closes_at = ? WHERE id = ?",
        (closes_at, bracket_id),
    )
    return cur.lastrowid


async def test_cast_vote_upsert_and_tally(conn, bracket_id):
    match_id = await _running_match(conn, bracket_id)
    assert await db.cast_vote(conn, match_id, 1, "a", now=0)
    assert await db.cast_vote(conn, match_id, 2, "a", now=0)
    assert await db.cast_vote(conn, match_id, 1, "b", now=0)  # change of heart, one vote
    assert await db.tally(conn, match_id) == (1, 1)


async def test_cast_vote_rejected_after_deadline(conn, bracket_id):
    match_id = await _running_match(conn, bracket_id, closes_at=1000)
    assert await db.cast_vote(conn, match_id, 1, "a", now=999)
    assert not await db.cast_vote(conn, match_id, 2, "a", now=1000)
    assert not await db.cast_vote(conn, match_id, 1, "b", now=1000)  # change also rejected
    assert await db.tally(conn, match_id) == (1, 0)


async def test_cast_vote_rejected_when_closing(conn, bracket_id):
    match_id = await _running_match(conn, bracket_id)
    await conn.execute("UPDATE brackets SET round_state = 'closing' WHERE id = ?", (bracket_id,))
    assert not await db.cast_vote(conn, match_id, 1, "a", now=0)


async def test_cast_vote_rejected_on_decided_match(conn, bracket_id):
    match_id = await _running_match(conn, bracket_id)
    await conn.execute("UPDATE matches SET winner = 'a' WHERE id = ?", (match_id,))
    assert not await db.cast_vote(conn, match_id, 1, "b", now=0)


async def test_unrelated_write_survives_concurrent_rollback(conn):
    """A write from another coroutine must not interleave into an open
    transaction and get rolled back with it."""
    doomed_bracket = await _create(conn, channel_id=101)
    other_bracket = await _create(conn, channel_id=102)
    in_txn = asyncio.Event()
    release = asyncio.Event()

    async def doomed():
        with pytest.raises(RuntimeError):
            async with db.transaction(conn):
                await db.add_item(conn, doomed_bracket, "Doomed")
                in_txn.set()
                await release.wait()
                raise RuntimeError("boom")

    async def unrelated():
        await in_txn.wait()
        insert = asyncio.create_task(db.add_item(conn, other_bracket, "Survivor"))
        await asyncio.sleep(0.05)
        assert not insert.done()  # gated out of the open transaction
        release.set()
        await insert

    await asyncio.gather(doomed(), unrelated())
    assert [i.name for i in await db.list_items(conn, other_bracket)] == ["Survivor"]
    assert await db.list_items(conn, doomed_bracket) == []  # rolled back


async def test_read_does_not_observe_uncommitted_transaction(conn):
    bracket = await _create(conn)
    in_txn = asyncio.Event()
    release = asyncio.Event()

    async def rolled_back():
        with pytest.raises(RuntimeError):
            async with db.transaction(conn):
                await db.add_item(conn, bracket, "Ghost")
                in_txn.set()
                await release.wait()
                raise RuntimeError("boom")

    async def reader():
        await in_txn.wait()
        read = asyncio.create_task(db.list_items(conn, bracket))
        await asyncio.sleep(0.05)
        assert not read.done()  # blocked until the transaction resolves
        release.set()
        assert await read == []  # never saw the uncommitted row

    await asyncio.gather(rolled_back(), reader())


async def test_transaction_still_sees_its_own_writes(conn):
    bracket = await _create(conn)
    async with db.transaction(conn):
        await db.add_item(conn, bracket, "Mine")
        assert [i.name for i in await db.list_items(conn, bracket)] == ["Mine"]

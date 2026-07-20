import time

import pytest

from bracketbot import db


@pytest.fixture
async def conn(tmp_path):
    connection = await db.connect(str(tmp_path / "test.db"))
    yield connection
    await connection.close()


@pytest.fixture
async def bracket_id(conn):
    """A bracket in setup with no items yet."""
    return await db.create_bracket(
        conn,
        guild_id=1,
        channel_id=100,
        owner_id=10,
        name="Best Snack",
        edit_mode="open",
        seeding="order",
        created_at=int(time.time()),
    )

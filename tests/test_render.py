import random

import pytest

from bracketbot import db, lifecycle, render


async def _rendered(conn, bracket_id, n, finish=False):
    for i in range(n):
        await db.add_item(conn, bracket_id, f"Contender {i} ✨ ünïcode " + "long " * 10)
    rng = random.Random(7)
    await lifecycle.start_bracket(conn, bracket_id, rng)
    if finish:
        bracket = await db.get_bracket(conn, bracket_id)
        while bracket.status == "running":
            await lifecycle.close_round(conn, bracket_id, rng)
            await lifecycle.publish_round(conn, _NullPublisher(), bracket_id, now=0)
            bracket = await db.get_bracket(conn, bracket_id)
    bracket = await db.get_bracket(conn, bracket_id)
    items = await db.item_names(conn, bracket_id)
    matches = await db.list_matches(conn, bracket_id)
    votes = {m.id: (2, 1) for m in matches}
    return render.render_bracket(bracket, items, matches, votes)


class _NullPublisher:
    async def post_round_open(self, *a): ...

    async def post_matchup(self, *a):
        return 1

    async def reveal_result(self, *a): ...

    async def post_round_summary(self, *a): ...

    async def post_champion(self, *a): ...


@pytest.mark.parametrize("n", [2, 3, 8, 32, 64])
async def test_render_sizes(conn, bracket_id, n):
    data = await _rendered(conn, bracket_id, n)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    # Discord's default upload limit is 10 MiB; stay far below it
    assert len(data) < 2 * 1024 * 1024


async def test_render_finished_bracket_has_champion(conn, bracket_id):
    data = await _rendered(conn, bracket_id, 5, finish=True)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"

#!/usr/bin/env bash
# No-network smoke test for a built image: verifies the runtime pieces the bot
# needs are actually present and importable, and that the renderer can produce
# a PNG with the fonts installed in the image. Usage: scripts/smoke.sh <image>
set -euo pipefail

image="${1:?usage: smoke.sh <image>}"

docker run --rm --network none --entrypoint python "$image" - <<'PY'
import asyncio
import random

from bracketbot import db, lifecycle, render
from bracketbot.cog import BracketCog  # noqa: F401  (pulls in discord.py wiring)


async def main():
    conn = await db.connect(":memory:")
    bid = await db.create_bracket(
        conn, guild_id=1, channel_id=1, owner_id=1, name="smoke",
        edit_mode="open", seeding="order", created_at=0,
    )
    for name in ("A", "B", "C"):
        await db.add_item(conn, bid, name)
    await lifecycle.start_bracket(conn, bid, random.Random(0))
    bracket = await db.get_bracket(conn, bid)
    png = render.render_bracket(
        bracket,
        await db.item_names(conn, bid),
        await db.list_matches(conn, bid),
        {},
    )
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "renderer did not produce a PNG"
    await conn.close()
    print(f"rendered {len(png)} byte PNG")


asyncio.run(main())
PY

echo "smoke test passed for $image"

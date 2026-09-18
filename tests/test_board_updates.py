import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from bracketbot import board_updates
from bracketbot.board_updates import BoardUpdates


@pytest.fixture
def updates(monkeypatch):
    monkeypatch.setattr(board_updates, "BOARD_UPDATE_INTERVAL_SECONDS", 0.01)
    return BoardUpdates()


async def test_bursts_keep_only_latest_snapshot(updates):
    edit = AsyncMock()
    tasks = [updates.queue(1, edit, content=str(n)) for n in range(300)]
    assert all(task is tasks[0] for task in tasks)
    await tasks[0]
    edit.assert_awaited_once_with(content="299")
    assert not updates._workers


async def test_terminal_bypasses_debounce_and_discards_counts(updates, monkeypatch):
    monkeypatch.setattr(board_updates, "BOARD_UPDATE_INTERVAL_SECONDS", 60)
    edit = AsyncMock()
    updates.queue(1, edit, content="stale", view="buttons")
    task = updates.queue(1, edit, terminal=True, content="results", view=None)
    updates.queue(1, edit, content="later stale", view="buttons")
    await asyncio.wait_for(task, 1)
    edit.assert_awaited_once_with(content="results", view=None)


async def test_throttle_keeps_latest_and_serializes_terminal(updates):
    started, release = asyncio.Event(), asyncio.Event()
    edits = []

    async def edit(**kwargs):
        if not edits:
            started.set()
            await release.wait()  # simulate discord.py waiting on Retry-After
        edits.append(kwargs)

    task = updates.queue(1, edit, content="first", view="buttons")
    await asyncio.wait_for(started.wait(), 1)
    for n in range(300):
        updates.queue(1, edit, content=str(n), view="buttons")
    updates.queue(1, edit, terminal=True, content="results", view=None)
    # A concurrent closed click only disables buttons; preserve queued results.
    updates.queue(1, edit, terminal=True, view=None)
    assert not task.done()
    release.set()
    await asyncio.wait_for(task, 1)
    assert edits == [
        {"content": "first", "view": "buttons"},
        {"content": "results", "view": None},
    ]


async def test_interval_applies_between_live_updates(updates, monkeypatch):
    interval = 0.03
    monkeypatch.setattr(board_updates, "BOARD_UPDATE_INTERVAL_SECONDS", interval)
    times = []

    async def edit(**kwargs):
        times.append(asyncio.get_running_loop().time())
        if len(times) == 1:
            updates.queue(1, edit, content="second")

    await updates.queue(1, edit, content="first")
    assert len(times) == 2
    assert times[1] - times[0] >= interval


@pytest.mark.parametrize("status", [403, 404, 500])
async def test_terminal_failure_is_retryable_except_missing_or_forbidden(updates, status):
    response = SimpleNamespace(status=status, reason="failure")
    cls = {403: discord.Forbidden, 404: discord.NotFound, 500: discord.HTTPException}[status]
    edit = AsyncMock(side_effect=cls(response, "failure"))
    task = updates.queue(1, edit, terminal=True, content="results")
    if status == 500:
        with pytest.raises(discord.HTTPException):
            await task
    else:
        await task
    assert not updates._workers


async def test_private_finish_discards_pending_update(updates):
    edit = AsyncMock()
    updates.queue(1, edit, content="stale")
    await updates.finish(1)
    edit.assert_not_awaited()
    assert not updates._workers


async def test_unload_cancels_and_cleans_workers(updates):
    started = asyncio.Event()

    async def edit(**kwargs):
        started.set()
        await asyncio.Event().wait()

    task = updates.queue(1, edit, content="count")
    await asyncio.wait_for(started.wait(), 1)
    await updates.close()
    assert task.cancelled()
    assert not updates._workers


async def test_unload_before_worker_starts(updates):
    edit = AsyncMock()
    task = updates.queue(1, edit, content="count")
    await updates.close()
    assert task.cancelled()
    assert not updates._workers
    edit.assert_not_awaited()

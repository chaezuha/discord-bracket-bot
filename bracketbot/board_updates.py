"""Coalesce live counts and serialize terminal edits without holding DB locks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import discord

log = logging.getLogger(__name__)
BOARD_UPDATE_INTERVAL_SECONDS = 1.0
Editor = Callable[..., Awaitable[object]]


@dataclass
class _Worker:
    edit: Editor
    pending: dict | None
    terminal: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None


class BoardUpdates:
    def __init__(self) -> None:
        self._workers: dict[int, _Worker] = {}

    def queue(self, message_id: int, edit: Editor, *, terminal: bool = False, **kwargs):
        """Queue while holding the bracket lock; workers never acquire that lock.

        Terminal edits supersede queued counts, but cannot overtake an in-flight
        request. Later terminal edits merge (e.g. disabling an already closing
        board must not discard its pending result text).
        """
        worker = self._workers.get(message_id)
        if worker is None:
            worker = _Worker(edit, kwargs, terminal)
            self._workers[message_id] = worker
            worker.task = asyncio.create_task(self._run(message_id, worker))
        elif not worker.terminal or terminal:
            if worker.terminal and worker.pending is not None:
                kwargs = worker.pending | kwargs
            worker.edit = edit
            worker.pending = kwargs
            worker.terminal = terminal
        if terminal:
            worker.wake.set()
        return worker.task

    async def _run(self, message_id: int, worker: _Worker) -> None:
        try:
            while worker.pending is not None:
                if not worker.terminal:
                    try:
                        await asyncio.wait_for(
                            worker.wake.wait(), timeout=BOARD_UPDATE_INTERVAL_SECONDS
                        )
                    except asyncio.TimeoutError:
                        pass
                kwargs, edit = worker.pending, worker.edit
                worker.pending = None
                terminal = worker.terminal
                try:
                    await edit(**kwargs)
                except (discord.NotFound, discord.Forbidden) as exc:
                    log.warning("Could not edit voting board %s: %s", message_id, exc)
                except Exception:
                    if terminal:
                        raise  # lifecycle keeps publication markers unset and retries
                    log.warning("Could not update voting board %s", message_id, exc_info=True)
        finally:
            self._workers.pop(message_id, None)

    async def finish(self, message_id: int) -> None:
        """Discard pending counts when a private board can no longer be edited."""
        if message_id not in self._workers:
            return

        async def noop(**kwargs):
            pass

        await self.queue(message_id, noop, terminal=True)

    async def close(self) -> None:
        tasks = [worker.task for worker in self._workers.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        # Tasks cancelled before their first event-loop turn never run finally.
        self._workers.clear()

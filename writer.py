from __future__ import annotations

import asyncio
import logging

from .config import SendConfig
from .models import ActivityRecord
from .storage import ActivityStore


logger = logging.getLogger(__name__)


class ActivityWriter:
    def __init__(self, store: ActivityStore, config: SendConfig) -> None:
        self._store = store
        self._config = config
        self._queue: asyncio.Queue[ActivityRecord] = asyncio.Queue(config.queue_size)
        self._task: asyncio.Task[None] | None = None
        self.dropped_records = 0
        self.last_dropped_at: float | None = None
        self.failed_batches = 0
        self.dead_letter_records: list[ActivityRecord] = []

    async def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="amia-send-writer")

    def enqueue(self, record: ActivityRecord) -> None:
        try:
            self._queue.put_nowait(record)
        except asyncio.QueueFull:
            self.dropped_records += 1
            self.last_dropped_at = asyncio.get_running_loop().time()
            logger.warning("send activity queue full; record dropped (total=%s)", self.dropped_records)

    async def stop(self) -> None:
        if self._task is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=10.0)
            except asyncio.TimeoutError:
                logger.error("send writer shutdown timed out with %s queued records", self._queue.qsize())
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            first = await self._queue.get()
            records = [first]
            deadline = asyncio.get_running_loop().time() + self._config.flush_interval_seconds
            try:
                while len(records) < self._config.batch_size:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    records.append(
                        await asyncio.wait_for(
                            self._queue.get(), remaining
                        )
                    )
            except TimeoutError:
                pass
            persisted = False
            for attempt in range(3):
                try:
                    await self._store.upsert_batch(records)
                    persisted = True
                    break
                except Exception:
                    if attempt < 2:
                        await asyncio.sleep(0.1 * (2**attempt))
            if not persisted:
                self.failed_batches += 1
                self.dead_letter_records.extend(records)
                logger.error("unable to persist send activity batch after retries; retained in memory dead letter queue")
            for _ in records:
                self._queue.task_done()

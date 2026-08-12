"""Persistent asynchronous task queue for image and gallery jobs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from .models import ImageTask, TaskStatus
from .storage import SQLiteStorage, StorageError

TaskHandler = Callable[[ImageTask], Awaitable[None]]


class TaskManager:
    """A small DB-backed queue with one or more cooperative workers.

    The database is the source of truth.  The asyncio event only reduces poll
    latency; a restarted plugin can recover queued tasks without an in-memory
    queue surviving the process.
    """

    def __init__(
        self,
        storage: SQLiteStorage,
        handler: TaskHandler,
        *,
        worker_count: int = 1,
        poll_interval: float = 0.5,
        max_queue_size: int = 100,
        logger: logging.Logger | None = None,
    ) -> None:
        self.storage = storage
        self.handler = handler
        self.worker_count = max(1, int(worker_count))
        self.poll_interval = max(0.05, float(poll_interval))
        self.max_queue_size = max(1, int(max_queue_size))
        self.logger = logger or logging.getLogger(__name__)
        self._wake = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []
        self._stopping = False
        self._started = False

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stopping = False
        requeued, failed = self.storage.recover_interrupted_tasks()
        if failed:
            self.logger.warning("%d 个已发起付费请求的任务被隔离为失败，避免重复计费", len(failed))
        for index in range(self.worker_count):
            self._workers.append(asyncio.create_task(self._worker_loop(index), name=f"maitu-worker-{index}"))
        if requeued:
            self._wake.set()

    async def stop(self) -> None:
        if not self._started:
            return
        self._stopping = True
        self._wake.set()
        for worker in self._workers:
            worker.cancel()
        for worker in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await worker
        self._workers.clear()
        self._started = False

    def submit(self, task: ImageTask) -> ImageTask:
        queued = self.storage.list_tasks(statuses=(TaskStatus.QUEUED,), limit=self.max_queue_size + 1)
        if len(queued) >= self.max_queue_size:
            raise StorageError("任务队列已满")
        created = self.storage.create_task(task)
        self._wake.set()
        return created

    def wake(self) -> None:
        self._wake.set()

    async def _worker_loop(self, index: int) -> None:
        while not self._stopping:
            task = self.storage.claim_next_task()
            if task is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval)
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                await self.handler(task)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # handler owns detailed task state when possible
                self.logger.exception("任务 %s worker %d 未处理异常", task.id, index)
                try:
                    self.storage.set_task_status(task.id, TaskStatus.FAILED, error_message=str(exc)[:1000])
                except Exception:
                    self.logger.exception("无法记录任务 %s 的失败状态", task.id)

    async def drain(self, timeout: float = 30.0) -> bool:
        """Wait until no queued/running tasks remain, primarily for tests."""

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            active = self.storage.list_tasks(statuses=(TaskStatus.QUEUED, TaskStatus.RUNNING), limit=1)
            if not active:
                return True
            await asyncio.sleep(min(self.poll_interval, 0.1))
        return False

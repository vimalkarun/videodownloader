"""In-memory asyncio job queue with bounded concurrency.

Nothing is persisted: if the process dies, queued jobs are simply gone (the
user still has their original message and can resend the link).
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import itertools
import logging
import threading
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from .url_resolver import Platform

log = logging.getLogger(__name__)


class JobState(enum.StrEnum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    UPLOADING = "uploading"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (JobState.DONE, JobState.FAILED, JobState.CANCELLED)


_seq = itertools.count(1)


@dataclass(eq=False)
class Job:
    url: str
    platform: Platform
    chat_id: int
    message_id: int
    user_id: int | None
    id: str = field(default_factory=lambda: f"{next(_seq):04d}-{uuid.uuid4().hex[:6]}")
    status_message_id: int | None = None
    state: JobState = JobState.QUEUED
    detail: str = ""
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    cancel_event: threading.Event = field(default_factory=threading.Event)
    task: asyncio.Task[None] | None = None

    def owned_by(self, user_id: int | None, chat_id: int) -> bool:
        if user_id is not None and self.user_id is not None:
            return self.user_id == user_id
        return self.chat_id == chat_id


Runner = Callable[[Job], Awaitable[None]]
Notifier = Callable[[Job], Awaitable[None]]


class QueueClosed(RuntimeError):
    pass


class DownloadQueue:
    """FIFO queue processed by ``concurrency`` worker tasks.

    ``runner(job)`` does the actual work and is responsible for its own
    error reporting, timeout and cleanup. ``on_dequeued_cancel(job)`` is
    awaited when a still-queued job is cancelled so its status message can
    be updated.
    """

    def __init__(self, runner: Runner, concurrency: int = 2,
                 on_queued_cancel: Notifier | None = None):
        self._runner = runner
        self._concurrency = max(1, concurrency)
        self._on_queued_cancel = on_queued_cancel
        self._queue: asyncio.Queue[Job] = asyncio.Queue()
        self._pending: list[Job] = []
        self._active: dict[str, Job] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._closing = False

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._workers:
            return
        self._workers = [asyncio.create_task(self._worker(i), name=f"dl-worker-{i}")
                         for i in range(self._concurrency)]

    async def shutdown(self, grace_seconds: float = 30) -> None:
        """Stop accepting jobs, cancel queued ones, give active jobs
        ``grace_seconds`` to finish, then cancel them. Each runner's own
        ``finally`` deletes its temp dir."""
        self._closing = True
        for job in list(self._pending):
            await self._cancel_pending(job)

        active = [j.task for j in self._active.values() if j.task and not j.task.done()]
        if active and grace_seconds > 0:
            log.info("shutdown: waiting up to %ss for %d active job(s)", grace_seconds, len(active))
            await asyncio.wait(active, timeout=grace_seconds)
        for job in list(self._active.values()):
            if job.task and not job.task.done():
                job.cancel_event.set()
                job.task.cancel()
        remaining = [j.task for j in self._active.values() if j.task]
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)

        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers = []

    # -- public API ---------------------------------------------------------

    def submit(self, job: Job) -> int:
        """Enqueue ``job``; return its 1-based position among waiting jobs
        (0 means a worker is free and it will start right away)."""
        if self._closing:
            raise QueueClosed()
        free_workers = self._concurrency - len(self._active)
        self._pending.append(job)
        self._queue.put_nowait(job)
        position = len(self._pending) - max(free_workers, 0)
        return max(position, 0)

    @property
    def pending(self) -> list[Job]:
        return list(self._pending)

    @property
    def active(self) -> list[Job]:
        return list(self._active.values())

    async def cancel_for(self, user_id: int | None, chat_id: int) -> tuple[int, int]:
        """Cancel the caller's queued and running jobs.
        Returns (queued_cancelled, active_cancelled)."""
        queued = 0
        for job in list(self._pending):
            if job.owned_by(user_id, chat_id):
                await self._cancel_pending(job)
                queued += 1
        running = 0
        for job in list(self._active.values()):
            if job.owned_by(user_id, chat_id) and job.task and not job.task.done():
                job.cancel_event.set()
                job.task.cancel()
                running += 1
        return queued, running

    # -- internals ----------------------------------------------------------

    async def _cancel_pending(self, job: Job) -> None:
        if job in self._pending:
            self._pending.remove(job)
        job.state = JobState.CANCELLED
        job.cancel_event.set()
        if self._on_queued_cancel:
            with contextlib.suppress(Exception):
                await self._on_queued_cancel(job)

    async def _worker(self, n: int) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job.state is JobState.CANCELLED:
                    continue
                if job in self._pending:
                    self._pending.remove(job)
                job.started_at = time.monotonic()
                self._active[job.id] = job
                job.task = asyncio.create_task(self._runner(job), name=f"job-{job.id}")
                # asyncio.wait (unlike `await task`) doesn't cancel the job
                # if this worker is cancelled; shutdown() handles jobs itself.
                await asyncio.wait({job.task})
                if not job.task.cancelled() and job.task.exception() is not None:
                    log.error("job=%s runner crashed: %r", job.id, job.task.exception())
            finally:
                self._active.pop(job.id, None)
                self._queue.task_done()

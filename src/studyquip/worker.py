"""A single asynchronous worker process with task-bound lease renewal."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
import uuid
from collections.abc import Awaitable
from types import FrameType
from typing import Any

from sqlalchemy.exc import OperationalError

from studyquip.ai import AIService, split_legacy_model_roles
from studyquip.config import Settings
from studyquip.db import Database, runtime_lock
from studyquip.jobs import JobStore, LeaseLost
from studyquip.pipelines import HANDLERS, NeedsReview, PipelineContext
from studyquip.scheduling import WindowClosed
from studyquip.subjects import ensure_default_subjects

logger = logging.getLogger(__name__)


async def with_heartbeat(
    jobs: JobStore, job: dict[str, Any], interval: float, operation: Awaitable[None]
) -> None:
    """Renew throughout all stages, even while waiting for another local lock."""
    work = asyncio.create_task(operation)

    async def pulse() -> None:
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(jobs.renew, job["id"], job["owner"], job["lease_token"])
            if not renewed:
                raise LeaseLost("任务租约已丢失或任务已取消")

    heartbeat = asyncio.create_task(pulse())
    try:
        done, _ = await asyncio.wait({work, heartbeat}, return_when=asyncio.FIRST_COMPLETED)
        if work in done:
            await work
        else:
            await heartbeat
    finally:
        work.cancel()
        heartbeat.cancel()
        await asyncio.gather(work, heartbeat, return_exceptions=True)


class Worker:
    def __init__(self, settings: Settings, db: Database, ai: AIService | None = None) -> None:
        self.settings, self.db = settings, db
        self.jobs = JobStore(db)
        self.ai = ai or AIService(db, settings)
        self.owner = str(uuid.uuid4())
        self.book_locks: dict[str, asyncio.Lock] = {}

    async def _dispatch(self, job: dict[str, Any]) -> None:
        handler = HANDLERS.get(job["kind"])
        if handler is None:
            raise ValueError(f"未知任务类型：{job['kind']}")
        ctx = PipelineContext(self.db, self.jobs, self.ai, self.settings, job)
        book_id: str | None = None
        if job["kind"] in {"book_process", "book_index"}:
            book_id = job["resource_id"]
        elif job["kind"] in {"page_recognize", "suggestion_regenerate"}:
            kind = "page" if job["kind"] == "page_recognize" else "suggestion"
            record = await asyncio.to_thread(self.db.get, kind, job["resource_id"])
            book_id = (record or {}).get("book_id")
        if book_id:
            async with self.book_locks.setdefault(book_id, asyncio.Lock()):
                await ctx.guard()
                await handler(ctx)
        else:
            await handler(ctx)

    async def execute(self, job: dict[str, Any]) -> None:
        lease = (job["id"], job["owner"], job["lease_token"])
        try:
            await with_heartbeat(self.jobs, job, self.settings.heartbeat_seconds, self._dispatch(job))
        except LeaseLost:
            logger.info("任务 %s 已失去租约，丢弃本次结果", job["id"])
        except WindowClosed as error:
            try:
                await asyncio.to_thread(self.jobs.defer, *lease, error.until, error.reason)
            except LeaseLost:
                pass
        except NeedsReview as error:
            try:
                await asyncio.to_thread(self.jobs.wait_for_review, *lease, str(error))
            except LeaseLost:
                pass
        except OperationalError:
            try:
                await asyncio.to_thread(
                    self.jobs.fail, *lease, "数据库暂时忙，已安排重试", retry_at=time.time() + 2
                )
            except (LeaseLost, OperationalError):
                logger.warning("任务 %s 暂时无法提交状态，将由租约恢复", job["id"])
        except Exception as error:
            try:
                await asyncio.to_thread(self.jobs.fail, *lease, str(error))
            except LeaseLost:
                pass
            logger.warning("任务 %s 失败：%s", job["id"], type(error).__name__)

    async def run(self, stop: asyncio.Event | None = None) -> None:
        stop = stop or asyncio.Event()
        active: set[asyncio.Task[None]] = set()
        try:
            await asyncio.to_thread(ensure_default_subjects, self.db, self.settings.default_subjects)
            converted = await asyncio.to_thread(split_legacy_model_roles, self.db)
            if converted:
                logger.info("已拆分 %s 个旧模型配置为教材与题目用途", converted)
            restored = await asyncio.to_thread(self.jobs.restore_review_pages)
            if restored:
                logger.info("已将 %s 个旧待校对页面恢复为草稿", restored)
            while not stop.is_set():
                while not stop.is_set():
                    job = await asyncio.to_thread(self.jobs.claim, self.owner)
                    if job is None:
                        break
                    task = asyncio.create_task(self.execute(job))
                    active.add(task)
                    task.add_done_callback(active.discard)
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.settings.worker_poll_seconds)
                except TimeoutError:
                    continue
        finally:
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)


async def run_worker(settings: Settings) -> None:
    with runtime_lock(settings, worker=True):
        db = Database(settings)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        handled_signals = [signal.SIGTERM]
        if sys.platform == "win32":
            handled_signals.append(signal.SIGBREAK)
        registered: dict[signal.Signals, tuple[Any, bool]] = {}

        def request_stop(signum: int, frame: FrameType | None) -> None:
            loop.call_soon_threadsafe(stop.set)

        try:
            for signum in handled_signals:
                previous = signal.getsignal(signum)
                try:
                    loop.add_signal_handler(signum, stop.set)
                    registered[signum] = (previous, False)
                except (NotImplementedError, RuntimeError):
                    # Windows event loops do not expose add_signal_handler.
                    try:
                        signal.signal(signum, request_stop)
                        registered[signum] = (previous, True)
                    except ValueError:
                        # Embedded execution in a non-main thread has no signal ownership.
                        pass
            await Worker(settings, db).run(stop)
        finally:
            for signum, (previous, native_handler) in registered.items():
                if not native_handler:
                    loop.remove_signal_handler(signum)
                signal.signal(signum, previous)
            db.close()

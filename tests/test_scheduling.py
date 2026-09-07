from __future__ import annotations

import asyncio
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from studyquip.ai import ModelProfile
from studyquip.jobs import LeaseLost
from studyquip.scheduling import CapacityLimiter, Window, model_bucket, next_allowed
from studyquip.worker import with_heartbeat


def configured(model: str, role: str = "book_text", limit: int = 2, total: int | None = 3) -> ModelProfile:
    return ModelProfile(
        base_url="https://provider.example/v1",
        api_key="fixture",
        model=model,
        role=role,
        max_concurrency=limit,
        credential_max_concurrency=total,
    )


def test_cross_midnight_window_and_role_independent_identity() -> None:
    zone = ZoneInfo("Asia/Shanghai")
    windows = [Window(start="23:00", end="06:00")]
    now = datetime(2026, 9, 6, 1, tzinfo=zone).timestamp()
    assert next_allowed(now, windows, "Asia/Shanghai") == now
    noon = datetime(2026, 9, 6, 12, tzinfo=zone).timestamp()
    assert next_allowed(noon, windows, "Asia/Shanghai") == datetime(2026, 9, 6, 23, tzinfo=zone).timestamp()
    assert (
        len(
            {
                model_bucket(b"secret", configured("same", role))
                for role in ("book_vision", "book_text", "question_vision", "question_text")
            }
        )
        == 1
    )


@pytest.mark.asyncio
async def test_two_limits_are_atomic_and_cancel_releases_both() -> None:
    first, second = configured("one"), configured("two")
    limiter = CapacityLimiter(b"fixture-secret")
    limiter.configure([first, second, configured("one", "question_vision", limit=1)])
    active = 0
    peak = 0
    by_model: dict[str, int] = {}
    release = asyncio.Event()

    async def work(profile: ModelProfile) -> None:
        nonlocal active, peak
        async with limiter.slot(profile):
            active += 1
            by_model[profile.model] = by_model.get(profile.model, 0) + 1
            peak = max(peak, active)
            assert active <= 3
            assert by_model[profile.model] <= (1 if profile.model == "one" else 2)
            try:
                await release.wait()
            finally:
                active -= 1
                by_model[profile.model] -= 1

    tasks = [asyncio.create_task(work(profile)) for profile in [first, first, second, second, second]]
    await asyncio.sleep(0.03)
    assert peak == 3
    tasks[0].cancel()
    await asyncio.gather(tasks[0], return_exceptions=True)
    release.set()
    await asyncio.gather(*tasks[1:])
    assert not any(limiter._models.values())
    assert not any(limiter._credentials.values())


@pytest.mark.asyncio
async def test_heartbeat_covers_blocking_offloaded_work_and_cancels_lost_lease() -> None:
    class LeaseFixture:
        def __init__(self) -> None:
            self.calls = 0
            self.valid = True

        def renew(self, *args: Any) -> bool:
            self.calls += 1
            return self.valid

    lease = LeaseFixture()
    job = {"id": "task", "owner": "worker", "lease_token": "attempt"}
    await with_heartbeat(lease, job, 0.01, asyncio.to_thread(time.sleep, 0.08))
    assert lease.calls >= 3
    lease.valid = False
    cancelled = asyncio.Event()

    async def long_reasoning() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(LeaseLost):
        await with_heartbeat(lease, job, 0.01, long_reasoning())
    assert cancelled.is_set()

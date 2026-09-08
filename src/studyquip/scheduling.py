"""Time windows and atomic credential/model request admission."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from pydantic import BaseModel, field_validator


class Window(BaseModel):
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def valid_clock(cls, value: str) -> str:
        parsed = datetime.strptime(value, "%H:%M")
        if parsed.strftime("%H:%M") != value:
            raise ValueError("时间必须为 HH:MM")
        return value


class WindowClosed(Exception):
    def __init__(self, until: float, reason: str = "等待模型允许的请求时间窗口") -> None:
        super().__init__(reason)
        self.until = until
        self.reason = reason


def retry_delay(attempt: int) -> float:
    """Shared backoff for transport failures and invalid structured output."""
    return min(2**attempt, 30)


def next_allowed(now: float, windows: Iterable[Window], timezone_name: str) -> float:
    """Return now when admitted; start == end explicitly means all day."""
    configured = list(windows)
    if not configured or any(item.start == item.end for item in configured):
        return now
    zone = ZoneInfo(timezone_name)
    local = datetime.fromtimestamp(now, zone)
    starts: list[float] = []
    for delta in range(-1, 3):
        day = local.date() + timedelta(days=delta)
        for item in configured:
            start = datetime.combine(day, datetime.strptime(item.start, "%H:%M").time(), zone)
            end_day = day + timedelta(days=item.end < item.start)
            end = datetime.combine(end_day, datetime.strptime(item.end, "%H:%M").time(), zone)
            if start.timestamp() <= now < end.timestamp():
                return now
            if start.timestamp() > now:
                starts.append(start.timestamp())
    return min(starts)


def normalized_endpoint(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.username or parts.password:
        raise ValueError("Base URL 必须是无内嵌凭据的 HTTP(S) URL")
    if parts.query or parts.fragment:
        raise ValueError("Base URL 不能包含 query 或 fragment")
    hostname = (parts.hostname or "").lower()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    port = parts.port
    standard = 443 if parts.scheme == "https" else 80
    authority = f"{hostname}:{port}" if port is not None and port != standard else hostname
    return urlunsplit((parts.scheme.lower(), authority, parts.path.rstrip("/"), "", ""))


def credential_identity(secret: bytes, profile: Any) -> str:
    value = [profile.api_key, profile.organization or "", profile.project or "", profile.auth_scope or ""]
    return hmac.new(secret, json.dumps(value, ensure_ascii=False).encode(), hashlib.sha256).hexdigest()


def model_bucket(secret: bytes, profile: Any) -> tuple[str, str]:
    identity = credential_identity(secret, profile)
    wire = [normalized_endpoint(profile.base_url), identity, profile.model]
    return hashlib.sha256(json.dumps(wire).encode()).hexdigest(), identity


class CapacityLimiter:
    """One worker owns admission. Both limits are acquired under one condition."""

    def __init__(self, secret: bytes) -> None:
        self.secret = secret
        self._condition = asyncio.Condition()
        self._models: Counter[str] = Counter()
        self._credentials: Counter[str] = Counter()
        self._model_limits: dict[str, int] = {}
        self._credential_limits: dict[str, int] = {}

    def configure(self, profiles: Iterable[Any]) -> None:
        models: dict[str, int] = {}
        credentials: dict[str, int] = {}
        for profile in profiles:
            bucket, identity = model_bucket(self.secret, profile)
            models[bucket] = min(models.get(bucket, profile.max_concurrency), profile.max_concurrency)
            cap = profile.credential_max_concurrency
            if cap is not None:
                credentials[identity] = min(credentials.get(identity, cap), cap)
        self._model_limits, self._credential_limits = models, credentials

    def effective(self, profile: Any) -> tuple[int, int | None]:
        bucket, identity = model_bucket(self.secret, profile)
        return self._model_limits.get(bucket, profile.max_concurrency), self._credential_limits.get(
            identity, profile.credential_max_concurrency
        )

    @asynccontextmanager
    async def slot(
        self,
        profile: Any,
        eligible: Callable[[], None] | None = None,
        refresh: Callable[[], Awaitable[None]] | None = None,
    ) -> AsyncIterator[None]:
        bucket, identity = model_bucket(self.secret, profile)
        while True:
            if refresh:
                await refresh()
            async with self._condition:
                if eligible:
                    eligible()
                model_cap, credential_cap = self.effective(profile)
                if self._models[bucket] < model_cap and (
                    credential_cap is None or self._credentials[identity] < credential_cap
                ):
                    self._models[bucket] += 1
                    self._credentials[identity] += 1
                    break
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=1)
                except TimeoutError:
                    continue
        try:
            yield
        finally:
            async with self._condition:
                self._models[bucket] -= 1
                self._credentials[identity] -= 1
                self._condition.notify_all()


def parse_retry_after(value: str | None, now: float, default: float) -> float:
    if not value:
        return default
    try:
        return max(0, float(value))
    except ValueError:
        from email.utils import parsedate_to_datetime

        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(0, date.timestamp() - now)
        except (ValueError, TypeError, OverflowError):
            return default

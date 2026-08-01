"""Request rate limiting.

Fixed-window counters, keyed by caller and bucket. Redis holds the counters
when it is reachable so a limit means the same thing across every worker;
otherwise each process falls back to an in-memory window, which still blunts a
brute-force attempt against a single instance.

Buckets exist because one number cannot serve every endpoint: signing in is
worth a handful of attempts per minute, listing clients is worth hundreds. The
route-to-bucket mapping lives in ``bucket_for`` and is deliberately path-based
so it applies before authentication has run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.config import settings

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60


@dataclass(frozen=True)
class Bucket:
    name: str
    limit: int


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: int
    remaining: int
    reset_after: int  # seconds until the window rolls over


def _buckets() -> dict[str, Bucket]:
    # Read from settings on each call so tests (and a config reload) see
    # changes without rebuilding the middleware.
    return {
        "auth": Bucket("auth", settings.rate_limit_auth_per_minute),
        "upload": Bucket("upload", settings.rate_limit_upload_per_minute),
        "anonymous": Bucket("anonymous", settings.rate_limit_anonymous_per_minute),
        "default": Bucket("default", settings.rate_limit_default_per_minute),
    }


# Endpoints that mint or spend credentials. Matched as a suffix on the path so
# the API prefix does not have to be baked in.
AUTH_PATH_SUFFIXES = ("/auth/login", "/auth/register")
UPLOAD_PATH_SUFFIXES = ("/documents/upload", "/portal/documents")


def bucket_for(path: str, method: str, *, authenticated: bool) -> Bucket:
    """Pick the bucket a request counts against."""
    buckets = _buckets()
    if method == "POST" and path.endswith(AUTH_PATH_SUFFIXES):
        return buckets["auth"]
    if method == "POST" and path.endswith(UPLOAD_PATH_SUFFIXES):
        return buckets["upload"]
    if not authenticated:
        return buckets["anonymous"]
    return buckets["default"]


# ------------------------------------------------------------------ backends --


class InMemoryCounter:
    """Per-process fixed-window counter.

    Entries are dropped lazily whenever the sweep interval has elapsed, so an
    attacker cycling keys cannot grow the map without bound for long.
    """

    SWEEP_INTERVAL_SECONDS = 300

    def __init__(self) -> None:
        self._windows: dict[str, tuple[int, int]] = {}  # key -> (window_start, count)
        self._lock = asyncio.Lock()
        self._last_sweep = 0.0

    async def hit(self, key: str, limit: int, window: int) -> Decision:
        now = int(time.time())
        window_start = now - (now % window)
        async with self._lock:
            self._sweep(now, window)
            start, count = self._windows.get(key, (window_start, 0))
            if start != window_start:
                start, count = window_start, 0
            count += 1
            self._windows[key] = (start, count)
        return _decide(count, limit, window, now, window_start)

    def _sweep(self, now: int, window: int) -> None:
        if now - self._last_sweep < self.SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        cutoff = now - (now % window)
        self._windows = {k: v for k, v in self._windows.items() if v[0] >= cutoff}

    async def reset(self) -> None:
        async with self._lock:
            self._windows.clear()


class RedisCounter:
    """Shared fixed-window counter backed by Redis INCR/EXPIRE.

    A Redis outage must not take the API down, so failures fall back to the
    in-memory counter and Redis is not retried for ``RETRY_AFTER_SECONDS``.
    """

    RETRY_AFTER_SECONDS = 30

    def __init__(self, url: str, fallback: InMemoryCounter) -> None:
        self._url = url
        self._fallback = fallback
        self._client = None
        self._unavailable_until = 0.0

    def _connect(self):
        if self._client is None:
            import redis.asyncio as redis  # imported lazily: optional at runtime

            self._client = redis.from_url(
                self._url,
                socket_connect_timeout=0.5,
                socket_timeout=0.5,
                health_check_interval=30,
            )
        return self._client

    async def hit(self, key: str, limit: int, window: int) -> Decision:
        now = int(time.time())
        if now < self._unavailable_until:
            return await self._fallback.hit(key, limit, window)

        window_start = now - (now % window)
        redis_key = f"caflow:ratelimit:{key}:{window_start}"
        try:
            client = self._connect()
            pipeline = client.pipeline()
            pipeline.incr(redis_key)
            # Expiry is set on every hit rather than only the first: an
            # INCR that raced a key eviction would otherwise leave the
            # counter with no TTL and lock the caller out indefinitely.
            pipeline.expire(redis_key, window + 1)
            count = (await pipeline.execute())[0]
        except Exception as exc:  # noqa: BLE001 - any client/network error degrades the same way
            self._unavailable_until = time.time() + self.RETRY_AFTER_SECONDS
            logger.warning(
                "Rate-limit store unavailable (%s); falling back to in-process counters "
                "for %ds",
                type(exc).__name__,
                self.RETRY_AFTER_SECONDS,
            )
            return await self._fallback.hit(key, limit, window)

        return _decide(int(count), limit, window, now, window_start)

    async def reset(self) -> None:
        await self._fallback.reset()
        self._unavailable_until = 0.0


def _decide(count: int, limit: int, window: int, now: int, window_start: int) -> Decision:
    return Decision(
        allowed=count <= limit,
        limit=limit,
        remaining=max(0, limit - count),
        reset_after=max(1, window_start + window - now),
    )


_memory = InMemoryCounter()
_counter: InMemoryCounter | RedisCounter | None = None


def get_counter() -> InMemoryCounter | RedisCounter:
    """The process-wide counter, built on first use."""
    global _counter
    if _counter is None:
        if settings.redis_url.startswith(("redis://", "rediss://", "unix://")):
            _counter = RedisCounter(settings.redis_url, _memory)
        else:
            _counter = _memory
    return _counter


async def reset_counters() -> None:
    """Drop every counter. Used by tests."""
    await get_counter().reset()

"""Rate-limit bucket selection and the two counter backends.

The middleware's behaviour over HTTP is covered in test_middleware.py; this
file exercises the parts that only run in production — the Redis-backed
counter, and what happens to it when Redis goes away.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.core.ratelimit import (
    WINDOW_SECONDS,
    Decision,
    InMemoryCounter,
    RedisCounter,
    bucket_for,
    get_counter,
)


def run(coro):
    return asyncio.run(coro)


class TestBucketSelection:
    def test_signing_in_gets_the_tight_credential_bucket(self):
        bucket = bucket_for("/api/v1/auth/login", "POST", authenticated=False)
        assert bucket.name == "auth"
        assert bucket.limit == settings.rate_limit_auth_per_minute

    def test_registration_shares_the_credential_bucket(self):
        assert bucket_for("/api/v1/auth/register", "POST", authenticated=False).name == "auth"

    def test_reading_the_login_route_is_not_a_credential_attempt(self):
        # Only a POST spends credentials; a GET should not eat the tight budget.
        assert bucket_for("/api/v1/auth/login", "GET", authenticated=False).name == "anonymous"

    @pytest.mark.parametrize(
        "path", ["/api/v1/documents/upload", "/api/v1/portal/documents"]
    )
    def test_uploads_get_their_own_budget(self, path):
        bucket = bucket_for(path, "POST", authenticated=True)
        assert bucket.name == "upload"
        assert bucket.limit == settings.rate_limit_upload_per_minute

    def test_anonymous_traffic_is_held_below_authenticated_traffic(self):
        anonymous = bucket_for("/api/v1/clients", "GET", authenticated=False)
        authenticated = bucket_for("/api/v1/clients", "GET", authenticated=True)
        assert anonymous.name == "anonymous"
        assert authenticated.name == "default"
        assert anonymous.limit < authenticated.limit

    @pytest.mark.parametrize(
        "path",
        [
            "/api/v1/auth/change-password",
            "/api/v1/auth/practitioners/"
            "11111111-1111-1111-1111-111111111111/reset-password",
        ],
    )
    def test_setting_a_password_shares_the_credential_bucket(self, path):
        """Both write a credential, and one of them checks the password in force.

        ``/auth/change-password`` asks the same question sign-in does, but from
        inside a session — so without this it counted against the generous
        ``default`` bucket, where the caller is authenticated. Someone holding a
        token they should not have is precisely who guesses at a password there,
        and a few hundred attempts a minute is not a rate at which bcrypt's cost
        buys anything.
        """
        bucket = bucket_for(path, "POST", authenticated=True)
        assert bucket.name == "auth"
        assert bucket.limit == settings.rate_limit_auth_per_minute

    def test_reading_a_password_route_is_not_a_credential_attempt(self):
        assert (
            bucket_for("/api/v1/auth/change-password", "GET", authenticated=True).name
            == "default"
        )

    def test_the_prefix_is_not_baked_in(self):
        # Buckets are chosen before routing, so the match is a path suffix and
        # survives a change to API_V1_PREFIX.
        assert bucket_for("/api/v2/auth/login", "POST", authenticated=False).name == "auth"


class TestInMemoryCounter:
    def test_it_allows_up_to_the_limit_then_refuses(self):
        counter = InMemoryCounter()
        decisions = [run(counter.hit("caller", 3, WINDOW_SECONDS)) for _ in range(4)]

        assert [d.allowed for d in decisions] == [True, True, True, False]
        assert [d.remaining for d in decisions] == [2, 1, 0, 0]

    def test_callers_are_counted_separately(self):
        counter = InMemoryCounter()
        run(counter.hit("first", 1, WINDOW_SECONDS))

        second = run(counter.hit("second", 1, WINDOW_SECONDS))
        assert second.allowed is True

    def test_a_new_window_starts_the_count_again(self, monkeypatch):
        counter = InMemoryCounter()
        now = 1_800_000_000
        monkeypatch.setattr("app.core.ratelimit.time.time", lambda: now)
        run(counter.hit("caller", 1, WINDOW_SECONDS))
        assert run(counter.hit("caller", 1, WINDOW_SECONDS)).allowed is False

        monkeypatch.setattr("app.core.ratelimit.time.time", lambda: now + WINDOW_SECONDS)
        assert run(counter.hit("caller", 1, WINDOW_SECONDS)).allowed is True

    def test_reset_after_counts_down_to_the_window_edge(self, monkeypatch):
        counter = InMemoryCounter()
        # 20 seconds into a 60-second window.
        monkeypatch.setattr("app.core.ratelimit.time.time", lambda: 1_800_000_020)
        decision = run(counter.hit("caller", 5, WINDOW_SECONDS))
        assert decision.reset_after == 40

    def test_stale_windows_are_swept(self, monkeypatch):
        # An attacker cycling keys must not grow the map without bound.
        counter = InMemoryCounter()
        now = 1_800_000_000
        monkeypatch.setattr("app.core.ratelimit.time.time", lambda: now)
        for index in range(50):
            run(counter.hit(f"caller-{index}", 10, WINDOW_SECONDS))
        assert len(counter._windows) == 50

        later = now + counter.SWEEP_INTERVAL_SECONDS + WINDOW_SECONDS
        monkeypatch.setattr("app.core.ratelimit.time.time", lambda: later)
        run(counter.hit("fresh-caller", 10, WINDOW_SECONDS))
        assert list(counter._windows) == ["fresh-caller"]

    def test_reset_clears_every_caller(self):
        counter = InMemoryCounter()
        run(counter.hit("caller", 1, WINDOW_SECONDS))
        run(counter.reset())
        assert run(counter.hit("caller", 1, WINDOW_SECONDS)).allowed is True


class FakePipeline:
    def __init__(self, store: dict, fail: bool = False):
        self._store = store
        self._fail = fail
        self._key: str | None = None

    def incr(self, key: str):
        self._key = key

    def expire(self, key: str, seconds: int):
        self._store.setdefault("expiries", {})[key] = seconds

    async def execute(self):
        if self._fail:
            raise ConnectionError("Redis is not answering")
        counts = self._store.setdefault("counts", {})
        counts[self._key] = counts.get(self._key, 0) + 1
        return [counts[self._key]]


class FakeRedis:
    """Just enough of redis.asyncio for the counter under test."""

    def __init__(self, fail: bool = False):
        self.store: dict = {}
        self.fail = fail
        self.pipelines = 0

    def pipeline(self):
        self.pipelines += 1
        return FakePipeline(self.store, self.fail)


class TestRedisCounter:
    def test_it_counts_in_the_shared_store(self, monkeypatch):
        fake = FakeRedis()
        counter = RedisCounter("redis://localhost:6379/0", InMemoryCounter())
        monkeypatch.setattr(counter, "_connect", lambda: fake)

        decisions = [run(counter.hit("caller", 2, WINDOW_SECONDS)) for _ in range(3)]
        assert [d.allowed for d in decisions] == [True, True, False]

    def test_the_key_carries_the_window_so_it_rolls_over(self, monkeypatch):
        fake = FakeRedis()
        counter = RedisCounter("redis://localhost:6379/0", InMemoryCounter())
        monkeypatch.setattr(counter, "_connect", lambda: fake)

        now = 1_800_000_000
        monkeypatch.setattr("app.core.ratelimit.time.time", lambda: now)
        run(counter.hit("caller", 5, WINDOW_SECONDS))
        monkeypatch.setattr("app.core.ratelimit.time.time", lambda: now + WINDOW_SECONDS)
        run(counter.hit("caller", 5, WINDOW_SECONDS))

        assert len(fake.store["counts"]) == 2

    def test_every_hit_re_sets_the_expiry(self, monkeypatch):
        # An INCR that raced an eviction would otherwise leave a counter with
        # no TTL, locking the caller out for good.
        fake = FakeRedis()
        counter = RedisCounter("redis://localhost:6379/0", InMemoryCounter())
        monkeypatch.setattr(counter, "_connect", lambda: fake)

        run(counter.hit("caller", 5, WINDOW_SECONDS))
        assert set(fake.store["expiries"].values()) == {WINDOW_SECONDS + 1}

    def test_an_outage_degrades_to_the_in_process_counter(self, monkeypatch):
        fallback = InMemoryCounter()
        counter = RedisCounter("redis://localhost:6379/0", fallback)
        monkeypatch.setattr(counter, "_connect", lambda: FakeRedis(fail=True))

        # The API must keep serving; the limit just stops being shared.
        decision = run(counter.hit("caller", 2, WINDOW_SECONDS))
        assert decision.allowed is True
        assert isinstance(decision, Decision)
        assert len(fallback._windows) == 1

    def test_redis_is_not_retried_on_every_request_after_an_outage(self, monkeypatch):
        failing = FakeRedis(fail=True)
        counter = RedisCounter("redis://localhost:6379/0", InMemoryCounter())
        monkeypatch.setattr(counter, "_connect", lambda: failing)

        for _ in range(5):
            run(counter.hit("caller", 100, WINDOW_SECONDS))

        # One attempt, then the circuit stays open for RETRY_AFTER_SECONDS.
        assert failing.pipelines == 1

    def test_it_tries_redis_again_once_the_cool_off_has_passed(self, monkeypatch):
        fake = FakeRedis(fail=True)
        counter = RedisCounter("redis://localhost:6379/0", InMemoryCounter())
        monkeypatch.setattr(counter, "_connect", lambda: fake)

        now = 1_800_000_000
        monkeypatch.setattr("app.core.ratelimit.time.time", lambda: now)
        run(counter.hit("caller", 100, WINDOW_SECONDS))
        assert fake.pipelines == 1

        later = now + counter.RETRY_AFTER_SECONDS + 1
        monkeypatch.setattr("app.core.ratelimit.time.time", lambda: later)
        run(counter.hit("caller", 100, WINDOW_SECONDS))
        assert fake.pipelines == 2

    def test_reset_clears_the_fallback_and_closes_the_circuit(self, monkeypatch):
        fallback = InMemoryCounter()
        counter = RedisCounter("redis://localhost:6379/0", fallback)
        monkeypatch.setattr(counter, "_connect", lambda: FakeRedis(fail=True))
        run(counter.hit("caller", 100, WINDOW_SECONDS))
        assert counter._unavailable_until > 0

        run(counter.reset())
        assert counter._unavailable_until == 0.0
        assert fallback._windows == {}


class TestCounterSelection:
    def test_the_suite_runs_on_the_in_memory_counter(self):
        # conftest points REDIS_URL at memory://, so no test dials out.
        assert isinstance(get_counter(), InMemoryCounter)

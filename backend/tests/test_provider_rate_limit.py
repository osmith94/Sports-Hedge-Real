"""Reusable provider 429 cooldown primitive.

Wave R3 introduced the primitive on Matchbook login. Concurrent HOT/UNIVERSE
workers now share the same cooldown truth on Kalshi and Polymarket public GETs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from email.utils import format_datetime

import httpx
import pytest

from sports_hedge.venues import kalshi as kalshi_mod
from sports_hedge.venues import matchbook as matchbook_mod
from sports_hedge.venues import polymarket as polymarket_mod
from sports_hedge.venues.rate_limit import (
    ProviderCooldown,
    ProviderRateLimitedError,
    RateLimitPolicy,
    retry_after_seconds,
)


class FakeMono:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def test_retry_after_delta_seconds_and_http_date_cap() -> None:
    now = datetime(2026, 9, 16, 20, 30, tzinfo=UTC)
    retry_at = datetime(2026, 9, 16, 20, 40, tzinfo=UTC)
    policy_cap = RateLimitPolicy(fallback_seconds=5, max_seconds=30, provider="example-get")
    dated = {"Retry-After": format_datetime(retry_at, usegmt=True)}
    assert retry_after_seconds(dated, now=now, policy=policy_cap) == 30
    uncapped = RateLimitPolicy(fallback_seconds=5, max_seconds=900)
    assert retry_after_seconds(dated, now=now, policy=uncapped) == 600
    assert retry_after_seconds({}, now=now, policy=policy_cap) == 5
    assert retry_after_seconds({"retry-after": "12"}, now=now, policy=policy_cap) == 12


def test_public_get_429_fail_fast_without_repeat_observe() -> None:
    """Simulate a public GET adapter wrapping `_get` with the shared primitive."""

    mono = FakeMono(10.0)
    cooldown = ProviderCooldown(
        RateLimitPolicy(fallback_seconds=7, max_seconds=30, provider="example-public-get"),
        monotonic_clock=mono,
    )
    issued = {"gets": 0}

    def public_get(*, status: int, headers: dict[str, str] | None = None) -> None:
        cooldown.raise_if_active()
        issued["gets"] += 1
        entered = cooldown.observe_status(status, headers or {}, now=datetime.now(UTC))
        if entered is not None:
            raise ProviderRateLimitedError(entered, provider=cooldown.policy.provider)

    with pytest.raises(ProviderRateLimitedError) as first:
        public_get(status=429, headers={"Retry-After": "12"})
    assert first.value.retry_after_seconds == 12
    assert first.value.provider == "example-public-get"
    assert "429" in str(first.value)

    with pytest.raises(ProviderRateLimitedError) as second:
        public_get(status=429, headers={"Retry-After": "12"})
    assert 0 < second.value.retry_after_seconds <= 12
    assert issued["gets"] == 1

    mono.value = 22.1
    with pytest.raises(ProviderRateLimitedError):
        public_get(status=429)
    assert issued["gets"] == 2


def test_observe_status_ignores_non_429_and_clear_resets() -> None:
    cooldown = ProviderCooldown(
        RateLimitPolicy(fallback_seconds=5, max_seconds=30, provider="example-get"),
        monotonic_clock=FakeMono(1.0),
    )
    assert cooldown.observe_status(200, {"Retry-After": "9"}) is None
    assert cooldown.is_active() is False
    assert cooldown.observe_status(429, {}) == 5
    assert cooldown.is_active() is True
    with pytest.raises(ProviderRateLimitedError):
        cooldown.raise_if_active()
    cooldown.clear()
    assert cooldown.is_active() is False


def test_httpx_response_headers_work_for_retry_after() -> None:
    now = datetime(2026, 9, 16, 20, 30, tzinfo=UTC)
    response = httpx.Response(429, headers={"Retry-After": "8"})
    policy = RateLimitPolicy(fallback_seconds=5, max_seconds=30)
    assert retry_after_seconds(response.headers, now=now, policy=policy) == 8


def test_shared_runtime_wires_kalshi_and_polymarket_cooldown() -> None:
    import inspect

    assert "ProviderCooldown" in inspect.getsource(matchbook_mod)
    assert "ProviderCooldown" in inspect.getsource(kalshi_mod)
    assert "ProviderCooldown" in inspect.getsource(polymarket_mod)
    assert "raise_if_active" in inspect.getsource(kalshi_mod)
    assert "raise_if_active" in inspect.getsource(polymarket_mod)

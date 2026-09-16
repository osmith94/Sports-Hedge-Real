"""Reusable HTTP 429 cooldown/backoff for read-only venue adapters.

Wave R3 wires this only to Matchbook login. The primitive itself is not
Matchbook-specific: a later lane can wrap Kalshi/Polymarket public GETs
with the same fail-fast cooldown without re-implementing Retry-After
parsing or in-process backoff state.

This is intentionally not a sleeper or retry loop. After a 429 the caller
must fail closed, report a truthful health/diagnostic, and skip further
requests until the cooldown expires.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from time import monotonic
from typing import Any

DEFAULT_FALLBACK_SECONDS = 30.0
DEFAULT_MAX_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class RateLimitPolicy:
    """Bounded cooldown policy for one provider or named request bucket."""

    fallback_seconds: float = DEFAULT_FALLBACK_SECONDS
    max_seconds: float = DEFAULT_MAX_SECONDS
    provider: str = "provider"

    def fallback_wait(self) -> float:
        cap = max(0.0, float(self.max_seconds))
        fallback = max(float(self.fallback_seconds), 1.0)
        if cap > 0:
            return min(fallback, cap)
        return fallback


class ProviderRateLimitedError(RuntimeError):
    """Provider is cooling down after HTTP 429. Fail fast; do not retry."""

    def __init__(
        self,
        retry_after_seconds: float,
        *,
        provider: str = "provider",
        detail: str | None = None,
    ) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        self.provider = provider
        wait_s = max(1, int(math.ceil(self.retry_after_seconds)))
        self.detail = detail or f"{provider} rate-limited (HTTP 429); retry after {wait_s}s"
        super().__init__(self.detail)


def retry_after_seconds(
    headers: Mapping[str, Any] | None,
    *,
    now: datetime,
    policy: RateLimitPolicy | None = None,
) -> float:
    """Honor Retry-After (delta-seconds or HTTP-date) when valid.

    Otherwise return the policy's bounded fallback. Header lookup is
    case-insensitive so both ``httpx.Headers`` and plain dicts work.
    """

    resolved = policy or RateLimitPolicy()
    fallback = resolved.fallback_wait()
    cap = max(0.0, float(resolved.max_seconds))
    raw = _header_value(headers, "Retry-After")
    if raw is None:
        return fallback
    text = str(raw).strip()
    if not text:
        return fallback
    try:
        delta = float(text)
    except ValueError:
        delta = None
    else:
        if delta <= 0:
            return fallback
        return min(delta, cap) if cap > 0 else delta

    try:
        when = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        return fallback
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    remaining = (when - now).total_seconds()
    if remaining <= 0:
        return fallback
    return min(remaining, cap) if cap > 0 else remaining


def _header_value(headers: Mapping[str, Any] | None, name: str) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if callable(getter):
        direct = getter(name)
        if direct is not None and str(direct).strip():
            return str(direct)
    target = name.lower()
    for key, value in headers.items():
        if str(key).lower() == target and value is not None and str(value).strip():
            return str(value)
    return None


class ProviderCooldown:
    """Process-local fail-fast cooldown for one provider/bucket.

    Typical later-lane public GET wrap:

        cooldown.raise_if_active()
        response = await client.get(...)
        if cooldown.observe_status(response.status_code, response.headers) is not None:
            raise ProviderRateLimitedError(
                cooldown.remaining_seconds(),
                provider=cooldown.policy.provider,
            )
    """

    def __init__(
        self,
        policy: RateLimitPolicy | None = None,
        *,
        monotonic_clock: Callable[[], float] | None = None,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.policy = policy or RateLimitPolicy()
        self._monotonic = monotonic_clock or monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._until: float | None = None

    def remaining_seconds(self) -> float:
        if self._until is None:
            return 0.0
        remaining = self._until - self._monotonic()
        if remaining <= 0:
            self._until = None
            return 0.0
        return remaining

    def is_active(self) -> bool:
        return self.remaining_seconds() > 0

    def raise_if_active(self) -> None:
        remaining = self.remaining_seconds()
        if remaining > 0:
            raise ProviderRateLimitedError(remaining, provider=self.policy.provider)

    def enter(self, retry_after: float) -> float:
        wait = max(0.0, float(retry_after))
        self._until = self._monotonic() + wait
        return wait

    def enter_from_headers(
        self,
        headers: Mapping[str, Any] | None,
        *,
        now: datetime | None = None,
    ) -> float:
        wait = retry_after_seconds(
            headers,
            now=now if now is not None else self._wall_clock(),
            policy=self.policy,
        )
        return self.enter(wait)

    def observe_status(
        self,
        status_code: int,
        headers: Mapping[str, Any] | None,
        *,
        now: datetime | None = None,
    ) -> float | None:
        """Enter cooldown on HTTP 429. Other statuses are ignored."""

        if int(status_code) != 429:
            return None
        return self.enter_from_headers(headers, now=now)

    def clear(self) -> None:
        self._until = None

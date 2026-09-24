"""Per-slice coalescing of identical exact-ID provider reads.

BACKGROUND pricing may ask for the same Matchbook market, Kalshi ticker, or
Polymarket token from more than one catalogue row. Within one pricing slice
that request is issued once and the payload is fanned out. The cache lives
only as long as the slice context is bound; the next slice starts empty.

PAPER / read-only. This module does not call venues and does not raise
provider caps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from contextvars import ContextVar, Token
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from sports_hedge.venues.matchbook import MatchbookMarketGoneError

_EXACT_ID_COALESCER: ContextVar[ExactIdSliceCoalescer | None] = ContextVar(
    "sports_hedge_exact_id_coalescer",
    default=None,
)


@dataclass(frozen=True, slots=True)
class ExactRequestKey:
    """Identity that makes one provider response reusable inside a slice."""

    venue: str
    stage: str
    parts: tuple[str, ...]

    def stage_label(self) -> str:
        return f"{self.venue}:{self.stage}"


@dataclass(slots=True)
class _SharedOutcome:
    value: Any = None
    error: BaseException | None = None


def current_exact_id_coalescer() -> ExactIdSliceCoalescer | None:
    return _EXACT_ID_COALESCER.get()


@contextmanager
def bind_exact_id_coalescer(coalescer: ExactIdSliceCoalescer):
    """Bind coalescing to this task and to tasks created while it is bound."""

    token: Token[ExactIdSliceCoalescer | None] = _EXACT_ID_COALESCER.set(coalescer)
    try:
        yield coalescer
    finally:
        _EXACT_ID_COALESCER.reset(token)


def _fanout_error(exc: BaseException) -> BaseException:
    """Give each dependant its own exception instance."""

    if isinstance(exc, MatchbookMarketGoneError):
        return MatchbookMarketGoneError(exc.event_id, exc.market_id, exc.status_code)
    try:
        if exc.args:
            return type(exc)(*exc.args)
        return type(exc)(str(exc))
    except Exception:
        return RuntimeError(f"{type(exc).__name__}: {exc}")


def _materialize(outcome: _SharedOutcome) -> Any:
    if outcome.error is not None:
        raise _fanout_error(outcome.error)
    return deepcopy(outcome.value)


@dataclass
class ExactIdSliceCoalescer:
    """In-flight and completed exact-ID reads for a single pricing slice."""

    issued_provider_calls: int = 0
    coalesced_provider_calls: int = 0
    repeated_exact_id_calls: int = 0
    provider_stage_calls: dict[str, int] = field(default_factory=dict)
    _done: dict[ExactRequestKey, _SharedOutcome] = field(default_factory=dict)
    _inflight: dict[ExactRequestKey, asyncio.Future[_SharedOutcome]] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def share(
        self,
        key: ExactRequestKey,
        leader: Callable[[], Awaitable[Any]],
        *,
        on_join: Callable[[], None] | None = None,
    ) -> Any:
        """Run ``leader`` once per key; later callers in this slice reuse it.

        ``on_join`` runs for every non-leader so the caller can close a
        coroutine object that will not be awaited.
        """

        async with self._lock:
            cached = self._done.get(key)
            if cached is not None:
                self.coalesced_provider_calls += 1
                outcome: _SharedOutcome | None = cached
                is_leader = False
                waiter: asyncio.Future[_SharedOutcome] | None = None
            else:
                waiter = self._inflight.get(key)
                if waiter is None:
                    waiter = asyncio.get_running_loop().create_future()
                    self._inflight[key] = waiter
                    self.issued_provider_calls += 1
                    label = key.stage_label()
                    self.provider_stage_calls[label] = (
                        self.provider_stage_calls.get(label, 0) + 1
                    )
                    is_leader = True
                    outcome = None
                else:
                    self.coalesced_provider_calls += 1
                    is_leader = False
                    outcome = None
        if not is_leader:
            if on_join is not None:
                on_join()
            if outcome is None:
                assert waiter is not None
                outcome = await asyncio.shield(waiter)
            return _materialize(outcome)

        assert waiter is not None
        try:
            value = await leader()
            stored = _SharedOutcome(value=deepcopy(value))
        except asyncio.CancelledError:
            if not waiter.done():
                waiter.cancel()
            raise
        except Exception as exc:
            stored = _SharedOutcome(error=exc)
            async with self._lock:
                self._done[key] = stored
                self._inflight.pop(key, None)
            if not waiter.done():
                waiter.set_result(stored)
            raise _fanout_error(exc) from exc
        async with self._lock:
            self._done[key] = stored
            self._inflight.pop(key, None)
        if not waiter.done():
            waiter.set_result(stored)
        return _materialize(stored)

    def clear(self) -> None:
        """Drop slice-local books. In-flight waiters are not completed here."""

        self._done.clear()
        self.issued_provider_calls = 0
        self.coalesced_provider_calls = 0
        self.repeated_exact_id_calls = 0
        self.provider_stage_calls.clear()

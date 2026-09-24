"""Shared exact-ID stage scheduler for catalogue price refresh.

UNIVERSE has already catalogued native IDs. This scheduler refreshes those
IDs. It does not discover, rematch, or raise provider caps.

Callers choose the provider lane and what an unexpected row exception does.
BACKGROUND uses ``background`` plus retry. The HOT latency consumer uses
``hot`` and records the row failed without a retry. ACTIVE TRADE does not
call this scheduler.

Stage order is Matchbook, then Kalshi with the existing optimistic upper
bound, then Polymarket. A slow call occupies one venue slot. Other rows keep
using the other venues up to the existing caps.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from sports_hedge.application.price_engine import (
    ExactIdPricingPrep,
    PriceEngineItemStatus,
    PriceEngineRuntimeItem,
    PriceEngineSliceResult,
    RetrievedVenuePayload,
)
from sports_hedge.domain.models import VenueName

_STAGE_VENUE = {
    "matchbook": VenueName.MATCHBOOK,
    "kalshi": VenueName.KALSHI,
    "polymarket": VenueName.POLYMARKET,
}


class RowExceptionPolicy(StrEnum):
    """What to do with an unexpected exception from one catalogue row.

    Provider timeouts and rate limits are statuses returned by the stage
    loaders. They are not this policy. This policy is only for exceptions
    that escape a stage or publish, such as a pricing error.
    """

    RETRY = "retry"
    ISOLATE_WITHOUT_RETRY = "isolate_without_retry"


@dataclass
class _RowJob:
    runtime: PriceEngineRuntimeItem
    prep: ExactIdPricingPrep
    stage: str
    started: bool = False
    terminal: PriceEngineItemStatus | None = None
    matchbook: RetrievedVenuePayload | None = None
    known_implied: dict[str, Any] = field(default_factory=dict)
    kalshi_books: Mapping[str, RetrievedVenuePayload] | None = None
    polymarket_books: Mapping[str, RetrievedVenuePayload] | None = None


def _initial_stage(prep: ExactIdPricingPrep) -> str:
    if prep.matchbook_ready:
        return "matchbook"
    if prep.kalshi_ready:
        return "kalshi"
    return "polymarket"


def _stage_after(job: _RowJob, finished: str) -> str:
    prep = job.prep
    if finished == "matchbook":
        if prep.kalshi_ready:
            return "kalshi"
        if prep.polymarket_ready:
            return "polymarket"
        return "publish"
    if finished == "kalshi":
        if prep.polymarket_ready:
            return "polymarket"
        return "publish"
    return "publish"


def _venue_limits(engine: Any) -> dict[VenueName, int]:
    access = engine.provider_access
    if access is None:
        return {
            VenueName.MATCHBOOK: 4,
            VenueName.KALSHI: 4,
            VenueName.POLYMARKET: 8,
        }
    limits = access.limits
    return {
        VenueName.MATCHBOOK: int(limits.get(VenueName.MATCHBOOK, 4)),
        VenueName.KALSHI: int(limits.get(VenueName.KALSHI, 4)),
        VenueName.POLYMARKET: int(limits.get(VenueName.POLYMARKET, 8)),
    }


async def run_staged_exact_id_slice(
    engine: Any,
    rows: list[PriceEngineRuntimeItem],
    result: PriceEngineSliceResult,
    *,
    remaining: Callable[[], float | None],
    lane: str,
    exception_policy: RowExceptionPolicy,
) -> None:
    """Price rows by provider stage.

    ``lane`` is the provider-access lane for every read in this slice.
    ``exception_policy`` does not change optimistic pruning or caps.
    """

    limits = _venue_limits(engine)
    queues: dict[str, deque[_RowJob]] = {
        "matchbook": deque(),
        "kalshi": deque(),
        "polymarket": deque(),
    }
    publish: deque[_RowJob] = deque()
    jobs: list[_RowJob] = []
    inflight = {stage: 0 for stage in queues}
    tasks: dict[asyncio.Task[Any], tuple[_RowJob, str]] = {}

    def _finish(job: _RowJob, outcome: PriceEngineItemStatus) -> None:
        if job.terminal is not None:
            return
        engine._record_outcome(job.runtime, outcome, result)
        job.runtime.in_flight = False
        job.terminal = outcome

    def _fail(job: _RowJob, exc: BaseException) -> None:
        if job.terminal is not None:
            return
        try:
            from sports_hedge.application.price_engine import _SLICE_DIAGNOSTICS

            diagnostics = _SLICE_DIAGNOSTICS.get()
            if diagnostics is not None:
                diagnostics.note_worker_error(exc)
        except Exception:
            pass
        job.runtime.last_error_stage = "price_item"
        job.runtime.last_error_detail = f"{type(exc).__name__}: {exc}"
        if exception_policy is RowExceptionPolicy.RETRY:
            outcome = engine._schedule_retry(job.runtime, job.runtime.last_error_detail)
            _finish(job, outcome)
            return
        job.runtime.status = PriceEngineItemStatus.FAILED
        _finish(job, PriceEngineItemStatus.FAILED)

    def _retire_unstarted(job: _RowJob) -> None:
        if job.terminal is not None:
            return
        job.runtime.status = PriceEngineItemStatus.NOT_STARTED
        engine._record_outcome(job.runtime, PriceEngineItemStatus.NOT_STARTED, result)
        engine._record_item_deadline_miss(job.runtime)
        job.runtime.in_flight = False
        job.terminal = PriceEngineItemStatus.NOT_STARTED

    def _wall_closed() -> bool:
        left = remaining()
        return left is not None and left <= 0

    for runtime in rows:
        marked = engine._mark_price_item_started(runtime, lane=lane)
        try:
            prepared = engine._prepare_exact_id_pricing(runtime, result, lane=marked)
        except asyncio.CancelledError:
            runtime.in_flight = False
            raise
        except Exception as exc:  # noqa: BLE001 — one row must not abort the slice
            job = _RowJob(
                runtime=runtime,
                prep=ExactIdPricingPrep(
                    lane=marked,
                    active_lane=False,
                    matchbook_ready=False,
                    kalshi_ready=False,
                    polymarket_ready=False,
                    pm_tokens=[],
                    skip_bound=False,
                ),
                stage="publish",
            )
            jobs.append(job)
            _fail(job, exc)
            continue
        if isinstance(prepared, PriceEngineItemStatus):
            engine._record_outcome(runtime, prepared, result)
            runtime.in_flight = False
            continue
        job = _RowJob(runtime=runtime, prep=prepared, stage=_initial_stage(prepared))
        jobs.append(job)
        queues[job.stage].append(job)

    def _accept(job: _RowJob, finished: str, value: Any) -> None:
        if isinstance(value, PriceEngineItemStatus):
            _finish(job, value)
            return
        if finished == "matchbook":
            job.matchbook, job.known_implied = value
        elif finished == "kalshi":
            job.kalshi_books = value
        else:
            job.polymarket_books = value
        job.stage = _stage_after(job, finished)
        if job.stage == "publish":
            publish.append(job)
        else:
            queues[job.stage].append(job)

    async def _run_stage(job: _RowJob) -> Any:
        if job.stage == "matchbook":
            return await engine._load_matchbook_stage(job.runtime, result, prep=job.prep)
        if job.stage == "kalshi":
            return await engine._load_kalshi_stage(
                job.runtime,
                result,
                prep=job.prep,
                known_implied=job.known_implied,
            )
        return await engine._load_polymarket_stage(
            job.runtime,
            prep=job.prep,
            matchbook=job.matchbook,
            kalshi_books=job.kalshi_books,
        )

    def _dispatch() -> None:
        for stage in ("matchbook", "kalshi", "polymarket"):
            venue = _STAGE_VENUE[stage]
            cap = max(1, int(limits.get(venue, 1)))
            queue = queues[stage]
            while inflight[stage] < cap and queue:
                nxt = queue[0]
                if _wall_closed() and not nxt.started:
                    queue.popleft()
                    _retire_unstarted(nxt)
                    continue
                queue.popleft()
                nxt.started = True
                inflight[stage] += 1
                task = asyncio.create_task(_run_stage(nxt), name=f"exact-id-{stage}")
                tasks[task] = (nxt, stage)

    async def _publish(job: _RowJob) -> None:
        try:
            outcome = await engine._publish_loaded_books(
                job.runtime,
                result,
                matchbook=job.matchbook,
                kalshi_books=job.kalshi_books,
                polymarket_books=job.polymarket_books,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — publish failure stays on this row
            _fail(job, exc)
            return
        _finish(job, outcome)

    try:
        while True:
            while publish:
                await _publish(publish.popleft())
            if _wall_closed():
                for stage, queue in list(queues.items()):
                    kept: deque[_RowJob] = deque()
                    while queue:
                        waiting = queue.popleft()
                        if waiting.started:
                            kept.append(waiting)
                        else:
                            _retire_unstarted(waiting)
                    queues[stage] = kept
            _dispatch()
            if not tasks and not publish and not any(queues.values()):
                break
            if not tasks:
                for queue in queues.values():
                    while queue:
                        leftover = queue.popleft()
                        if leftover.terminal is None:
                            _retire_unstarted(leftover)
                if not publish:
                    break
                continue
            done, _pending = await asyncio.wait(set(tasks), return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                job, stage = tasks.pop(task)
                inflight[stage] = max(0, inflight[stage] - 1)
                try:
                    value = task.result()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — stage failure stays on this row
                    _fail(job, exc)
                    continue
                _accept(job, stage, value)
    except asyncio.CancelledError:
        for task in list(tasks):
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        raise
    finally:
        for job in jobs:
            if job.terminal is None:
                job.runtime.in_flight = False

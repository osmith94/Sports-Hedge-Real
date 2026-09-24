"""Latency-first HOT consumer of the shared exact-ID stage scheduler.

Production ``CataloguePriceEngine.run_slice(HOT)`` calls this module.
ACTIVE TRADE still calls ``_price_item`` with the active-trade lane from
live refresh. BACKGROUND uses a separate coalescer.

This consumer is the reviewed shape for switching HOT later:

- provider lane is ``hot``, so a queued HOT read is granted ahead of BACKGROUND;
- exact catalogue IDs only, with the same optimistic Kalshi pruning;
- one stalled venue call occupies that venue's slot, not unrelated HOT rows;
- identical exact-ID reads coalesce inside this slice only;
- an unexpected row exception is isolated and is not turned into a retry;
- a finished row publishes through the existing economics path immediately.

Caps stay Matchbook 4 / Kalshi 4 / Polymarket 8. The provider timeout stays
the engine's existing timeout (default 8s). This module does not discover.
"""

from __future__ import annotations

from collections.abc import Callable

from sports_hedge.application.exact_id_coalesce import (
    ExactIdSliceCoalescer,
    bind_exact_id_coalescer,
)
from sports_hedge.application.exact_id_stage_scheduler import (
    RowExceptionPolicy,
    run_staged_exact_id_slice,
)
from sports_hedge.application.price_engine import (
    PriceEngineRuntimeItem,
    PriceEngineSliceResult,
)
from sports_hedge.application.scan_lanes import ScanLane

HOT_LATENCY_CALL_SHAPE = "hot_latency_staged_exact_id"


async def run_hot_latency_exact_id_slice(
    engine: object,
    rows: list[PriceEngineRuntimeItem],
    result: PriceEngineSliceResult,
    *,
    remaining: Callable[[], float | None],
) -> None:
    """Price a small HOT set by provider stage.

    The coalescer is created here and dropped when this slice returns. It is
    not the BACKGROUND slice cache. The next call issues fresh reads.
    """

    coalescer = ExactIdSliceCoalescer()
    with bind_exact_id_coalescer(coalescer):
        await run_staged_exact_id_slice(
            engine,
            rows,
            result,
            remaining=remaining,
            lane=ScanLane.HOT.value,
            exception_policy=RowExceptionPolicy.ISOLATE_WITHOUT_RETRY,
        )
    result.issued_provider_calls = coalescer.issued_provider_calls
    result.coalesced_provider_calls = coalescer.coalesced_provider_calls
    result.provider_stage_calls = dict(coalescer.provider_stage_calls)
    result.pricing_call_shape = HOT_LATENCY_CALL_SHAPE

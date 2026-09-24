"""BACKGROUND provider-centric exact-ID pricing.

UNIVERSE has already catalogued native IDs. This planner refreshes those IDs
for due BACKGROUND rows. It does not discover, rematch, or raise provider caps.

The stage queues live in ``exact_id_stage_scheduler``. BACKGROUND keeps
provider lane ``background`` and turns an unexpected row exception into that
row's own retry. HOT does not call this function. The latency-first consumer
is ``hot_latency_exact_id.run_hot_latency_exact_id_slice``, and production
``run_slice`` does not call it yet. ACTIVE TRADE stays on ``_price_item``.
"""

from __future__ import annotations

from collections.abc import Callable

from sports_hedge.application.exact_id_stage_scheduler import (
    RowExceptionPolicy,
    run_staged_exact_id_slice,
)
from sports_hedge.application.price_engine import (
    PriceEngineRuntimeItem,
    PriceEngineSliceResult,
)
from sports_hedge.application.provider_access import PRICE_ENGINE_BACKGROUND_LANE


async def run_background_exact_id_slice(
    engine: object,
    rows: list[PriceEngineRuntimeItem],
    result: PriceEngineSliceResult,
    *,
    remaining: Callable[[], float | None],
) -> None:
    """Price due BACKGROUND rows by provider stage, isolating row failures."""

    await run_staged_exact_id_slice(
        engine,
        rows,
        result,
        remaining=remaining,
        lane=PRICE_ENGINE_BACKGROUND_LANE,
        exception_policy=RowExceptionPolicy.RETRY,
    )

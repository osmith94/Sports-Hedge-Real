"""One scanner-economic FX context for every sport and lane.

FX is not sport-specific. Football, NFL, and MLB must see the same USD→GBP
snapshot resolved from the shared FX service. Missing or stale rates fail
closed with that service's reason. This module does not invent a rate.
"""

from __future__ import annotations

from datetime import datetime

from sports_hedge.domain.models import FxRateSnapshot
from sports_hedge.fx.models import FxRateUnavailable

SCANNER_FX_CURRENCIES = frozenset({"USD", "GBP"})
_DEMO_FX_SOURCE = "paper_demo_fx_snapshot"


def resolve_scanner_economic_fx(
    fx_service,
    *,
    as_of: datetime,
) -> list[FxRateSnapshot]:
    """Resolve USD and GBP once for a scanning lane.

    ``fx_service`` is the shared ``FxRateService``. A missing service, a stale
    rate, or a demo snapshot raises ``FxRateUnavailable`` so every sport fails
    closed for the same reason.
    """

    if fx_service is None:
        raise FxRateUnavailable(
            "missing_fx_rate:USD",
            "scanner FX service is not configured",
        )
    snapshots = list(fx_service.paper_snapshots(set(SCANNER_FX_CURRENCIES), as_of=as_of))
    if any(item.source == _DEMO_FX_SOURCE for item in snapshots):
        raise FxRateUnavailable(
            "missing_fx_rate:USD",
            "demo FX snapshots are not scanner-economic rates",
        )
    if not any(item.currency.upper() == "USD" for item in snapshots):
        raise FxRateUnavailable(
            "missing_fx_rate:USD",
            "scanner FX context did not include USD",
        )
    return snapshots


def bind_lane_fx(
    *,
    explicit: list[FxRateSnapshot] | None,
    fx_service,
    as_of: datetime,
) -> tuple[list[FxRateSnapshot] | None, str | None]:
    """Prefer an explicit lane snapshot. Otherwise resolve once from the service.

    An explicit list is returned unchanged. When the service cannot produce a
    rate, the reason is the shared fail-closed token and the snapshot list is
    empty. When no service is configured and no explicit list was supplied,
    both values stay empty so GBP-only callers keep their existing behaviour.
    """

    if explicit:
        return list(explicit), None
    if fx_service is None:
        return None, None
    try:
        return resolve_scanner_economic_fx(fx_service, as_of=as_of), None
    except FxRateUnavailable as exc:
        return None, exc.reason or "missing_fx_rate:USD"

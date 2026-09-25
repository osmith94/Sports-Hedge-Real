"""Process-memory viability cache is shared. Isolate it between tests."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from sports_hedge.application.opportunity_viability import reset_opportunity_viability_cache

# Suite kickoffs are fixed dates in September 2026 (12 Sep and 20 Sep are both
# common). Production discovery uses the wall clock and the 4h current-radar
# ceiling. Pinning the test clock to 1 Sep keeps those historical payloads
# inside the horizon without changing production.
_PINNED_POLYMARKET_DISCOVERY_CLOCK = datetime(2026, 9, 1, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_opportunity_viability_cache() -> Iterator[None]:
    reset_opportunity_viability_cache()
    yield
    reset_opportunity_viability_cache()


@pytest.fixture(autouse=True)
def _pin_polymarket_fixture_discovery_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sports_hedge.application.polymarket_fixture_discovery.discovery_clock",
        lambda: _PINNED_POLYMARKET_DISCOVERY_CLOCK,
    )

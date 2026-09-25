"""Process-memory viability cache is shared. Isolate it between tests."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from sports_hedge.application.opportunity_viability import reset_opportunity_viability_cache


@pytest.fixture(autouse=True)
def _reset_opportunity_viability_cache() -> Iterator[None]:
    reset_opportunity_viability_cache()
    yield
    reset_opportunity_viability_cache()

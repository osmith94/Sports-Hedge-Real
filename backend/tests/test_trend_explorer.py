from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import MarketSnapshot
from sports_hedge.market_intelligence.trends import TrendExplorer, TrendMetric, TrendQuery


BASE = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


def make_snapshot(
    event_number: int,
    minute: int,
    probability: str,
    *,
    family: MarketFamily = MarketFamily.MATCH_RESULT,
    venue: VenueName = VenueName.MATCHBOOK,
    team: str = "Newcastle United",
    opponent: str = "Arsenal",
    competition: str = "Premier League",
    spread: str = "0.04",
    liquidity: str = "100",
) -> MarketSnapshot:
    implied = Decimal(probability)
    event_id = f"evt:{event_number}"
    return MarketSnapshot(
        observed_at=BASE + timedelta(days=event_number, minutes=minute),
        venue=venue,
        canonical_event_id=event_id,
        canonical_market_id=f"{event_id}:{family.value}",
        canonical_outcome="home",
        market_family=family,
        competition=competition,
        home_team=team,
        away_team=opponent,
        kickoff_utc=BASE + timedelta(days=event_number, hours=3),
        decimal_odds=Decimal("1") / implied,
        implied_probability=implied,
        spread_decimal=Decimal(spread),
        total_liquidity=Decimal(liquidity),
    )


def series(
    event_number: int,
    probabilities: tuple[str, str, str],
    *,
    family: MarketFamily = MarketFamily.MATCH_RESULT,
    venue: VenueName = VenueName.MATCHBOOK,
    team: str = "Newcastle United",
    opponent: str = "Arsenal",
) -> list[MarketSnapshot]:
    spreads = ("0.04", "0.03", "0.02")
    liquidity = ("100", "150", "200")
    return [
        make_snapshot(
            event_number,
            minute,
            probability,
            family=family,
            venue=venue,
            team=team,
            opponent=opponent,
            spread=spread,
            liquidity=depth,
        )
        for minute, probability, spread, depth in zip(
            (0, 30, 60), probabilities, spreads, liquidity, strict=True
        )
    ]


def test_all_five_trend_metrics_compute_from_market_series() -> None:
    snapshots = series(1, ("0.40", "0.50", "0.45"))
    explorer = TrendExplorer(minimum_sample_size=1)

    open_close = explorer.analyze(TrendMetric.OPEN_TO_CLOSE_PROBABILITY, snapshots)
    volatility = explorer.analyze(TrendMetric.REALIZED_LOGIT_VOLATILITY, snapshots)
    spread = explorer.analyze(TrendMetric.SPREAD_CHANGE, snapshots)
    liquidity = explorer.analyze(TrendMetric.LIQUIDITY_GROWTH, snapshots)
    retracement = explorer.analyze(TrendMetric.PEAK_RETRACEMENT, snapshots)

    assert open_close.observations[0].value == pytest.approx(0.05)
    assert volatility.observations[0].value > 0
    assert spread.observations[0].value == pytest.approx(-0.02)
    assert liquidity.observations[0].value == pytest.approx(1.0)
    assert retracement.observations[0].value == pytest.approx(0.5)


def test_composable_filters_select_team_family_venue_and_start_price_bucket() -> None:
    snapshots = [
        *series(1, ("0.40", "0.44", "0.45")),
        *series(
            2,
            ("0.62", "0.61", "0.60"),
            team="Liverpool",
            opponent="Chelsea",
        ),
        *series(
            3,
            ("0.38", "0.39", "0.41"),
            family=MarketFamily.CORNERS,
        ),
        *series(
            4,
            ("0.41", "0.43", "0.42"),
            venue=VenueName.POLYMARKET,
        ),
    ]
    explorer = TrendExplorer(minimum_sample_size=1)
    query = TrendQuery(
        team="Newcastle United",
        market_family=MarketFamily.MATCH_RESULT,
        venue=VenueName.MATCHBOOK,
        minimum_start_probability=0.35,
        maximum_start_probability=0.45,
    )

    summary = explorer.analyze(
        TrendMetric.OPEN_TO_CLOSE_PROBABILITY,
        snapshots,
        query=query,
    )

    assert summary.sample_size == 1
    assert summary.observations[0].canonical_event_id == "evt:1"
    assert summary.median == pytest.approx(0.05)


def test_summary_exposes_cohort_distribution_and_sample_guardrail() -> None:
    snapshots = [
        *series(1, ("0.40", "0.43", "0.45")),
        *series(2, ("0.40", "0.42", "0.43")),
        *series(3, ("0.40", "0.39", "0.38")),
    ]
    explorer = TrendExplorer(minimum_sample_size=4)

    summary = explorer.analyze(TrendMetric.OPEN_TO_CLOSE_PROBABILITY, snapshots)

    assert summary.sample_size == 3
    assert summary.sufficient_sample is False
    assert summary.median == pytest.approx(0.03)
    assert summary.positive_rate == pytest.approx(2 / 3)
    assert summary.p25 is not None
    assert summary.p75 is not None
    assert summary.stability_iqr == pytest.approx(summary.p75 - summary.p25)


def test_venue_series_are_kept_separate_in_trend_summary() -> None:
    snapshots = [
        *series(1, ("0.40", "0.45", "0.47"), venue=VenueName.MATCHBOOK),
        *series(1, ("0.41", "0.42", "0.43"), venue=VenueName.POLYMARKET),
    ]
    explorer = TrendExplorer(minimum_sample_size=2)

    summary = explorer.analyze(TrendMetric.OPEN_TO_CLOSE_PROBABILITY, snapshots)

    assert summary.sample_size == 2
    assert {item.venue for item in summary.observations} == {
        VenueName.MATCHBOOK,
        VenueName.POLYMARKET,
    }
    assert sorted(item.value for item in summary.observations) == pytest.approx([0.02, 0.07])

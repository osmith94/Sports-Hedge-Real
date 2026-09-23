"""Outrights Phase 1A follow-up: COMPETITION_SEASON UNIVERSE picker eligibility.

PAPER / read-only. Captured public native IDs 2026-09-20. Not owner-live quotes.
No cross-venue outright equivalence or PAPER admission.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sports_hedge.application.approved_market_catalogue import CatalogueRowState
from sports_hedge.application.target_competitions import (
    filter_in_scope_events,
    resolve_target_competition_from_kalshi_ticker,
    scope_kalshi_event,
)
from sports_hedge.domain.market_scope import MarketScope
from sports_hedge.domain.models import VenueName
from sports_hedge.outrights.universe_scopes import (
    FORBIDDEN_NFL_FIXTURE_SERIES,
    TOP_SCORER_NOT_EXECUTABLE_REASON,
    SeasonUniverseScopeCode,
    kalshi_series_tickers_for_season_scopes,
    kalshi_ticker_is_season_series,
    observe_selected_season_kalshi_events,
    operator_season_scope_catalog,
    partition_kalshi_season_events,
)
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

NOW = datetime(2026, 9, 21, 8, 0, tzinfo=UTC)


def _kalshi_event(*, series: str, event: str, ticker: str, title: str, extra: dict | None = None) -> dict:
    market = {"ticker": ticker, "title": title, **(extra or {})}
    return {
        "event_ticker": event,
        "series_ticker": series,
        "title": title,
        "markets": [market],
    }


def test_season_catalog_is_observation_only_and_excludes_nfl_fixtures() -> None:
    rows = {row["code"]: row for row in operator_season_scope_catalog()}
    assert set(rows) == {
        SeasonUniverseScopeCode.EPL_2026_27_CHAMPION.value,
        SeasonUniverseScopeCode.NFL_2026_SUPER_BOWL_CHAMPION.value,
        SeasonUniverseScopeCode.EPL_2026_27_TOP_SCORER.value,
    }
    for row in rows.values():
        assert row["market_scope"] == MarketScope.COMPETITION_SEASON.value
        assert row["paper_executable"] is False
        assert row["selectable"] is True
        assert row["default_selected"] is False
    top = rows[SeasonUniverseScopeCode.EPL_2026_27_TOP_SCORER.value]
    assert top["observation_only"] is True
    assert top["unavailable_reason"] == TOP_SCORER_NOT_EXECUTABLE_REASON
    tickers = kalshi_series_tickers_for_season_scopes(list(rows))
    assert tickers == ["KXPREMIERLEAGUE", "KXSB", "KXEPLLEADER"]
    assert FORBIDDEN_NFL_FIXTURE_SERIES.isdisjoint(tickers)
    assert kalshi_ticker_is_season_series("KXEPLLEADER-27GOAL")
    assert kalshi_ticker_is_season_series("KXPREMIERLEAGUE-27")
    assert kalshi_ticker_is_season_series("KXSB-27")
    assert not kalshi_ticker_is_season_series("KXEPLGAME")
    assert not kalshi_ticker_is_season_series("KXNFLGAME")


def test_season_kalshi_series_never_join_fixture_scope() -> None:
    assert resolve_target_competition_from_kalshi_ticker("KXEPLGAME") is not None
    assert resolve_target_competition_from_kalshi_ticker("KXEPLLEADER") is None
    assert resolve_target_competition_from_kalshi_ticker("KXPREMIERLEAGUE") is None
    assert resolve_target_competition_from_kalshi_ticker("KXSB") is None
    leader = scope_kalshi_event(
        {"series_ticker": "KXEPLLEADER", "title": "EPL top scorer"},
        selected_codes=["premier_league"],
    )
    assert leader.allowed is False
    fixture, season = partition_kalshi_season_events(
        [
            _kalshi_event(
                series="KXEPLGAME",
                event="KXEPLGAME-26SEP21ARSCFC",
                ticker="KXEPLGAME-26SEP21ARSCFC",
                title="Arsenal vs Chelsea",
            ),
            _kalshi_event(
                series="KXPREMIERLEAGUE",
                event="KXPREMIERLEAGUE-27",
                ticker="KXPREMIERLEAGUE-27-ARS",
                title="Arsenal",
            ),
            _kalshi_event(
                series="KXEPLLEADER",
                event="KXEPLLEADER-27GOAL",
                ticker="KXEPLLEADER-27GOAL-EHAALA9",
                title="Erling Haaland",
                extra={"soccer_player_uuid": "561342a7-d5fb-4877-9a70-59d20db9caa3"},
            ),
            _kalshi_event(
                series="KXSB",
                event="KXSB-27",
                ticker="KXSB-27-LAR",
                title="Los Angeles Rams",
            ),
        ],
        selected_season_scope_codes=["epl_2026_27_champion"],
    )
    assert [item["series_ticker"] for item in fixture] == ["KXEPLGAME"]
    assert [item["series_ticker"] for item in season] == ["KXPREMIERLEAGUE"]
    scoped = filter_in_scope_events(
        fixture, venue=VenueName.KALSHI, selected_codes=["premier_league"]
    )
    assert len(scoped.allowed) == 1


def test_deselecting_season_scope_keeps_catalogue_and_does_not_admit_paper() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    observed = observe_selected_season_kalshi_events(
        store,
        [
            _kalshi_event(
                series="KXPREMIERLEAGUE",
                event="KXPREMIERLEAGUE-27",
                ticker="KXPREMIERLEAGUE-27-ARS",
                title="Arsenal",
            ),
            _kalshi_event(
                series="KXEPLLEADER",
                event="KXEPLLEADER-27GOAL",
                ticker="KXEPLLEADER-27GOAL-EHAALA9",
                title="Erling Haaland",
                extra={"soccer_player_uuid": "561342a7-d5fb-4877-9a70-59d20db9caa3"},
            ),
        ],
        now=NOW,
        generation_id="gen-1",
    )
    assert observed
    assert all(row.market_scope is MarketScope.COMPETITION_SEASON for row in observed)
    assert all(row.row_state is CatalogueRowState.ACTIVE for row in observed)
    assert all(row.home_canonical is None and row.away_canonical is None for row in observed)
    ids = {row.catalogue_row_id for row in observed}
    fixture, season = partition_kalshi_season_events(
        [
            _kalshi_event(
                series="KXPREMIERLEAGUE",
                event="KXPREMIERLEAGUE-27",
                ticker="KXPREMIERLEAGUE-27-ARS",
                title="Arsenal",
            )
        ],
        selected_season_scope_codes=[],
    )
    assert fixture == []
    assert season == []
    kept = [row for row in store.list_active() if row.catalogue_row_id in ids]
    assert {row.catalogue_row_id for row in kept} == ids
    assert all(row.row_state is CatalogueRowState.ACTIVE for row in kept)
    top = [row for row in observed if row.family == "top_scorer"]
    assert top
    from sports_hedge.application.approved_market_catalogue import (
        catalogue_row_supports_paper_eligibility,
    )

    assert all(catalogue_row_supports_paper_eligibility(row, None) is False for row in observed)
    store.close()

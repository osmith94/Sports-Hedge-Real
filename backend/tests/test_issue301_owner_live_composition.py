"""Issue #301: combined-path proof for exact #300 + #299 + #298 composition.

One deterministic fixture carries the three architect-accepted repair seams:

- PR #300 / #293: production EventMatcher overlap + attempt-live capture/replay
  preferring approved BTTS over GAMEWIN-blocked 1X2
- PR #299 / #295: Kalshi ``get_order_book`` only for approved catalogue pair legs
- PR #298 / #296: Matchbook exotic/compound markets fail closed before uniqueness
  validation errors

Fixture/demo doubles. Not owner-live quotes, not historical books, not modelled
probabilities. PAPER / read-only. Polymarket off. Execution disabled.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sports_hedge.application.capture_replay import (
    DATA_CLASS_LIVE_CAPTURE,
    FORBIDDEN_WRITE_METHODS,
    ReplayBundle,
    assert_no_write_methods,
    attempt_live_read_only_capture,
    catalogue_relevant_kalshi_series,
    cluster_live_matchbook_kalshi_events,
    replay_bundle,
)
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_cycle_audit import (
    cycle_last_error,
    issue_is_provider_failure,
    issue_is_unsupported_market_skip,
)
from sports_hedge.catalogue.admission import catalogue_allows_solver
from sports_hedge.catalogue.classify import PayloadSide, classify_payload_pair, normalize_payload_side
from sports_hedge.catalogue.corpus import GAMEWIN_TEMPLATE, REGULATION
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.venues import (
    MATCHBOOK_COMPOUND_FAMILY_REASON,
    MATCHBOOK_NON_UNIQUE_CANONICAL_REASON,
)
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from venue_cost_helpers import matchbook_polymarket_costs

KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
HOME = "West Ham"
AWAY = "Chelsea"
COMPETITION = "Premier League"
MB_EVENT_ID = 301001
BTTS_TICKER = "KXEPLBTTS-26SEP20WHUCHE-BTTS"
UNAPPROVED_GAME_TICKERS = (
    "KXEPLGAME-26SEP20WHUCHE-WHU",
    "KXEPLGAME-26SEP20WHUCHE-DRAW",
    "KXEPLGAME-26SEP20WHUCHE-CHE",
)
BTTS_SERIES = {
    "ticker": "KXEPLBTTS",
    "title": "Premier League Both Teams To Score",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
}
GAME_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
}


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx", captured_at=NOW),
        FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency", captured_at=NOW),
    ]


def _costs() -> list[Any]:
    return [
        *matchbook_polymarket_costs(captured_at=NOW),
        kalshi_cost_from_series(BTTS_SERIES, captured_at=NOW),
        kalshi_cost_from_series(GAME_SERIES, captured_at=NOW),
    ]


def _back(odds: str, amount: str = "80") -> dict[str, str]:
    return {"side": "back", "odds": odds, "available-amount": amount}


def _runner(runner_id: int, name: str, odds: str = "2.10") -> dict[str, Any]:
    return {"id": runner_id, "name": name, "prices": [_back(odds)]}


def _mb_event() -> dict[str, Any]:
    return {
        "id": MB_EVENT_ID,
        "name": f"{HOME} vs {AWAY}",
        "start": KICKOFF.isoformat(),
        "competition-name": COMPETITION,
    }


def _mb_btts() -> dict[str, Any]:
    return {
        "id": 301020,
        "name": "Both Teams To Score",
        "runners": [
            _runner(11, "Yes", "1.90"),
            _runner(12, "No", "1.95"),
        ],
    }


def _mb_match_odds() -> dict[str, Any]:
    return {
        "id": 301010,
        "name": "Match Odds",
        "runners": [
            _runner(1, HOME, "2.40"),
            _runner(2, "Draw", "3.40"),
            _runner(3, AWAY, "2.90"),
        ],
    }


def _mb_exotics() -> list[dict[str, Any]]:
    """Owner-live-shaped compound/exotic markets that previously collapsed outcomes."""

    return [
        {
            "id": 301030,
            "name": "Correct Score",
            "market-type": "correct_score",
            "runners": [
                _runner(21, "ANY OTHER DRAW"),
                _runner(22, "1-0"),
                _runner(23, "2-1"),
            ],
        },
        {
            "id": 301031,
            "name": "Half Time/Full Time",
            "market-type": "ht_ft",
            "runners": [
                _runner(31, "WEST HAM/WEST HAM"),
                _runner(32, "WEST HAM/CHELSEA"),
                _runner(33, "CHELSEA/CHELSEA"),
            ],
        },
        {
            "id": 301032,
            "name": "Result and Both Teams To Score",
            "market-type": "other",
            "runners": [
                _runner(41, "West Ham and Yes"),
                _runner(42, "West Ham and No"),
                _runner(43, "Draw and Yes"),
                _runner(44, "Chelsea and No"),
            ],
        },
        {
            "id": 301033,
            "name": "Match Odds and Over/Under 3.5 Goals",
            "market-type": "other",
            "runners": [
                _runner(51, "West Ham and Under 3.5"),
                _runner(52, "West Ham and Over 3.5"),
                _runner(53, "Draw and Under 3.5"),
            ],
        },
        {
            "id": 301034,
            "name": "Double Chance",
            "market-type": "double_chance",
            "runners": [
                _runner(61, "Draw or West Ham"),
                _runner(62, "West Ham or Chelsea"),
                _runner(63, "Draw or Chelsea"),
            ],
        },
    ]


def _mb_markets() -> list[dict[str, Any]]:
    return [_mb_match_odds(), _mb_btts(), *_mb_exotics()]


def _kalshi_game_event() -> dict[str, Any]:
    ticker = "KXEPLGAME-26SEP20WHUCHE"
    title = f"{HOME} vs {AWAY}"
    return {
        "event_ticker": ticker,
        "series_ticker": "KXEPLGAME",
        "title": title,
        "category": "Sports",
        "strike_date": KICKOFF.isoformat(),
        "milestone": {"start_date": KICKOFF.isoformat()},
        "product_metadata": {"competition": COMPETITION, "competition_scope": "Game"},
        "markets": [
            {
                "ticker": UNAPPROVED_GAME_TICKERS[0],
                "event_ticker": ticker,
                "title": title,
                "yes_sub_title": HOME,
                "rules_primary": GAMEWIN_TEMPLATE,
            },
            {
                "ticker": UNAPPROVED_GAME_TICKERS[1],
                "event_ticker": ticker,
                "title": title,
                "yes_sub_title": "Draw",
                "rules_primary": GAMEWIN_TEMPLATE,
            },
            {
                "ticker": UNAPPROVED_GAME_TICKERS[2],
                "event_ticker": ticker,
                "title": title,
                "yes_sub_title": AWAY,
                "rules_primary": GAMEWIN_TEMPLATE,
            },
        ],
    }


def _kalshi_btts_event() -> dict[str, Any]:
    ticker = "KXEPLBTTS-26SEP20WHUCHE"
    return {
        "event_ticker": ticker,
        "series_ticker": "KXEPLBTTS",
        "title": f"{HOME} vs {AWAY}",
        "category": "Sports",
        "strike_date": KICKOFF.isoformat(),
        "milestone": {"start_date": KICKOFF.isoformat()},
        "product_metadata": {"competition": COMPETITION, "competition_scope": "Game"},
        "markets": [
            {
                "ticker": BTTS_TICKER,
                "event_ticker": ticker,
                "title": "Both Teams To Score",
                "yes_sub_title": "Yes",
                "rules_primary": REGULATION,
            }
        ],
    }


def _btts_book() -> dict[str, Any]:
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.40", "100.00"]],
            "no_dollars": [["0.49", "200.00"]],
        }
    }


class _DisabledPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []


class _RefusingGameKalshi(OverlapKalshi):
    """Kalshi double that records books and refuses unapproved GAME tickers."""

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        ticker = str(market_id)
        self.book_calls.append(ticker)
        if ticker in UNAPPROVED_GAME_TICKERS or "GAME" in ticker:
            raise AssertionError(f"unapproved Kalshi GAME ticker {ticker} must not be fetched")
        if ticker != BTTS_TICKER:
            raise AssertionError(f"unexpected Kalshi book ticker {ticker}")
        book = self.books.get(ticker)
        if book is None:
            raise LookupError(ticker)
        return deepcopy(book)


def _kalshi() -> _RefusingGameKalshi:
    return _RefusingGameKalshi(
        [_kalshi_game_event(), _kalshi_btts_event()],
        series=BTTS_SERIES,
        series_by_ticker={
            "KXEPLBTTS": dict(BTTS_SERIES),
            "KXEPLGAME": dict(GAME_SERIES),
        },
        books={BTTS_TICKER: _btts_book()},
    )


def _matchbook() -> OverlapMatchbook:
    return OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets()})


async def _collect(matchbook: OverlapMatchbook, kalshi: _RefusingGameKalshi):
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=_DisabledPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        provider_call_timeout_seconds=8.0,
        provider_concurrency={
            VenueName.MATCHBOOK: 1,
            VenueName.POLYMARKET: 1,
            VenueName.KALSHI: 1,
        },
    )
    try:
        return await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        )
    finally:
        repository.close()


def _matched_fixtures(report: Any) -> list[Any]:
    return [
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    ]


def test_paper_and_execution_boundaries_hold() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert_no_write_methods()
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method), f"{client.__name__}.{method} must not exist"


def test_catalogue_relevant_series_still_includes_approved_families() -> None:
    series = catalogue_relevant_kalshi_series()
    assert "KXEPLGAME" in series
    assert "KXEPLBTTS" in series
    assert "KXEPLTOTAL" in series
    assert "KXEPLFTTS" in series


def test_approved_btts_is_catalogue_equivalent_and_game_is_not() -> None:
    mb_event = _mb_event()
    btts_assessment = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_btts()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_btts_event(),
            markets=list(_kalshi_btts_event()["markets"]),
            series=BTTS_SERIES,
        ),
    )
    assert btts_assessment.state is CatalogueApprovalState.APPROVED_EQUIVALENT
    mb = normalize_payload_side(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_btts()])
    )
    kalshi = normalize_payload_side(
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_btts_event(),
            markets=list(_kalshi_btts_event()["markets"]),
            series=BTTS_SERIES,
        )
    )
    match = MarketMatcher().match(mb, kalshi)
    assert catalogue_allows_solver(mb, kalshi) is True
    assert scan_eligible_pair(mb, kalshi, match) is True

    game_assessment = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_game_event(),
            markets=list(_kalshi_game_event()["markets"]),
            series=GAME_SERIES,
        ),
    )
    assert game_assessment.state is not CatalogueApprovalState.APPROVED_EQUIVALENT


@pytest.mark.asyncio
async def test_combined_owner_live_seams_on_one_fixture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One West Ham vs Chelsea fixture proves the three composed repairs together."""

    original_match = EventMatcher.match
    matcher_calls = {"n": 0}

    def _counting_match(self: EventMatcher, left: Any, right: Any, **kwargs: Any) -> Any:
        matcher_calls["n"] += 1
        return original_match(self, left, right, **kwargs)

    monkeypatch.setattr(EventMatcher, "match", _counting_match)

    clusters = cluster_live_matchbook_kalshi_events(
        [_mb_event()],
        [_kalshi_game_event(), _kalshi_btts_event()],
    )
    overlaps = [
        cluster
        for cluster in clusters
        if cluster.matchbook is not None and cluster.kalshi is not None
    ]
    assert len(overlaps) == 1
    assert len(overlaps[0].kalshi_events) == 2
    assert matcher_calls["n"] > 0

    matchbook = _matchbook()
    kalshi = _kalshi()
    report = await _collect(matchbook, kalshi)

    # 1. Production identity resolves one fixture.
    matched = _matched_fixtures(report)
    assert len(matched) == 1
    fixture = matched[0]
    assert HOME in (fixture.home_team or "")
    assert AWAY in (fixture.away_team or "")

    # 3/4. Kalshi depth only for approved BTTS; GAME tickers never called.
    assert kalshi.book_calls == [BTTS_TICKER]
    assert all(ticker not in kalshi.book_calls for ticker in UNAPPROVED_GAME_TICKERS)
    policy = report.scan_diagnostics["kalshi_order_book_policy"]
    assert policy["eligible_markets"] == 1
    assert policy["skipped_unapproved"] >= 1

    rows = report.fixture_markets[fixture.canonical_event_id]

    # 2. Approved BTTS becomes MATCHED_EQUIVALENT.
    btts = [row for row in rows if row.family == "both_teams_to_score"]
    assert btts
    assert any(row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT for row in btts)

    # 6/7. Approved BTTS enters the solver and economics are computed.
    solver_btts = [
        row
        for row in btts
        if row.entered_solver and row.current_net_edge is not None
    ]
    assert solver_btts
    assert report.paper_decisions
    # Arb or non-arb are both acceptable; the composed path must still produce a number.
    assert all(isinstance(row.current_net_edge, Decimal) for row in solver_btts)

    game_inventory = [
        row
        for row in rows
        if row.kalshi is not None
        and (
            "GAME" in row.kalshi.source_event_id
            or any(ticker in row.kalshi.source_market_id for ticker in UNAPPROVED_GAME_TICKERS)
        )
    ]
    assert game_inventory
    assert all(not row.entered_solver for row in game_inventory)
    assert all(row.kalshi is None or not row.kalshi.best_backs for row in game_inventory)

    # 5. Matchbook exotics fail closed without uniqueness validation errors.
    details = [str(issue.detail) for issue in report.issues]
    assert all("outcome_books must contain unique canonical outcomes" not in detail for detail in details)
    unsupported = [issue for issue in report.issues if issue_is_unsupported_market_skip(issue)]
    assert unsupported
    assert all(not issue_is_provider_failure(issue) for issue in unsupported)
    assert cycle_last_error(report) is None
    reasons = " ".join(str(issue.detail) for issue in unsupported)
    assert MATCHBOOK_NON_UNIQUE_CANONICAL_REASON in reasons
    assert MATCHBOOK_COMPOUND_FAMILY_REASON in reasons
    assert "Correct Score" in reasons
    assert "Result and Both Teams To Score" in reasons
    quoted_ids = {
        row.matchbook.source_market_id
        for row in rows
        if row.matchbook and row.matchbook.best_backs
    }
    assert "301020" in quoted_ids
    assert "301010" in quoted_ids
    assert "301030" not in quoted_ids
    assert "301031" not in quoted_ids
    assert "301032" not in quoted_ids
    assert "301033" not in quoted_ids
    assert "301034" not in quoted_ids

    # 9/10. PAPER/read-only, execution disabled, no venue writes.
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert_no_write_methods()
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)

    # 8. Replay bundle contains the approved BTTS path and replays offline.
    capture_matchbook = _matchbook()
    capture_kalshi = _kalshi()
    bundle_path = tmp_path / "issue301-combined.replay-bundle.json"
    capture = await attempt_live_read_only_capture(
        matchbook_client=capture_matchbook,
        kalshi_client=capture_kalshi,
        fx_snapshots=_fx(),
        venue_costs=_costs(),
        bundle_out=bundle_path,
    )
    requested = (capture_kalshi.list_events_filters[0].get("series_tickers") or []) if capture_kalshi.list_events_filters else []
    assert "KXEPLBTTS" in requested
    assert "KXEPLGAME" in requested
    assert set(requested) != {"KXEPLGAME", "KXBUNDESLIGAGAME", "KXSERIEAGAME", "KXLALIGAGAME"}
    assert capture.same_event_overlap_found is True
    assert capture.paper_mode == "paper"
    assert capture.execution_enabled is False
    assert capture.polymarket_included is False
    assert HOME in (capture.overlap_fixture or "")
    assert AWAY in (capture.overlap_fixture or "")
    assert capture.matched_equivalent is True
    assert capture.approved_family_on_both is True
    assert capture.canonical_market_key is not None
    assert "both_teams_to_score" in capture.canonical_market_key
    assert capture.comparison_economics_computed is True
    assert capture.replay_bundle_path == str(bundle_path)
    assert capture_kalshi.book_calls == [BTTS_TICKER]
    assert all(ticker not in capture_kalshi.book_calls for ticker in UNAPPROVED_GAME_TICKERS)
    saved = ReplayBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    assert saved.data_class == DATA_CLASS_LIVE_CAPTURE
    assert saved.paper_mode == "paper"
    assert saved.execution_enabled is False
    assert saved.kalshi.event is not None
    assert saved.kalshi.event.get("series_ticker") == "KXEPLBTTS"
    assert any("BTTS" in str(market.get("ticker") or "") for market in saved.kalshi.markets)
    assert BTTS_TICKER in saved.kalshi.order_books
    assert all(ticker not in saved.kalshi.order_books for ticker in UNAPPROVED_GAME_TICKERS)
    dumped = bundle_path.read_text(encoding="utf-8").casefold()
    assert "password" not in dumped
    assert "authorization" not in dumped
    replay_report, summary = await replay_bundle(saved)
    assert summary.matched_equivalent is True
    assert summary.entered_solver is True
    assert summary.comparison_economics_computed is True
    assert summary.network_used is False
    assert summary.catalogue_admission_allowed is True
    replay_matched = _matched_fixtures(replay_report)
    assert len(replay_matched) == 1
    replay_rows = replay_report.fixture_markets[replay_matched[0].canonical_event_id]
    assert any(
        row.family == "both_teams_to_score"
        and row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
        and row.entered_solver
        and row.current_net_edge is not None
        for row in replay_rows
    )
    replay_details = [str(issue.detail) for issue in replay_report.issues]
    assert all(
        "outcome_books must contain unique canonical outcomes" not in detail
        for detail in replay_details
    )

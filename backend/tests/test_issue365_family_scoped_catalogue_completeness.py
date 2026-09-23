"""Issue #365 Wave 2: family-scoped catalogue completeness.

UNIVERSE discovery → catalogue-maintenance boundary only.
GAME/BTTS success must not imply TOTAL/FTTS completeness.
Deterministic fixture/demo providers. Not owner-live quotes.
PAPER / read-only. No live venue execution.
"""

from __future__ import annotations

import inspect
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest

from sports_hedge.application import live_refresh as live_refresh_module
from sports_hedge.application import price_engine as price_engine_module
from sports_hedge.application import scan_lanes as scan_lanes_module
from sports_hedge.application.approved_market_catalogue import CatalogueRowState
from sports_hedge.application.capture_replay import DEFAULT_KALSHI_BOOK, FORBIDDEN_WRITE_METHODS
from sports_hedge.application.catalogue_maintenance import (
    DISAPPEARED_FAMILY_REASON,
    FamilyDiscoveryCompleteness,
    catalogue_family_key,
    complete_family_keys,
    family_key_from_kalshi_series,
    persist_universe_catalogue_pass,
    persist_universe_catalogue_pass_offloop,
)
from sports_hedge.application.collector import (
    DiscoveredFixture,
    ReadOnlyCrossVenueCollector,
    _NormalizedEvent,
    _VenueSideFetch,
    _kalshi_family_key_from_event,
    _mark_unprocessed_kalshi_families_incomplete,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.catalogue.corpus import REGULATION
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.approved_register import (
    CANONICAL_BTTS_FT,
    CANONICAL_FTTS_FT,
    CANONICAL_MATCH_RESULT_FT,
    CANONICAL_TOTAL_GOALS_FT,
)
from sports_hedge.normalization.venues import MatchbookNormalizer
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from test_issue316_catalogue_registry import (
    KICKOFF,
    MB_EVENT_ID,
    _DisabledPolymarket,
    _costs,
    _fx,
    _kalshi_btts_event,
    _kalshi_event,
    _kalshi_ftts_event,
    _kalshi_game_event,
    _mb_btts,
    _mb_event,
    _mb_ftts,
    _mb_match_odds,
    _runner,
)
from test_issue341_approved_market_catalogue import NOW, _collect_catalogue, _series_map

TOTAL_LINES = ("2.5", "3.5", "4.5")
TOTAL_KEYS = {f"{CANONICAL_TOTAL_GOALS_FT}:{line}" for line in TOTAL_LINES}
_TOTAL_MB_IDS = {"2.5": 316030, "3.5": 316033, "4.5": 316034}
_TOTAL_MB_RUNNERS = {"2.5": 21, "3.5": 41, "4.5": 61}
HOME = "Brentford"
AWAY = "Chelsea"


def _mb_total_line(line: str) -> dict[str, Any]:
    runner_base = _TOTAL_MB_RUNNERS[line]
    return {
        "id": _TOTAL_MB_IDS[line],
        "name": f"Over/Under {line} Goals",
        "line": line,
        "runners": [
            _runner(runner_base, f"Over {line}", "1.85"),
            _runner(runner_base + 1, f"Under {line}", "2.05"),
        ],
    }


def _mb_markets_with_totals(lines: tuple[str, ...] = TOTAL_LINES) -> list[dict[str, Any]]:
    return [_mb_match_odds(), _mb_btts(), *[_mb_total_line(line) for line in lines], _mb_ftts()]


def _kalshi_totals_event(lines: tuple[str, ...]) -> dict[str, Any]:
    ticker = "KXEPLTOTAL-26SEP20BRECHE"
    markets = [
        {
            "ticker": f"KXEPLTOTAL-26SEP20BRECHE-{line}",
            "event_ticker": ticker,
            "title": f"{HOME} vs {AWAY} Total Goals {line}",
            "yes_sub_title": f"Over {line}",
            "rules_primary": REGULATION,
            "strike": line,
        }
        for line in lines
    ]
    return _kalshi_event(ticker, "KXEPLTOTAL", markets)


def _kalshi_family_events(total_lines: tuple[str, ...] = TOTAL_LINES) -> list[dict[str, Any]]:
    events = [
        _kalshi_game_event(rules=REGULATION),
        _kalshi_btts_event(),
        _kalshi_ftts_event(),
    ]
    if total_lines:
        events.insert(2, _kalshi_totals_event(total_lines))
    return events


def _books_for(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    books: dict[str, dict[str, Any]] = {}
    for event in events:
        for market in event.get("markets") or []:
            if isinstance(market, dict) and market.get("ticker"):
                books[str(market["ticker"])] = deepcopy(DEFAULT_KALSHI_BOOK)
    return books


class _SeriesFilteredKalshi(OverlapKalshi):
    """Per-series list_events with optional family timeout. Fixture/demo only."""

    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        timeout_series: set[str] | None = None,
        series_by_ticker: dict[str, dict[str, Any]] | None = None,
        books: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        super().__init__(events, series_by_ticker=series_by_ticker, books=books)
        self.timeout_series = {str(item).strip() for item in (timeout_series or set())}
        self.settings = Settings()

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        ticker = str(filters.get("series_ticker") or "").strip()
        if ticker and ticker in self.timeout_series:
            raise TimeoutError("discovery_timeout")
        payload = await super().list_events(**filters)
        events = list(payload.get("events") or [])
        if ticker:
            events = [
                item
                for item in events
                if str(item.get("series_ticker") or "").strip() == ticker
            ]
        return {**payload, "events": events}


_FAMILY_FETCH_ORDER = {
    CANONICAL_MATCH_RESULT_FT: 0,
    CANONICAL_BTTS_FT: 1,
    CANONICAL_TOTAL_GOALS_FT: 2,
    CANONICAL_FTTS_FT: 3,
}


class _PartialFamilyBudgetCollector(ReadOnlyCrossVenueCollector):
    """Fixture/demo only: exhaust budget mid-fixture without starving the other venue."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.exhaust_kalshi_after: int | None = None
        self.exhaust_matchbook_after: int | None = None
        self.kalshi_loads = 0
        self._in_kalshi_fetch = False
        self._in_matchbook_fetch = False

    def _provider_budget_exhausted(self) -> bool:
        if self._in_kalshi_fetch and self.exhaust_kalshi_after is not None:
            return self.kalshi_loads >= self.exhaust_kalshi_after
        if self._in_matchbook_fetch and self.exhaust_matchbook_after is not None:
            listed = getattr(self.matchbook, "list_markets_calls", [])
            return len(listed) >= self.exhaust_matchbook_after
        return super()._provider_budget_exhausted()

    async def _fetch_matchbook_cluster_side(self, mb_events, **kwargs):  # type: ignore[no-untyped-def]
        self._in_matchbook_fetch = True
        try:
            return await super()._fetch_matchbook_cluster_side(mb_events, **kwargs)
        finally:
            self._in_matchbook_fetch = False

    async def _fetch_kalshi_cluster_side(self, k_events, **kwargs):  # type: ignore[no-untyped-def]
        ordered = sorted(
            k_events,
            key=lambda event: _FAMILY_FETCH_ORDER.get(
                _kalshi_family_key_from_event(event) or "", 99
            ),
        )
        self._in_kalshi_fetch = True
        self.kalshi_loads = 0
        try:
            return await super()._fetch_kalshi_cluster_side(ordered, **kwargs)
        finally:
            self._in_kalshi_fetch = False

    async def _load_kalshi_markets(self, k_event, *, issues):  # type: ignore[no-untyped-def]
        result = await super()._load_kalshi_markets(k_event, issues=issues)
        if self._in_kalshi_fetch:
            self.kalshi_loads += 1
        return result


async def _collect_with(
    matchbook: OverlapMatchbook,
    kalshi: OverlapKalshi,
    store: SqliteApprovedMarketCatalogueStore,
    *,
    collector_cls: type[ReadOnlyCrossVenueCollector] = ReadOnlyCrossVenueCollector,
    configure: Any | None = None,
) -> tuple[Any, ReadOnlyCrossVenueCollector]:
    repository = SqliteMarketIntelligenceRepository()
    collector = collector_cls(
        matchbook=matchbook,
        polymarket=_DisabledPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        catalogue_store=store,
    )
    if configure is not None:
        configure(collector)
    try:
        report = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            unbounded_cycle=True,
        )
        return report, collector
    finally:
        repository.close()


def _rows_by_key(store: SqliteApprovedMarketCatalogueStore, fixture_id: str) -> dict[str, Any]:
    return {row.register_canonical_key: row for row in store.list_rows_for_event(fixture_id)}


def test_paper_boundary_and_exact_line_family_keys() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert catalogue_family_key("TOTAL_GOALS_FT:2.5") == CANONICAL_TOTAL_GOALS_FT
    assert catalogue_family_key("TOTAL_GOALS_FT:3.5") == CANONICAL_TOTAL_GOALS_FT
    assert catalogue_family_key("TOTAL_GOALS_FT:2.5") == catalogue_family_key("TOTAL_GOALS_FT:4.5")
    assert catalogue_family_key(CANONICAL_MATCH_RESULT_FT) == CANONICAL_MATCH_RESULT_FT
    assert family_key_from_kalshi_series("KXEPLTOTAL") == CANONICAL_TOTAL_GOALS_FT
    assert family_key_from_kalshi_series("KXEPLTOTAL-26SEP20BRECHE") == CANONICAL_TOTAL_GOALS_FT
    assert family_key_from_kalshi_series("KXEFLCHAMPIONSHIPGAME") == CANONICAL_MATCH_RESULT_FT
    assert family_key_from_kalshi_series("KXEPLFTTS") == CANONICAL_FTTS_FT


def test_game_btts_success_does_not_complete_total_or_ftts() -> None:
    timeout_evidence = FamilyDiscoveryCompleteness(
        matchbook_listing_complete=True,
        kalshi_series_results=(
            {"series": "KXEPLGAME", "status": "ok"},
            {"series": "KXEPLBTTS", "status": "ok"},
            {"series": "KXEPLTOTAL", "status": "discovery_timeout"},
            {"series": "KXEPLFTTS", "status": "ok"},
        ),
        target_competition_code="premier_league",
    )
    complete = complete_family_keys(timeout_evidence)
    assert CANONICAL_MATCH_RESULT_FT in complete
    assert CANONICAL_BTTS_FT in complete
    assert CANONICAL_FTTS_FT in complete
    assert CANONICAL_TOTAL_GOALS_FT not in complete

    deferred_evidence = FamilyDiscoveryCompleteness(
        matchbook_listing_complete=True,
        kalshi_series_results=(
            {"series": "KXEPLGAME", "status": "ok"},
            {"series": "KXEPLBTTS", "status": "ok"},
            {"series": "KXEPLTOTAL", "status": "deferred"},
        ),
        target_competition_code="premier_league",
    )
    deferred = complete_family_keys(deferred_evidence)
    assert CANONICAL_TOTAL_GOALS_FT not in deferred
    assert CANONICAL_FTTS_FT not in deferred

    championship = FamilyDiscoveryCompleteness(
        matchbook_listing_complete=True,
        kalshi_series_results=(
            {"series": "KXEFLCHAMPIONSHIPGAME", "status": "ok"},
            {"series": "KXEFLCHAMPIONSHIPBTTS", "status": "ok"},
            {"series": "KXEFLCHAMPIONSHIPTOTAL", "status": "ok"},
        ),
        target_competition_code="championship",
    )
    championship_complete = complete_family_keys(championship)
    assert CANONICAL_MATCH_RESULT_FT in championship_complete
    assert CANONICAL_TOTAL_GOALS_FT in championship_complete
    assert CANONICAL_FTTS_FT not in championship_complete

    other_league = FamilyDiscoveryCompleteness(
        matchbook_listing_complete=True,
        kalshi_series_results=({"series": "KXLALIGATOTAL", "status": "ok"},),
        target_competition_code="premier_league",
    )
    assert complete_family_keys(other_league) == frozenset()

    matchbook_failed = FamilyDiscoveryCompleteness(
        matchbook_listing_complete=False,
        kalshi_series_results=({"series": "KXEPLTOTAL", "status": "ok"},),
        target_competition_code="premier_league",
    )
    assert complete_family_keys(matchbook_failed) == frozenset()

    budget_remainder = FamilyDiscoveryCompleteness(
        matchbook_listing_complete=True,
        kalshi_series_results=(
            {"series": "KXEPLGAME", "status": "ok"},
            {"series": "KXEPLBTTS", "status": "ok"},
            {"series": "KXEPLTOTAL", "status": "ok"},
            {"series": "KXEPLFTTS", "status": "ok"},
        ),
        kalshi_incomplete_family_keys=frozenset({CANONICAL_TOTAL_GOALS_FT, CANONICAL_FTTS_FT}),
        target_competition_code="premier_league",
    )
    budget_complete = complete_family_keys(budget_remainder)
    assert CANONICAL_MATCH_RESULT_FT in budget_complete
    assert CANONICAL_BTTS_FT in budget_complete
    assert CANONICAL_TOTAL_GOALS_FT not in budget_complete
    assert CANONICAL_FTTS_FT not in budget_complete


def test_budget_break_marks_current_and_remaining_kalshi_families_incomplete() -> None:
    side = _VenueSideFetch()
    remaining = [
        _NormalizedEvent({"series_ticker": "KXEPLBTTS", "event_ticker": "KXEPLBTTS-1"}, object()),
        _NormalizedEvent({"series_ticker": "KXEPLTOTAL", "event_ticker": "KXEPLTOTAL-1"}, object()),
        _NormalizedEvent({"series_ticker": "KXEPLFTTS", "event_ticker": "KXEPLFTTS-1"}, object()),
    ]
    _mark_unprocessed_kalshi_families_incomplete(side, remaining)
    assert side.kalshi_incomplete_family_keys == {
        CANONICAL_BTTS_FT,
        CANONICAL_TOTAL_GOALS_FT,
        CANONICAL_FTTS_FT,
    }
    assert _kalshi_family_key_from_event(remaining[1]) == CANONICAL_TOTAL_GOALS_FT


@pytest.mark.asyncio
async def test_total_timeout_preserves_existing_exact_lines_as_active() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets_with_totals()})
    events = _kalshi_family_events()
    kalshi = _SeriesFilteredKalshi(
        events, series_by_ticker=_series_map(), books=_books_for(events)
    )
    try:
        first = await _collect_catalogue(matchbook, kalshi, store)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        before = _rows_by_key(store, fixture_id)
        assert TOTAL_KEYS <= set(before)
        assert all(before[key].row_state is CatalogueRowState.ACTIVE for key in TOTAL_KEYS)
        confirmed_at = {key: before[key].last_confirmed_at for key in TOTAL_KEYS}

        kalshi.timeout_series = {"KXEPLTOTAL"}
        await _collect_catalogue(matchbook, kalshi, store)
        after = _rows_by_key(store, fixture_id)
        for key in TOTAL_KEYS:
            row = after[key]
            assert row.row_state is CatalogueRowState.ACTIVE
            assert row.invalidation_reason is None
            assert row.last_confirmed_at == confirmed_at[key]
        assert after[CANONICAL_MATCH_RESULT_FT].row_state is CatalogueRowState.ACTIVE
        assert after[CANONICAL_BTTS_FT].row_state is CatalogueRowState.ACTIVE
        assert {row.register_canonical_key for row in store.list_active()} >= TOTAL_KEYS
    finally:
        store.close()


@pytest.mark.asyncio
async def test_total_complete_absent_line_disappears_siblings_remain() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets_with_totals()})
    events = _kalshi_family_events()
    kalshi = _SeriesFilteredKalshi(
        events, series_by_ticker=_series_map(), books=_books_for(events)
    )
    try:
        first = await _collect_catalogue(matchbook, kalshi, store)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        before = _rows_by_key(store, fixture_id)
        assert {before[key].catalogue_row_id for key in TOTAL_KEYS} == {
            before["TOTAL_GOALS_FT:2.5"].catalogue_row_id,
            before["TOTAL_GOALS_FT:3.5"].catalogue_row_id,
            before["TOTAL_GOALS_FT:4.5"].catalogue_row_id,
        }
        assert before["TOTAL_GOALS_FT:2.5"].catalogue_row_id != before["TOTAL_GOALS_FT:3.5"].catalogue_row_id
        assert before["TOTAL_GOALS_FT:2.5"].line == "2.5"
        assert before["TOTAL_GOALS_FT:3.5"].line == "3.5"

        present = ("2.5", "4.5")
        matchbook.markets_by_id = {str(MB_EVENT_ID): _mb_markets_with_totals(present)}
        kalshi.events = _kalshi_family_events(present)
        kalshi.books = _books_for(kalshi.events)
        await _collect_catalogue(matchbook, kalshi, store)
        after = _rows_by_key(store, fixture_id)
        assert after["TOTAL_GOALS_FT:2.5"].row_state is CatalogueRowState.ACTIVE
        assert after["TOTAL_GOALS_FT:4.5"].row_state is CatalogueRowState.ACTIVE
        disappeared = after["TOTAL_GOALS_FT:3.5"]
        assert disappeared.row_state is CatalogueRowState.DISAPPEARED
        assert disappeared.invalidation_reason == DISAPPEARED_FAMILY_REASON
        assert after["TOTAL_GOALS_FT:2.5"].catalogue_row_id != disappeared.catalogue_row_id
    finally:
        store.close()


@pytest.mark.asyncio
async def test_total_complete_all_present_lines_remain_active() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets_with_totals()})
    events = _kalshi_family_events()
    kalshi = _SeriesFilteredKalshi(
        events, series_by_ticker=_series_map(), books=_books_for(events)
    )
    try:
        first = await _collect_catalogue(matchbook, kalshi, store)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        await _collect_catalogue(matchbook, kalshi, store)
        rows = _rows_by_key(store, fixture_id)
        for key in TOTAL_KEYS:
            assert rows[key].row_state is CatalogueRowState.ACTIVE
            assert rows[key].invalidation_reason is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_match_result_and_btts_honest_disappearance_when_family_checked() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets_with_totals()})
    events = _kalshi_family_events()
    kalshi = _SeriesFilteredKalshi(
        events, series_by_ticker=_series_map(), books=_books_for(events)
    )
    try:
        first = await _collect_catalogue(matchbook, kalshi, store)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        kalshi.events = [
            _kalshi_game_event(rules=REGULATION),
            _kalshi_totals_event(TOTAL_LINES),
            _kalshi_ftts_event(),
        ]
        kalshi.books = _books_for(kalshi.events)
        await _collect_catalogue(matchbook, kalshi, store)
        rows = _rows_by_key(store, fixture_id)
        assert rows[CANONICAL_BTTS_FT].row_state is CatalogueRowState.DISAPPEARED
        assert rows[CANONICAL_BTTS_FT].invalidation_reason == DISAPPEARED_FAMILY_REASON
        assert rows[CANONICAL_MATCH_RESULT_FT].row_state is CatalogueRowState.ACTIVE
        for key in TOTAL_KEYS:
            assert rows[key].row_state is CatalogueRowState.ACTIVE
        assert rows[CANONICAL_FTTS_FT].row_state is CatalogueRowState.ACTIVE
    finally:
        store.close()


@pytest.mark.asyncio
async def test_ftts_timeout_does_not_disappear_from_other_family_success() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets_with_totals()})
    events = _kalshi_family_events()
    kalshi = _SeriesFilteredKalshi(
        events, series_by_ticker=_series_map(), books=_books_for(events)
    )
    try:
        first = await _collect_catalogue(matchbook, kalshi, store)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        ftts_before = _rows_by_key(store, fixture_id)[CANONICAL_FTTS_FT]
        kalshi.timeout_series = {"KXEPLFTTS"}
        await _collect_catalogue(matchbook, kalshi, store)
        ftts_after = _rows_by_key(store, fixture_id)[CANONICAL_FTTS_FT]
        assert ftts_after.row_state is CatalogueRowState.ACTIVE
        assert ftts_after.invalidation_reason is None
        assert ftts_after.last_confirmed_at == ftts_before.last_confirmed_at
        assert _rows_by_key(store, fixture_id)[CANONICAL_MATCH_RESULT_FT].row_state is CatalogueRowState.ACTIVE
        assert _rows_by_key(store, fixture_id)[CANONICAL_BTTS_FT].row_state is CatalogueRowState.ACTIVE
    finally:
        store.close()


@pytest.mark.asyncio
async def test_terminal_fixture_still_marks_all_rows_terminal() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets_with_totals()})
    events = _kalshi_family_events()
    kalshi = _SeriesFilteredKalshi(
        events, series_by_ticker=_series_map(), books=_books_for(events)
    )
    try:
        first = await _collect_catalogue(matchbook, kalshi, store)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        closed = _mb_event()
        closed["status"] = "closed"
        matchbook.events = [closed]
        await _collect_catalogue(matchbook, kalshi, store)
        rows = store.list_rows_for_event(fixture_id)
        assert rows
        assert all(row.row_state is CatalogueRowState.TERMINAL for row in rows)
        assert store.list_active() == []
    finally:
        store.close()


@pytest.mark.asyncio
async def test_persist_timeout_evidence_does_not_use_fixture_wide_listed_ok() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets_with_totals()})
    events = _kalshi_family_events()
    kalshi = _SeriesFilteredKalshi(
        events, series_by_ticker=_series_map(), books=_books_for(events)
    )
    try:
        first = await _collect_catalogue(matchbook, kalshi, store)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        persist_universe_catalogue_pass(
            store,
            canonical_event_id=fixture_id,
            competition="premier_league",
            home_canonical="brentford",
            away_canonical="chelsea",
            kickoff_utc=NOW,
            pairs=[],
            now=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
            generation_id="timeout-gen",
            family_discovery=FamilyDiscoveryCompleteness(
                matchbook_listing_complete=True,
                kalshi_series_results=(
                    {"series": "KXEPLGAME", "status": "ok"},
                    {"series": "KXEPLBTTS", "status": "ok"},
                    {"series": "KXEPLTOTAL", "status": "discovery_timeout"},
                    {"series": "KXEPLFTTS", "status": "ok"},
                ),
                target_competition_code="premier_league",
            ),
            terminal=False,
        )
        rows = _rows_by_key(store, fixture_id)
        for key in TOTAL_KEYS:
            assert rows[key].row_state is CatalogueRowState.ACTIVE
        assert rows[CANONICAL_MATCH_RESULT_FT].row_state is CatalogueRowState.DISAPPEARED
        assert rows[CANONICAL_BTTS_FT].row_state is CatalogueRowState.DISAPPEARED
        assert rows[CANONICAL_FTTS_FT].row_state is CatalogueRowState.DISAPPEARED
    finally:
        store.close()


@pytest.mark.asyncio
async def test_kalshi_budget_before_total_ftts_preserves_rows_even_if_series_ok() -> None:
    """GAME/BTTS inventory plus series-ok TOTAL/FTTS must not invent TOTAL/FTTS completeness."""

    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets_with_totals()})
    events = _kalshi_family_events()
    kalshi = _SeriesFilteredKalshi(
        events, series_by_ticker=_series_map(), books=_books_for(events)
    )
    try:
        first, _collector = await _collect_with(matchbook, kalshi, store)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        before = _rows_by_key(store, fixture_id)
        assert TOTAL_KEYS <= set(before)
        confirmed_at = {
            key: before[key].last_confirmed_at
            for key in (*TOTAL_KEYS, CANONICAL_FTTS_FT, CANONICAL_MATCH_RESULT_FT, CANONICAL_BTTS_FT)
        }

        def _exhaust_after_game_btts(collector: ReadOnlyCrossVenueCollector) -> None:
            assert isinstance(collector, _PartialFamilyBudgetCollector)
            collector.exhaust_kalshi_after = 2

        second, budget_collector = await _collect_with(
            matchbook,
            kalshi,
            store,
            collector_cls=_PartialFamilyBudgetCollector,
            configure=_exhaust_after_game_btts,
        )
        del second
        assert isinstance(budget_collector, _PartialFamilyBudgetCollector)
        assert budget_collector.kalshi_loads == 2
        after = _rows_by_key(store, fixture_id)
        for key in TOTAL_KEYS:
            row = after[key]
            assert row.row_state is CatalogueRowState.ACTIVE
            assert row.invalidation_reason is None
            assert row.last_confirmed_at == confirmed_at[key]
        assert after[CANONICAL_FTTS_FT].row_state is CatalogueRowState.ACTIVE
        assert after[CANONICAL_FTTS_FT].invalidation_reason is None
        assert after[CANONICAL_FTTS_FT].last_confirmed_at == confirmed_at[CANONICAL_FTTS_FT]
        assert after[CANONICAL_MATCH_RESULT_FT].row_state is CatalogueRowState.ACTIVE
        assert after[CANONICAL_BTTS_FT].row_state is CatalogueRowState.ACTIVE
    finally:
        store.close()


@pytest.mark.asyncio
async def test_matchbook_budget_break_before_later_source_cannot_disappear() -> None:
    """One successful Matchbook listing is not fixture listing complete."""

    store = SqliteApprovedMarketCatalogueStore(":memory:")
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets_with_totals()})
    events = _kalshi_family_events()
    kalshi = _SeriesFilteredKalshi(
        events, series_by_ticker=_series_map(), books=_books_for(events)
    )
    try:
        first, _collector = await _collect_with(matchbook, kalshi, store)
        fixture_id = next(
            item.canonical_event_id for item in first.discovered_fixtures if item.matchbook_matched
        )
        before = _rows_by_key(store, fixture_id)
        tracked = (*TOTAL_KEYS, CANONICAL_MATCH_RESULT_FT, CANONICAL_BTTS_FT, CANONICAL_FTTS_FT)
        confirmed_at = {key: before[key].last_confirmed_at for key in tracked}

        second_id = MB_EVENT_ID + 1
        first_raw = _mb_event()
        second_raw = dict(_mb_event())
        second_raw["id"] = second_id
        isolated_matchbook = OverlapMatchbook(
            [first_raw, second_raw],
            {
                str(MB_EVENT_ID): [_mb_match_odds()],
                str(second_id): [_mb_btts(), *[_mb_total_line(line) for line in TOTAL_LINES], _mb_ftts()],
            },
        )
        repository = SqliteMarketIntelligenceRepository()
        collector = _PartialFamilyBudgetCollector(
            matchbook=isolated_matchbook,
            polymarket=_DisabledPolymarket(),
            kalshi=kalshi,
            paper_scan=PaperScanService(MarketIntelligenceService(repository)),
            catalogue_store=store,
        )
        collector.exhaust_matchbook_after = 1
        try:
            normalizer = MatchbookNormalizer()
            mb_events = [
                _NormalizedEvent(first_raw, normalizer.normalize_event(first_raw)),
                _NormalizedEvent(second_raw, normalizer.normalize_event(second_raw)),
            ]
            side = _VenueSideFetch()
            fixture = DiscoveredFixture(
                source_event_id=str(MB_EVENT_ID),
                canonical_event_id=fixture_id,
                home_team=HOME,
                away_team=AWAY,
                competition="Premier League",
                kickoff_utc=KICKOFF,
                last_seen_at=NOW,
            )
            await collector._fetch_matchbook_cluster_side(
                mb_events,
                side=side,
                matchbook_market_filters={},
                issues=[],
                fixture=fixture,
            )
            assert isolated_matchbook.list_markets_calls == [str(MB_EVENT_ID)]
            assert side.listed is True
            assert side.failed is False
            assert side.listing_complete is False
        finally:
            repository.close()

        persist_universe_catalogue_pass(
            store,
            canonical_event_id=fixture_id,
            competition="premier_league",
            home_canonical="brentford",
            away_canonical="chelsea",
            kickoff_utc=NOW,
            pairs=[],
            now=datetime(2026, 9, 19, 12, 0, tzinfo=UTC),
            generation_id="mb-partial-listing",
            family_discovery=FamilyDiscoveryCompleteness(
                matchbook_listing_complete=False,
                kalshi_series_results=(
                    {"series": "KXEPLGAME", "status": "ok"},
                    {"series": "KXEPLBTTS", "status": "ok"},
                    {"series": "KXEPLTOTAL", "status": "ok"},
                    {"series": "KXEPLFTTS", "status": "ok"},
                ),
                target_competition_code="premier_league",
            ),
            terminal=False,
        )
        after = _rows_by_key(store, fixture_id)
        for key in tracked:
            row = after[key]
            assert row.row_state is CatalogueRowState.ACTIVE
            assert row.invalidation_reason is None
            assert row.last_confirmed_at == confirmed_at[key]
    finally:
        store.close()


def test_wave2_does_not_change_scanner_architecture() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert settings.paper_live_refresh_hot_interval_seconds == 30
    assert settings.paper_background_price_interval_seconds == 90
    assert settings.paper_universe_discovery_interval_seconds == 1800
    assert settings.paper_live_refresh_universe_interval_seconds == 180
    assert settings.paper_background_price_interval_seconds == 90
    assert settings.paper_universe_discovery_interval_seconds == 1800
    assert settings.paper_scan_provider_timeout_seconds == 8
    assert settings.paper_scan_venue_timeout_seconds == 15
    assert settings.paper_scan_matchbook_concurrency == 4
    assert settings.paper_scan_kalshi_concurrency == 4
    assert settings.matchbook_event_per_page == 100
    assert settings.matchbook_market_per_page == 100
    assert settings.matchbook_market_max_pages == 10
    assert "KXEPLTOTAL" in settings.kalshi_series_tickers
    assert "KXEPLFTTS" in settings.kalshi_series_tickers
    assert "KXEFLCHAMPIONSHIPFTTS" not in settings.kalshi_series_tickers
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)

    persist_src = inspect.getsource(persist_universe_catalogue_pass)
    import sports_hedge.application.catalogue_maintenance as catalogue_maintenance

    maintenance_src = inspect.getsource(catalogue_maintenance)
    offloop_src = inspect.getsource(persist_universe_catalogue_pass_offloop)
    collector_persist = inspect.getsource(
        ReadOnlyCrossVenueCollector._persist_universe_catalogue_from_pairs
    )
    collector_terminal = inspect.getsource(
        ReadOnlyCrossVenueCollector._mark_universe_catalogue_terminal
    )
    collector_scan = inspect.getsource(ReadOnlyCrossVenueCollector._scan_cluster)
    collector_kalshi = inspect.getsource(ReadOnlyCrossVenueCollector._fetch_kalshi_cluster_side)
    collector_matchbook = inspect.getsource(
        ReadOnlyCrossVenueCollector._fetch_matchbook_cluster_side
    )
    remainder_src = inspect.getsource(_mark_unprocessed_kalshi_families_incomplete)
    assert "listed_ok" not in persist_src
    assert "FamilyDiscoveryCompleteness" in persist_src
    assert "complete_family_keys" in maintenance_src
    assert "asyncio.to_thread" in offloop_src
    assert "persist_universe_catalogue_pass_offloop" in collector_persist
    assert "persist_universe_catalogue_pass_offloop" in collector_terminal
    assert "FamilyDiscoveryCompleteness" in collector_persist
    assert "listed_ok" not in collector_persist
    assert "matchbook_side.listing_complete" in collector_scan
    assert "matchbook_listing_complete=matchbook_side.listed" not in collector_scan
    assert "_mark_unprocessed_kalshi_families_incomplete" in collector_kalshi
    assert "truncated" in collector_matchbook
    assert "listing_complete" in collector_matchbook
    assert "remaining_events" in remainder_src
    assert "FamilyDiscoveryCompleteness" not in inspect.getsource(price_engine_module)
    assert "FamilyDiscoveryCompleteness" not in inspect.getsource(live_refresh_module)
    assert "FamilyDiscoveryCompleteness" not in inspect.getsource(scan_lanes_module)

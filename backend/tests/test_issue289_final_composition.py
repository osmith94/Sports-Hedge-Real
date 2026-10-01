"""Issue #289: combined-path proof for exact #279 + corrected #286 composition.

Deterministic fixture/demo plus captured public Kalshi payload shape.
Not live quotes, not owner-live books, not modelled probabilities.
PAPER / read-only. Polymarket off. Execution disabled.
"""

from __future__ import annotations

import copy
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import (
    CollectionReport,
    DiscoveredFixture,
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.fixture_clusters import cluster_canonical_event_id, cluster_identity_aliases, cluster_venue_events
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.hot_identity import hot_scheduling_key
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.catalogue.admission import assess_catalogue_admission, catalogue_allows_solver
from sports_hedge.catalogue.classify import PayloadSide, classify_payload_pair, normalize_payload_side
from sports_hedge.catalogue.corpus import GAMEWIN_TEMPLATE, KALSHI_GAMEWIN_SERIES, _kalshi, _kalshi_1x2, _mb, _mb_1x2
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.config import Settings
from sports_hedge.domain.football import SettlementScope
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.aliases import SAFE_TEAM_AFFIX_TOKENS, resolve_team_name
from sports_hedge.facts.identity import canonical_team_id
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.venues import (
    KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON,
    classify_settlement_wording,
)
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_issue277_club_name_variants import _canonical, _venue_event
from test_issue278_settlement_equivalent_recovery import (
    GAMEWIN_SERIES,
    _captured,
    _mb_event,
    _mb_match_odds,
)
from venue_cost_helpers import matchbook_polymarket_costs

KICKOFF = datetime(2026, 9, 18, 18, 30, tzinfo=UTC)
NOW = datetime(2026, 9, 18, 9, 45, tzinfo=UTC)
FORBIDDEN_WRITE_METHODS = (
    "place_order",
    "cancel_order",
    "place_bet",
    "sign_wallet",
    "submit_order",
)
SERIE_A_SERIES = {
    "ticker": "KXSERIEAGAME",
    "title": "Serie A",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "contract_terms_url": "https://assets.kalshi.com/contract_terms/SOCCERGAMEWIN.pdf",
    "contract_family": dict(KALSHI_GAMEWIN_SERIES["contract_family"]),
}


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx"),
        FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency"),
    ]


def _scan_costs(series: dict[str, Any] | None = None) -> list[Any]:
    return [
        *matchbook_polymarket_costs(),
        kalshi_cost_from_series(series or GAMEWIN_SERIES, captured_at=NOW),
    ]


def _rewrite_captured(
    *,
    home: str,
    away: str,
    title: str,
    competition: str,
    ticker: str,
    series_ticker: str,
    include_fair_price: bool,
) -> dict[str, Any]:
    payload = copy.deepcopy(_captured()["payload"])
    payload["event_ticker"] = ticker
    payload["series_ticker"] = series_ticker
    payload["title"] = title
    payload["product_metadata"] = {"competition": competition, "competition_scope": "Game"}
    replacements = (
        ("Bayern Munich", home),
        ("Union Berlin", away),
        ("Bundesliga", competition),
        ("KXBUNDESLIGAGAME-26SEP18BMUUNI", ticker),
        ("KXBUNDESLIGAGAME", series_ticker),
    )
    for market in payload["markets"]:
        for field in ("ticker", "event_ticker", "title", "yes_sub_title", "no_sub_title", "rules_primary", "rules_secondary"):
            text = str(market.get(field) or "")
            for old, new in replacements:
                text = text.replace(old, new)
            market[field] = text
        if not include_fair_price:
            market["rules_secondary"] = ""
    return payload


def _mb_named(home: str, away: str, *, competition: str, event_id: int) -> dict[str, Any]:
    return {
        "id": event_id,
        "name": f"{home} vs {away}",
        "start": KICKOFF.isoformat(),
        "competition-name": competition,
    }


def _mb_odds(home: str, away: str, *, market_id: int) -> dict[str, Any]:
    return {
        "id": market_id,
        "name": "Match Odds",
        "runners": [
            {"id": 1, "name": home, "prices": [{"side": "back", "odds": "2.10", "available-amount": "80"}]},
            {"id": 2, "name": "Draw", "prices": [{"side": "back", "odds": "3.40", "available-amount": "80"}]},
            {"id": 3, "name": away, "prices": [{"side": "back", "odds": "3.60", "available-amount": "80"}]},
        ],
    }


class _NamedMatchbook:
    def __init__(self, event: dict[str, Any], market: dict[str, Any]) -> None:
        self.event = event
        self.market = market

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [self.event]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": [self.market]}


class _DisabledPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []


class _NestedKalshi:
    def __init__(self, event: dict[str, Any], series: dict[str, Any]) -> None:
        self.event = event
        self.series = series
        self.get_market_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [self.event]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.get_market_calls.append(ticker)
        for market in self.event.get("markets") or []:
            if str(market.get("ticker") or "") == ticker:
                return dict(market)
        raise AssertionError(f"unexpected Get Market ticker {ticker}")

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.47", "100.00"]],
                "no_dollars": [["0.52", "200.00"]],
            }
        }

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        return {**self.series, "ticker": series_ticker}


async def _collect(event: dict[str, Any], *, home: str, away: str, competition: str, series: dict[str, Any]) -> tuple[Any, _NestedKalshi]:
    repository = SqliteMarketIntelligenceRepository()
    kalshi = _NestedKalshi(event, series)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=_NamedMatchbook(_mb_named(home, away, competition=competition, event_id=28901), _mb_odds(home, away, market_id=28910)),
        polymarket=_DisabledPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=_scan_costs(series),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        )
    finally:
        repository.close()
    return report, kalshi


def _fixture(
    canonical_id: str,
    *,
    home: str,
    away: str,
    source_event_id: str,
    matchbook_matched: bool,
    kalshi_matched: bool,
) -> DiscoveredFixture:
    return DiscoveredFixture(
        source=VenueName.MATCHBOOK if matchbook_matched else VenueName.KALSHI,
        source_event_id=source_event_id,
        canonical_event_id=canonical_id,
        home_team=home,
        away_team=away,
        competition="Serie A",
        kickoff_utc=KICKOFF,
        last_seen_at=NOW,
        last_scanned_at=NOW,
        matchbook_matched=matchbook_matched,
        kalshi_matched=kalshi_matched,
        market_evaluation_state=MarketEvaluationState.EVALUATED.value,
        opportunity_state="matched",
    )


def _report(
    fixture: DiscoveredFixture,
    *,
    aliases: dict[str, str],
    source_events: list[dict[str, Any]],
) -> CollectionReport:
    return CollectionReport(
        started_at=NOW,
        completed_at=NOW,
        discovered_fixtures=[fixture],
        scan_lane=ScanLane.UNIVERSE.value,
        fixture_identity_aliases=aliases,
        fixture_source_events={fixture.canonical_event_id: source_events},
        operator_summary="issue-289-final-composition",
        venue_health={"matchbook": "ok", "kalshi": "ok", "polymarket": "disabled"},
    )


def test_paper_polymarket_and_execution_boundaries_hold() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method), f"{client.__name__}.{method} must not exist"


def test_event_matcher_threshold_and_no_generic_stripping() -> None:
    assert EventMatcher().threshold == 0.92
    assert "threshold: float = 0.92" in inspect.getsource(EventMatcher.__init__)
    assert SAFE_TEAM_AFFIX_TOKENS == frozenset({"fc", "cf", "afc", "sc", "calcio", "bc"})
    assert resolve_team_name("AC Unknownville") == "ac unknownville"
    assert resolve_team_name("Unknownville Calcio") == "unknownville calcio"
    assert resolve_team_name("Unknownville Barcelona") == "unknownville barcelona"
    assert resolve_team_name("AC Milan") != resolve_team_name("AC Monza")
    assert resolve_team_name("Espanyol") != resolve_team_name("Barcelona")
    assert canonical_team_id("AC Monza") == canonical_team_id("Monza")
    assert canonical_team_id("RCD Espanyol Barcelona") == canonical_team_id("Espanyol")


def test_bayern_union_and_betis_getafe_identity_regressions() -> None:
    matcher = EventMatcher()
    assert matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Bayern Munich",
            "Union Berlin",
            competition="Bundesliga",
            source_event_id="mb-bayern",
        ),
        _canonical(
            VenueName.KALSHI,
            "FC Bayern München",
            "1. FC Union Berlin",
            competition="Bundesliga",
            source_event_id="k-bayern",
        ),
    ).matched is True
    assert matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Real Betis",
            "Getafe",
            competition="La Liga",
            source_event_id="mb-betis",
        ),
        _canonical(
            VenueName.KALSHI,
            "Betis",
            "Getafe",
            competition="La Liga",
            source_event_id="k-betis",
        ),
    ).matched is True


def test_monza_and_espanyol_alias_collapse_to_one_canonical_fixture() -> None:
    matcher = EventMatcher()
    for mb_home, mb_away, k_home, k_away, competition in (
        ("Monza", "Sassuolo", "AC Monza", "Sassuolo Calcio", "Serie A"),
        ("Espanyol", "Elche CF", "Espanyol Barcelona", "Elche CF", "La Liga"),
        ("Espanyol", "Elche CF", "RCD Espanyol Barcelona", "Elche CF", "La Liga"),
    ):
        clusters, counts = cluster_venue_events(
            matchbook=[
                _venue_event(
                    VenueName.MATCHBOOK,
                    mb_home,
                    mb_away,
                    competition=competition,
                    source_event_id="mb-1",
                )
            ],
            polymarket=[],
            kalshi=[
                _venue_event(
                    VenueName.KALSHI,
                    k_home,
                    k_away,
                    competition=competition,
                    source_event_id="k-game",
                ),
                _venue_event(
                    VenueName.KALSHI,
                    k_home,
                    k_away,
                    competition=competition,
                    source_event_id="k-btts",
                ),
            ],
            matcher=matcher,
            max_event_pairs=8,
        )
        assert len(clusters) == 1
        assert counts["matchbook_kalshi"] == 1
        canonical_id = cluster_canonical_event_id(clusters[0])
        aliases = cluster_identity_aliases(clusters[0])
        assert aliases["mb-1"] == canonical_id
        assert aliases["k-game"] == canonical_id
        assert aliases["k-btts"] == canonical_id


def test_exclusion_only_wording_does_not_fabricate_regulation_scope() -> None:
    for text in (
        "This does not include extra time or penalties.",
        "Not including extra time and penalties.",
    ):
        scope, extra_time, penalties = classify_settlement_wording(text)
        assert scope is SettlementScope.UNKNOWN
        assert extra_time is None
        assert penalties is None


def test_single_subject_extra_time_exclusion_remains_regulation() -> None:
    for text in ("Resolves not including extra time.", "Does not include extra time."):
        scope, extra_time, penalties = classify_settlement_wording(text)
        assert scope is SettlementScope.REGULATION_TIME
        assert extra_time is False


def test_generic_gamewin_and_title_only_reg_time_are_paper_assumed_not_approved() -> None:
    assessment = classify_payload_pair(
        _mb([_mb_1x2()]),
        _kalshi(_kalshi_1x2(rules=GAMEWIN_TEMPLATE), series=KALSHI_GAMEWIN_SERIES),
    )
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.reason == "paper_assumed_equivalent"
    assert assessment.settlement_assumption == "regulation_time"
    markets = _kalshi_1x2(rules=GAMEWIN_TEMPLATE)
    for item in markets:
        item["subtitle"] = "REG TIME"
        item["title"] = f"{item['title']} REG TIME"
    titled = classify_payload_pair(_mb([_mb_1x2()]), _kalshi(markets, series=KALSHI_GAMEWIN_SERIES))
    assert titled.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    assert titled.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert titled.settlement_complete is False


def test_material_kalshi_fair_price_is_paper_assumed_never_approved() -> None:
    captured = _captured()["payload"]
    assert "cancelled or rescheduled" in captured["markets"][0]["rules_secondary"].casefold()
    assert "fair price" in captured["markets"][0]["rules_secondary"].casefold()
    left = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_match_odds()])
    right = PayloadSide(
        venue=VenueName.KALSHI,
        event=captured,
        markets=list(captured["markets"]),
        series=GAMEWIN_SERIES,
    )
    assessment = classify_payload_pair(left, right)
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    assert assessment.reason == "paper_assumed_equivalent"
    assert assessment.execution_eligible is True
    mb = normalize_payload_side(left)
    kalshi = normalize_payload_side(right)
    assert kalshi.settlement.unknown_reason == KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON
    assert catalogue_allows_solver(mb, kalshi) is True
    assert scan_eligible_pair(mb, kalshi, MarketMatcher().match(mb, kalshi)) is True
    assert assess_catalogue_admission(mb, kalshi).live_execution_eligible is True


@pytest.mark.asyncio
async def test_collector_safe_90m_monza_collapses_and_is_matched_equivalent() -> None:
    event = _rewrite_captured(
        home="AC Monza",
        away="Sassuolo Calcio",
        title="AC Monza vs Sassuolo Calcio",
        competition="Serie A",
        ticker="KXSERIEAGAME-MONSAS",
        series_ticker="KXSERIEAGAME",
        include_fair_price=False,
    )
    report, kalshi = await _collect(
        event, home="Monza", away="Sassuolo", competition="Serie A", series=SERIE_A_SERIES
    )
    clustered = [item for item in report.discovered_fixtures if item.matchbook_matched and item.kalshi_matched]
    assert len(clustered) == 1
    fixture = clustered[0]
    assert fixture.matched_equivalent_count == 1
    rows = report.fixture_markets[fixture.canonical_event_id]
    assert any(
        row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
        and row.family == "match_result"
        and row.entered_solver
        for row in rows
    )
    coverage = report.scan_diagnostics["matching_coverage"]
    assert coverage["equivalent_markets"] == 1
    assert coverage["matching_state"] == "cross_venue_equivalent_present"
    assert kalshi.get_market_calls == []
    mb = normalize_payload_side(
        PayloadSide(
            venue=VenueName.MATCHBOOK,
            event=_mb_named("Monza", "Sassuolo", competition="Serie A", event_id=28901),
            markets=[_mb_odds("Monza", "Sassuolo", market_id=28910)],
        )
    )
    kalshi_side = normalize_payload_side(
        PayloadSide(venue=VenueName.KALSHI, event=event, markets=list(event["markets"]), series=SERIE_A_SERIES)
    )
    assert catalogue_allows_solver(mb, kalshi_side) is True
    assert assess_catalogue_admission(mb, kalshi_side).allowed is True
    assert scan_eligible_pair(mb, kalshi_side, MarketMatcher().match(mb, kalshi_side)) is True
    assert Settings().sports_hedge_execution_enabled is False


@pytest.mark.asyncio
async def test_collector_fair_price_monza_collapses_with_precise_reason_and_solver_closed() -> None:
    event = _rewrite_captured(
        home="AC Monza",
        away="Sassuolo Calcio",
        title="AC Monza vs Sassuolo Calcio",
        competition="Serie A",
        ticker="KXSERIEAGAME-MONSAS",
        series_ticker="KXSERIEAGAME",
        include_fair_price=True,
    )
    report, kalshi = await _collect(
        event, home="Monza", away="Sassuolo", competition="Serie A", series=SERIE_A_SERIES
    )
    clustered = [item for item in report.discovered_fixtures if item.matchbook_matched and item.kalshi_matched]
    assert len(clustered) == 1
    fixture = clustered[0]
    assert fixture.matched_equivalent_count == 1
    rows = report.fixture_markets[fixture.canonical_event_id]
    assert any(
        row.comparison_status is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
        and row.family == "match_result"
        for row in rows
    )
    assert not any(row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT for row in rows)
    coverage = report.scan_diagnostics["matching_coverage"]
    assert coverage["equivalent_markets"] == 1
    assert kalshi.get_market_calls == []
    mb = normalize_payload_side(
        PayloadSide(
            venue=VenueName.MATCHBOOK,
            event=_mb_named("Monza", "Sassuolo", competition="Serie A", event_id=28901),
            markets=[_mb_odds("Monza", "Sassuolo", market_id=28910)],
        )
    )
    kalshi_side = normalize_payload_side(
        PayloadSide(venue=VenueName.KALSHI, event=event, markets=list(event["markets"]), series=SERIE_A_SERIES)
    )
    assert kalshi_side.settlement.unknown_reason == KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON
    assert catalogue_allows_solver(mb, kalshi_side) is True
    assert assess_catalogue_admission(mb, kalshi_side).allowed is True
    assert assess_catalogue_admission(mb, kalshi_side).live_execution_eligible is True
    assert scan_eligible_pair(mb, kalshi_side, MarketMatcher().match(mb, kalshi_side)) is True


def test_fresh_process_current_state_starts_empty() -> None:
    store = FixtureCurrentStateStore()
    assert store._rows == {}
    assert store.has_collection() is False


def test_pre_alias_split_rows_merge_when_post_alias_source_ids_overlap() -> None:
    """Open UNIVERSE checkpoint restore can rehydrate pre-alias split rows.

    FixtureCurrentStateStore is process-memory. A fresh process starts empty
    unless an open checkpoint restores a previous report. Team+kickoff
    collision alone does not merge; overlapping trusted source IDs do.
    """

    store = FixtureCurrentStateStore()
    mb_row = _fixture(
        "evt-pre-mb",
        home="Monza",
        away="Sassuolo",
        source_event_id="27701",
        matchbook_matched=True,
        kalshi_matched=True,
    )
    kalshi_only = _fixture(
        "evt-pre-k",
        home="AC Monza",
        away="Sassuolo Calcio",
        source_event_id="KXSERIEABTTS-MONSAS",
        matchbook_matched=False,
        kalshi_matched=True,
    )
    store.upsert_from_report(
        _report(
            mb_row,
            aliases={"evt-pre-mb": "evt-pre-mb", "27701": "evt-pre-mb", "KXSERIEAGAME-MONSAS": "evt-pre-mb"},
            source_events=[
                {"venue": "matchbook", "source_event_id": "27701", "raw": {"id": "27701"}},
                {
                    "venue": "kalshi",
                    "source_event_id": "KXSERIEAGAME-MONSAS",
                    "raw": {"event_ticker": "KXSERIEAGAME-MONSAS"},
                },
            ],
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    store.upsert_from_report(
        _report(
            kalshi_only,
            aliases={"evt-pre-k": "evt-pre-k", "KXSERIEABTTS-MONSAS": "evt-pre-k"},
            source_events=[
                {
                    "venue": "kalshi",
                    "source_event_id": "KXSERIEABTTS-MONSAS",
                    "raw": {"event_ticker": "KXSERIEABTTS-MONSAS"},
                }
            ],
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    assert len(store._rows) == 2
    assert hot_scheduling_key(mb_row) == hot_scheduling_key(kalshi_only)

    clustered = _fixture(
        "evt-post-alias",
        home="Monza",
        away="Sassuolo",
        source_event_id="27701",
        matchbook_matched=True,
        kalshi_matched=True,
    )
    store.upsert_from_report(
        _report(
            clustered,
            aliases={
                "evt-post-alias": "evt-post-alias",
                "27701": "evt-post-alias",
                "KXSERIEAGAME-MONSAS": "evt-post-alias",
                "KXSERIEABTTS-MONSAS": "evt-post-alias",
                "evt-pre-mb": "evt-post-alias",
                "evt-pre-k": "evt-post-alias",
            },
            source_events=[
                {"venue": "matchbook", "source_event_id": "27701", "raw": {"id": "27701"}},
                {
                    "venue": "kalshi",
                    "source_event_id": "KXSERIEAGAME-MONSAS",
                    "raw": {"event_ticker": "KXSERIEAGAME-MONSAS"},
                },
                {
                    "venue": "kalshi",
                    "source_event_id": "KXSERIEABTTS-MONSAS",
                    "raw": {"event_ticker": "KXSERIEABTTS-MONSAS"},
                },
            ],
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    assert len(store._rows) == 1
    survivor = next(iter(store._rows))
    assert store.resolve_canonical_id("27701") == survivor
    assert store.resolve_canonical_id("KXSERIEAGAME-MONSAS") == survivor
    assert store.resolve_canonical_id("KXSERIEABTTS-MONSAS") == survivor


def test_scheduling_key_alone_does_not_absorb_unrelated_canonical_rows() -> None:
    store = FixtureCurrentStateStore()
    first = _fixture(
        "evt-a",
        home="Monza",
        away="Sassuolo",
        source_event_id="src-a",
        matchbook_matched=True,
        kalshi_matched=False,
    )
    second = _fixture(
        "evt-b",
        home="AC Monza",
        away="Sassuolo Calcio",
        source_event_id="src-b",
        matchbook_matched=False,
        kalshi_matched=True,
    )
    store.upsert_from_report(
        _report(
            first,
            aliases={"evt-a": "evt-a", "src-a": "evt-a"},
            source_events=[{"venue": "matchbook", "source_event_id": "src-a", "raw": {"id": "src-a"}}],
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    store.upsert_from_report(
        _report(
            second,
            aliases={"evt-b": "evt-b", "src-b": "evt-b"},
            source_events=[{"venue": "kalshi", "source_event_id": "src-b", "raw": {"id": "src-b"}}],
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    assert hot_scheduling_key(first) == hot_scheduling_key(second)
    assert len(store._rows) == 2

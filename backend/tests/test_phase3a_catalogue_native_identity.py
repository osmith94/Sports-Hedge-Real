"""Phase 3A: one catalogue row keeps venue-native ids unless they are empty.

Synthetic fixture markets. Not live quotes. Polymarket is still outside the
Approved Register; these rows are built directly to test identity merge.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from sports_hedge.application.approved_market_catalogue import OutcomeNativeId
from sports_hedge.application.catalogue_maintenance import (
    CATALOGUE_NATIVE_IDENTITY_CONFLICT,
    CatalogueNativeIdentityConflict,
    CataloguePairIdentity,
    FamilyDiscoveryCompleteness,
    _merge_native_list,
    _merge_native_scalar,
    catalogue_row_id_for,
    persist_universe_catalogue_pass,
)
from sports_hedge.application.opportunity_viability import reset_opportunity_viability_cache
from sports_hedge.application.price_engine import (
    CataloguePriceEngine,
    DerivedPriceEngineItem,
    PriceEngineItemStatus,
    PriceEngineRuntimeItem,
    PriceEngineSliceResult,
    RetrievedVenuePayload,
)
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import (
    APPROVED_PAPER_VENUE_PAIR,
    canonical_key_for_market,
)
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

NOW = datetime(2026, 9, 26, 18, 45, tzinfo=UTC)
EVENT_ID = "evt:eng-esp"
KEY = "MATCH_RESULT_FT"


def _event(venue: VenueName, source_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        competition="UEFA Nations League",
        home_team="England",
        away_team="Spain",
        kickoff_utc=NOW,
        source_venue=venue,
        source_event_id=source_id,
    )


def _hda(venue: VenueName, event_id: str, market_id: str, runners: list[tuple[str, str]]) -> CanonicalMarket:
    event = _event(venue, event_id)
    return CanonicalMarket(
        event=event,
        source_venue=venue,
        source_market_id=market_id,
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
        settlement=SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            line=None,
            push_possible=False,
            extra_time_included=False,
            penalties_included=False,
        ),
        runners=[
            CanonicalRunner(source_runner_id=native, outcome=CanonicalOutcome(outcome), label=outcome)
            for outcome, native in runners
        ],
    )


def _kalshi_runners(prefix: str) -> list[tuple[str, str]]:
    return [
        ("home", f"{prefix}:home"),
        ("draw", f"{prefix}:draw"),
        ("away", f"{prefix}:away"),
    ]


def _pair(
    *,
    matchbook: CanonicalMarket | None = None,
    kalshi: CanonicalMarket | None = None,
    polymarket: CanonicalMarket | None = None,
    kalshi_event: str = "KXUEFANLGAME-26SEP26ENGESP",
    series: str = "KXUEFANLGAME",
) -> CataloguePairIdentity:
    return CataloguePairIdentity(
        register_canonical_key=KEY,
        matchbook=matchbook,
        kalshi=kalshi,
        polymarket=polymarket,
        kalshi_event_payload={"event_ticker": kalshi_event, "series_ticker": series},
        kalshi_series_payload={"ticker": series},
    )


def _persist(
    store: SqliteApprovedMarketCatalogueStore,
    pairs: list[CataloguePairIdentity],
    conflicts: list[CatalogueNativeIdentityConflict] | None = None,
) -> None:
    persist_universe_catalogue_pass(
        store,
        canonical_event_id=EVENT_ID,
        competition="uefa_nations_league",
        home_canonical="England",
        away_canonical="Spain",
        kickoff_utc=NOW,
        pairs=pairs,
        now=NOW,
        generation_id="phase3a",
        family_discovery=FamilyDiscoveryCompleteness(),
        terminal=False,
        allow_disappearance=False,
        identity_conflicts=conflicts,
    )


def _mb() -> CanonicalMarket:
    return _hda(
        VenueName.MATCHBOOK,
        "mb-eng-esp",
        "mb-1x2",
        [("home", "mb-home"), ("draw", "mb-draw"), ("away", "mb-away")],
    )


def _kalshi(prefix: str = "KXUEFANLGAME-26SEP26ENGESP") -> CanonicalMarket:
    return _hda(VenueName.KALSHI, prefix, prefix, _kalshi_runners(prefix))


def _polymarket(event_id: str = "1016065", market_id: str = "4521504") -> CanonicalMarket:
    return _hda(
        VenueName.POLYMARKET,
        event_id,
        market_id,
        [
            ("home", "713856789012345678901"),
            ("draw", "713856789012345678902"),
            ("away", "713856789012345678903"),
        ],
    )


def test_scalar_merge_fills_keeps_and_conflicts() -> None:
    conflicts: list[CatalogueNativeIdentityConflict] = []
    kwargs = {
        "venue": "kalshi",
        "field": "kalshi_event_ticker",
        "conflicts": conflicts,
        "canonical_event_id": EVENT_ID,
        "register_canonical_key": KEY,
        "venue_pair": "matchbook/kalshi",
    }
    assert _merge_native_scalar("KX-A", None, **kwargs) == "KX-A"
    assert conflicts == []
    assert _merge_native_scalar(None, "KX-A", **kwargs) == "KX-A"
    assert _merge_native_scalar("KX-A", "KX-A", **kwargs) == "KX-A"
    assert _merge_native_scalar("  KX-A  ", "KX-A", **kwargs) == "KX-A"
    assert conflicts == []
    assert _merge_native_scalar("KX-B", "KX-A", **kwargs) == "KX-A"
    assert len(conflicts) == 1
    assert conflicts[0].reason == CATALOGUE_NATIVE_IDENTITY_CONFLICT
    assert conflicts[0].existing_native_value == "KX-A"
    assert conflicts[0].incoming_native_value == "KX-B"
    assert "KX-B" in conflicts[0].audit_detail()


def test_list_merge_is_ordered_exact_identity() -> None:
    conflicts: list[CatalogueNativeIdentityConflict] = []
    kwargs = {
        "venue": "kalshi",
        "field": "kalshi_market_tickers",
        "conflicts": conflicts,
        "canonical_event_id": EVENT_ID,
        "register_canonical_key": KEY,
        "venue_pair": "polymarket/kalshi",
    }
    stored = ["A", "B", "C"]
    assert _merge_native_list(stored, [], **kwargs) == stored
    assert _merge_native_list([], stored, **kwargs) == stored
    assert _merge_native_list(["A", "B", "C"], stored, **kwargs) == stored
    assert conflicts == []
    kept = _merge_native_list(["A", "X", "C"], stored, **kwargs)
    assert kept == stored
    assert len(conflicts) == 1
    assert conflicts[0].field == "kalshi_market_tickers"
    assert "X" in conflicts[0].incoming_native_value


def test_matchbook_kalshi_then_same_kalshi_polymarket_is_one_row() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(store, [_pair(matchbook=_mb(), kalshi=_kalshi())])
        rows = store.list_active()
        assert len(rows) == 1
        row = rows[0]
        assert row.catalogue_row_id == catalogue_row_id_for(EVENT_ID, KEY)
        assert row.matchbook_event_id == "mb-eng-esp"
        assert row.matchbook_market_id == "mb-1x2"
        assert row.kalshi_event_ticker == "KXUEFANLGAME-26SEP26ENGESP"
        assert row.polymarket_event_id is None
        assert row.polymarket_token_ids == []

        conflicts: list[CatalogueNativeIdentityConflict] = []
        _persist(
            store,
            [_pair(kalshi=_kalshi(), polymarket=_polymarket())],
            conflicts,
        )
        assert conflicts == []
        again = store.list_active()
        assert len(again) == 1
        filled = again[0]
        assert filled.catalogue_row_id == row.catalogue_row_id
        assert filled.matchbook_event_id == "mb-eng-esp"
        assert filled.matchbook_market_id == "mb-1x2"
        assert filled.kalshi_event_ticker == row.kalshi_event_ticker
        assert filled.kalshi_market_tickers == row.kalshi_market_tickers
        assert filled.polymarket_event_id == "1016065"
        assert filled.polymarket_market_id == "4521504"
        assert [item.native_id for item in filled.polymarket_token_ids] == [
            "713856789012345678901",
            "713856789012345678902",
            "713856789012345678903",
        ]

        _persist(
            store,
            [_pair(matchbook=_mb(), kalshi=_kalshi(), polymarket=_polymarket())],
            conflicts,
        )
        assert conflicts == []
        repeated = store.list_active()
        assert len(repeated) == 1
        assert repeated[0].catalogue_row_id == row.catalogue_row_id
        assert repeated[0].content_version == filled.content_version
        assert repeated[0].matchbook_market_id == "mb-1x2"
        assert repeated[0].kalshi_event_ticker == "KXUEFANLGAME-26SEP26ENGESP"
        assert repeated[0].polymarket_event_id == "1016065"
    finally:
        store.close()


def test_conflicting_kalshi_identity_preserves_the_row_and_continues() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(store, [_pair(matchbook=_mb(), kalshi=_kalshi())])
        original = store.list_active()[0]
        conflicts: list[CatalogueNativeIdentityConflict] = []
        _persist(
            store,
            [
                _pair(
                    kalshi=_kalshi("KXUEFANLGAME-OTHER"),
                    polymarket=_polymarket(event_id="999", market_id="888"),
                    kalshi_event="KXUEFANLGAME-OTHER",
                )
            ],
            conflicts,
        )
        assert conflicts
        assert conflicts[0].reason == CATALOGUE_NATIVE_IDENTITY_CONFLICT
        assert conflicts[0].canonical_event_id == EVENT_ID
        assert conflicts[0].register_canonical_key == KEY
        assert conflicts[0].venue == VenueName.KALSHI.value
        assert conflicts[0].field == "kalshi_event_ticker"
        assert conflicts[0].existing_native_value == "KXUEFANLGAME-26SEP26ENGESP"
        assert conflicts[0].incoming_native_value == "KXUEFANLGAME-OTHER"
        assert conflicts[0].venue_pair == "kalshi/polymarket"
        held = store.get_row(original.catalogue_row_id)
        assert held is not None
        assert held.kalshi_event_ticker == original.kalshi_event_ticker
        assert held.kalshi_market_tickers == original.kalshi_market_tickers
        assert held.matchbook_market_id == original.matchbook_market_id
        assert held.polymarket_event_id is None
        assert held.polymarket_token_ids == []
        assert held.content_version == original.content_version
        assert len(store.list_active()) == 1
        _persist(store, [_pair(matchbook=_mb(), kalshi=_kalshi())])
        assert len(store.list_active()) == 1
        assert store.get_row(original.catalogue_row_id).kalshi_event_ticker == original.kalshi_event_ticker
    finally:
        store.close()


def test_one_row_prepares_matchbook_kalshi_and_polymarket_combinations() -> None:
    reset_opportunity_viability_cache()
    identity_row = _hda(
        VenueName.POLYMARKET,
        "1016065",
        "4521504",
        [
            ("home", "713856789012345678901"),
            ("draw", "713856789012345678902"),
            ("away", "713856789012345678903"),
        ],
    )
    assert canonical_key_for_market(identity_row) is None
    assert APPROVED_PAPER_VENUE_PAIR == frozenset({VenueName.MATCHBOOK, VenueName.KALSHI})
    identity = DerivedPriceEngineItem(
        catalogue_row_id=catalogue_row_id_for(EVENT_ID, KEY),
        content_version=1,
        canonical_event_id=EVENT_ID,
        register_canonical_key=KEY,
        matchbook_event_id="mb-eng-esp",
        matchbook_market_id="mb-1x2",
        matchbook_runner_ids=[
            OutcomeNativeId(outcome="home", native_id="mb-home"),
            OutcomeNativeId(outcome="draw", native_id="mb-draw"),
            OutcomeNativeId(outcome="away", native_id="mb-away"),
        ],
        kalshi_event_ticker="KXUEFANLGAME-26SEP26ENGESP",
        kalshi_market_tickers=["KXUEFANLGAME-26SEP26ENGESP"],
        kalshi_outcome_ids=[
            OutcomeNativeId(outcome="home", native_id="KXUEFANLGAME-26SEP26ENGESP:home"),
            OutcomeNativeId(outcome="draw", native_id="KXUEFANLGAME-26SEP26ENGESP:draw"),
            OutcomeNativeId(outcome="away", native_id="KXUEFANLGAME-26SEP26ENGESP:away"),
        ],
        polymarket_event_id="1016065",
        polymarket_market_id="4521504",
        polymarket_token_ids=[
            OutcomeNativeId(outcome="home", native_id="713856789012345678901"),
            OutcomeNativeId(outcome="draw", native_id="713856789012345678902"),
            OutcomeNativeId(outcome="away", native_id="713856789012345678903"),
        ],
        required_outcomes=["home", "draw", "away"],
    )
    engine = CataloguePriceEngine(paper_scan=_PairRecorder())
    runtime = PriceEngineRuntimeItem(identity=identity)
    result = PriceEngineSliceResult()
    prepared = engine._prepare_exact_id_pricing(runtime, result, lane="background")
    assert prepared.matchbook_ready is True
    assert prepared.kalshi_ready is True
    assert prepared.polymarket_ready is True
    ready = [
        name
        for name, flag in (
            (VenueName.MATCHBOOK.value, prepared.matchbook_ready),
            (VenueName.KALSHI.value, prepared.kalshi_ready),
            (VenueName.POLYMARKET.value, prepared.polymarket_ready),
        )
        if flag
    ]
    assert ready == ["matchbook", "kalshi", "polymarket"]

    recorded: list[tuple[str, str]] = []

    async def complete(runtime_item, *, matchbook, kalshi_books, result):
        del runtime_item, matchbook, kalshi_books, result
        recorded.append((VenueName.MATCHBOOK.value, VenueName.KALSHI.value))
        return PriceEngineItemStatus.EVALUATED

    engine._evaluate_complete_item = complete
    engine._build_matchbook_obs = lambda *args, **kwargs: SimpleNamespace(venue=VenueName.MATCHBOOK)
    engine._build_kalshi_obs = lambda *args, **kwargs: SimpleNamespace(venue=VenueName.KALSHI)
    engine._build_polymarket_obs = lambda *args, **kwargs: SimpleNamespace(venue=VenueName.POLYMARKET)
    payload = RetrievedVenuePayload(payload={}, retrieved_at=NOW)

    async def publish() -> None:
        await engine._publish_loaded_books(
            runtime,
            result,
            matchbook=payload,
            kalshi_books={"KXUEFANLGAME-26SEP26ENGESP": payload},
            polymarket_books={"713856789012345678901": payload},
        )

    import asyncio

    asyncio.run(publish())
    assert recorded == [(VenueName.MATCHBOOK.value, VenueName.KALSHI.value)]
    assert engine.paper_scan.pairs == [
        (VenueName.MATCHBOOK.value, VenueName.POLYMARKET.value),
        (VenueName.POLYMARKET.value, VenueName.KALSHI.value),
    ]


class _PairRecorder:
    def __init__(self) -> None:
        self.pairs: list[tuple[str, str]] = []

    def scan_pair(self, left, right, **kwargs):
        del kwargs
        self.pairs.append((left.venue.value, right.venue.value))

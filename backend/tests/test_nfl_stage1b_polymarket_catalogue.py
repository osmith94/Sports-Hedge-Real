"""NFL Stage 1B review blockers: durable Polymarket IDs, no synthetic tokens.

PAPER / read-only. Fixture/demo providers. Not owner-live quotes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sports_hedge.application.approved_market_catalogue import (
    OutcomeNativeId,
    derived_price_engine_working_set,
    executable_polymarket_token_ids,
)
from sports_hedge.application.catalogue_maintenance import (
    persist_universe_catalogue_pass,
    pair_identity_from_markets,
)
from sports_hedge.application.price_engine import (
    CataloguePriceEngine,
    PriceEngineItemStatus,
    PriceEngineSliceResult,
)
from sports_hedge.application.provider_access import ProviderAccessLayer, reset_shared_provider_access
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.nfl.constants import CANONICAL_NFL_GAME_WINNER, NFL_SPORT
from sports_hedge.nfl.normalize import is_fabricated_polymarket_clob_token
from sports_hedge.nfl.settlement import nfl_paper_settlement
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from test_issue344_price_engine import (
    FakeKalshi,
    FakeMatchbook,
    StubPaperScan,
    _qualifying_decision,
    _row,
)

NOW = datetime(2026, 9, 20, 21, 0, tzinfo=UTC)
KICKOFF = NOW + timedelta(hours=3)
PM_TOKENS = (
    "101mon000111222333444555666777888999000111222333",
    "202mon000111222333444555666777888999000111222333",
)


@pytest.fixture(autouse=True)
def _reset_shared_provider() -> Any:
    reset_shared_provider_access()
    yield
    reset_shared_provider_access()


def _event(venue: VenueName, source_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        sport=NFL_SPORT,
        competition="NFL",
        home_team="kansas city chiefs",
        away_team="indianapolis colts",
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source_id,
    )


def _game_winner(
    venue: VenueName, event_id: str, market_id: str, runners: list[tuple[str, str]]
) -> CanonicalMarket:
    event = _event(venue, event_id)
    return CanonicalMarket(
        event=event,
        source_venue=venue,
        source_market_id=market_id,
        family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        line=None,
        settlement=nfl_paper_settlement(family=MarketFamily.GAME_WINNER),
        runners=[
            CanonicalRunner(
                source_runner_id=native,
                outcome=CanonicalOutcome(outcome),
                label=outcome,
            )
            for outcome, native in runners
        ],
    )


class FakePolymarket:
    def __init__(self) -> None:
        self.book_calls: list[tuple[str, str, str]] = []
        self.payloads: dict[str, dict[str, Any]] = {}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del filters
        token = str(outcome_id or "")
        self.book_calls.append((str(event_id), str(market_id), token))
        if token in self.payloads:
            return self.payloads[token]
        return {"bids": [], "asks": []}


def test_fabricated_condition_index_tokens_are_rejected() -> None:
    condition = "0xabc"
    assert is_fabricated_polymarket_clob_token(f"{condition}:0", condition_id=condition) is True
    assert is_fabricated_polymarket_clob_token(PM_TOKENS[0], condition_id=condition) is False
    assert (
        executable_polymarket_token_ids(
            [
                OutcomeNativeId(outcome="home", native_id=f"{condition}:0"),
                OutcomeNativeId(outcome="away", native_id=f"{condition}:1"),
            ],
            event_id="827222",
            market_id="3482783",
            condition_id=condition,
            required_outcomes=["home", "away"],
        )
        == []
    )


def test_catalogue_merges_polymarket_exact_ids_onto_registered_row() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        matchbook = _game_winner(
            VenueName.MATCHBOOK,
            "33306877354500023",
            "33306877358600023",
            [("home", "33306877359000023"), ("away", "33306877359300023")],
        )
        kalshi = _game_winner(
            VenueName.KALSHI,
            "KXNFLGAME-26SEP20INDKC",
            "KXNFLGAME-26SEP20INDKC:game_winner",
            [
                ("home", "KXNFLGAME-26SEP20INDKC-KC:YES"),
                ("away", "KXNFLGAME-26SEP20INDKC-IND:YES"),
            ],
        )
        polymarket = _game_winner(
            VenueName.POLYMARKET,
            "827222",
            "3482783",
            [("away", PM_TOKENS[0]), ("home", PM_TOKENS[1])],
        )
        mb_k = pair_identity_from_markets(
            matchbook,
            kalshi,
            kalshi_event_payload={
                "event_ticker": "KXNFLGAME-26SEP20INDKC",
                "series_ticker": "KXNFLGAME",
            },
            kalshi_series_payload={"ticker": "KXNFLGAME"},
        )
        mb_pm = pair_identity_from_markets(matchbook, polymarket)
        assert mb_k is not None
        assert mb_pm is not None
        persist_universe_catalogue_pass(
            store,
            canonical_event_id="evt-indkc",
            competition="NFL",
            home_canonical="kansas city chiefs",
            away_canonical="indianapolis colts",
            kickoff_utc=KICKOFF,
            pairs=[mb_k],
            now=NOW,
            generation_id="g1",
            family_discovery=None,
            terminal=False,
            allow_disappearance=False,
        )
        persist_universe_catalogue_pass(
            store,
            canonical_event_id="evt-indkc",
            competition="NFL",
            home_canonical="kansas city chiefs",
            away_canonical="indianapolis colts",
            kickoff_utc=KICKOFF,
            pairs=[mb_pm],
            now=NOW,
            generation_id="g2",
            family_discovery=None,
            terminal=False,
            allow_disappearance=False,
        )
        row = store.get_active("evt-indkc", CANONICAL_NFL_GAME_WINNER)
        assert row is not None
        assert row.matchbook_market_id == "33306877358600023"
        assert row.kalshi_event_ticker == "KXNFLGAME-26SEP20INDKC"
        assert row.polymarket_event_id == "827222"
        assert row.polymarket_market_id == "3482783"
        assert {item.native_id for item in row.polymarket_token_ids} == set(PM_TOKENS)
        working = derived_price_engine_working_set([row])
        assert working[0].polymarket_market_id == "3482783"
        assert {item.native_id for item in working[0].polymarket_token_ids} == set(PM_TOKENS)
    finally:
        store.close()


def test_missing_polymarket_tokens_are_not_catalogued() -> None:
    matchbook = _game_winner(
        VenueName.MATCHBOOK,
        "33306877354500023",
        "33306877358600023",
        [("home", "1"), ("away", "2")],
    )
    polymarket = _game_winner(
        VenueName.POLYMARKET,
        "827222",
        "3482783",
        [
            ("away", "0xdead:0"),
            ("home", "0xdead:1"),
        ],
    )
    assert pair_identity_from_markets(matchbook, polymarket) is None


@pytest.mark.asyncio
async def test_price_engine_reprices_real_polymarket_tokens_without_inventing_ids() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    row = _row(suffix="pm1")
    row = row.model_copy(
        update={
            "polymarket_event_id": "827222",
            "polymarket_market_id": "3482783",
            "polymarket_condition_id": "0x0ad30faec3cd25a8ee81a919c4241ef9ec076882435a0b23205cd2b31bf32e70",
            "polymarket_token_ids": [
                OutcomeNativeId(outcome="yes", native_id=PM_TOKENS[0]),
                OutcomeNativeId(outcome="no", native_id=PM_TOKENS[1]),
            ],
        }
    )
    store.upsert_catalogue_row(row)
    matchbook = FakeMatchbook()
    kalshi = FakeKalshi()
    polymarket = FakePolymarket()
    scan = StubPaperScan(_qualifying_decision())
    engine = CataloguePriceEngine(
        catalogue_store=store,
        matchbook=matchbook,
        kalshi=kalshi,
        polymarket=polymarket,
        paper_scan=scan,
        clock=lambda: NOW,
        provider_access=ProviderAccessLayer(
            limits={VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
        ),
    )
    try:
        engine.reconstruct()
        runtime = engine.item(row.catalogue_row_id)
        assert runtime is not None
        status = await engine._price_item(runtime, PriceEngineSliceResult())
        assert status is PriceEngineItemStatus.EVALUATED
        called_tokens = {item[2] for item in polymarket.book_calls}
        assert called_tokens == set(PM_TOKENS)
        assert all(":0" not in token and ":1" not in token for token in called_tokens)
        assert scan.calls >= 1
    finally:
        store.close()


@pytest.mark.asyncio
async def test_synthetic_polymarket_tokens_are_ignored_for_scheduled_pricing() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    condition = "0x0ad30faec3cd25a8ee81a919c4241ef9ec076882435a0b23205cd2b31bf32e70"
    row = _row(suffix="pm-fake")
    row = row.model_copy(
        update={
            "polymarket_event_id": "827222",
            "polymarket_market_id": "3482783",
            "polymarket_condition_id": condition,
            "polymarket_token_ids": [
                OutcomeNativeId(outcome="yes", native_id=f"{condition}:0"),
                OutcomeNativeId(outcome="no", native_id=f"{condition}:1"),
            ],
        }
    )
    store.upsert_catalogue_row(row)
    polymarket = FakePolymarket()
    engine = CataloguePriceEngine(
        catalogue_store=store,
        matchbook=FakeMatchbook(),
        kalshi=FakeKalshi(),
        polymarket=polymarket,
        paper_scan=StubPaperScan(_qualifying_decision()),
        clock=lambda: NOW,
        provider_access=ProviderAccessLayer(
            limits={VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
        ),
    )
    try:
        engine.reconstruct()
        runtime = engine.item(row.catalogue_row_id)
        assert runtime is not None
        status = await engine._price_item(runtime, PriceEngineSliceResult())
        assert status is PriceEngineItemStatus.EVALUATED
        assert polymarket.book_calls == []
    finally:
        store.close()

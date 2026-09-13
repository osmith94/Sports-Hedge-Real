"""Labelled DEMO / FIXTURE REPLAY market payloads.

These observations exercise the same allocator → autofill → treasury →
close path as live paper scans. They must never be presented as live
venue quotes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import (
    CostKnownStatus,
    FeeBasis,
    FeeScope,
    MarketAction,
    OrderRole,
    VenueCostSnapshot,
)
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.unwind.models import ReverseQuote

DEMO_FIXTURE_LABEL = "DEMO / FIXTURE REPLAY"
DEMO_DATA_KIND = "demo_fixture_replay"
LIVE_DATA_KIND = "live_paper"

KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
FTTS_KICKOFF = datetime(2026, 9, 12, 18, 45, tzinfo=UTC)
FTTS_OBSERVED = datetime(2026, 9, 12, 16, 45, tzinfo=UTC)

DEMO_FX = [
    FxRateSnapshot(
        currency="USD",
        gbp_per_unit=Decimal("0.80"),
        spread_bps=Decimal("0"),
        source="paper_demo_fx_snapshot",
    )
]

KALSHI_EVENT = {
    "event_ticker": "KXEPLGAME-26SEP20NEWCHE",
    "series_ticker": "KXEPLGAME",
    "title": "Newcastle United vs Chelsea",
    "category": "Sports",
    "strike_date": KICKOFF.isoformat(),
}

KALSHI_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}

VenuePair = Literal["matchbook_polymarket", "matchbook_kalshi", "polymarket_kalshi"]
SolverKind = Literal["simple", "generalized"]


def matchbook_btts_payloads() -> tuple[dict, dict]:
    event = {
        "id": 1001,
        "name": "Newcastle United vs Chelsea",
        "start": KICKOFF.isoformat(),
        "competition-name": "Premier League",
    }
    market = {
        "id": 2001,
        "name": "Both Teams To Score",
        "runners": [
            {
                "id": 301,
                "name": "Yes",
                "prices": [
                    {"side": "back", "odds": "2.20", "available-amount": "40"},
                    {"side": "back", "odds": "2.10", "available-amount": "80"},
                    {"side": "lay", "odds": "2.22", "available-amount": "120"},
                ],
            },
            {
                "id": 302,
                "name": "No",
                "prices": [
                    {"side": "back", "odds": "1.80", "available-amount": "100"},
                    {"side": "lay", "odds": "1.82", "available-amount": "100"},
                ],
            },
        ],
    }
    return event, market


def polymarket_btts_payloads() -> tuple[dict, dict, dict[str, dict]]:
    event = {
        "id": "pm-event-1",
        "title": "Newcastle United vs Chelsea",
        "startTime": KICKOFF.isoformat(),
        "competition": "Premier League",
    }
    market = {
        "id": "pm-market-1",
        "question": "Both teams to score?",
        "sportsMarketType": "both teams to score",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "description": "Resolves based on 90 minutes of regulation time.",
    }
    books = {
        "yes-token": {
            "asset_id": "yes-token",
            "bids": [{"price": "0.49", "size": "250"}],
            "asks": [{"price": "0.51", "size": "250"}],
        },
        "no-token": {
            "asset_id": "no-token",
            "bids": [{"price": "0.41", "size": "300"}],
            "asks": [
                {"price": "0.43", "size": "160"},
                {"price": "0.45", "size": "180"},
            ],
        },
    }
    return event, market, books


def matchbook_btts() -> VenueMarketObservation:
    event, market = matchbook_btts_payloads()
    return MatchbookObservationBuilder().build(event, market, observed_at=OBSERVED, quote_age_ms=120)


def polymarket_btts() -> VenueMarketObservation:
    event, market, books = polymarket_btts_payloads()
    return PolymarketObservationBuilder().build(
        event, market, books, observed_at=OBSERVED, quote_age_ms=180
    )


def kalshi_btts() -> VenueMarketObservation:
    market = {
        "ticker": "KXEPLGAME-26SEP20NEWCHE-BTTS",
        "event_ticker": KALSHI_EVENT["event_ticker"],
        "title": "Both Teams To Score",
        "yes_sub_title": "Yes",
        "rules_primary": "Resolves on 90 minutes of regulation time. Extra time and penalties do not count.",
    }
    book = {
        "orderbook_fp": {
            "yes_dollars": [["0.20", "500.00"]],
            "no_dollars": [["0.70", "500.00"]],
        }
    }
    return KalshiObservationBuilder().build(
        KALSHI_EVENT,
        market,
        {market["ticker"]: book},
        series=KALSHI_SERIES,
        observed_at=OBSERVED,
        quote_age_ms=80,
        quote_age_basis="retrieval",
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )


def matchbook_ftts() -> VenueMarketObservation:
    event = {
        "id": 7001,
        "name": "Tottenham vs Everton",
        "start": FTTS_KICKOFF.isoformat(),
        "competition-name": "Premier League",
    }
    market = {
        "id": 9601,
        "name": "First Team To Score",
        "runners": [
            {"id": 1, "name": "Tottenham", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
            {"id": 2, "name": "Everton", "prices": [{"side": "back", "odds": "3.50", "available-amount": "80"}]},
            {"id": 3, "name": "No Goal", "prices": [{"side": "back", "odds": "4.50", "available-amount": "80"}]},
        ],
    }
    return MatchbookObservationBuilder().build(event, market, observed_at=FTTS_OBSERVED, quote_age_ms=120)


def polymarket_ftts() -> VenueMarketObservation:
    event = {
        "id": "pm-tot-eve-step7",
        "title": "Tottenham vs Everton",
        "startTime": FTTS_KICKOFF.isoformat(),
        "competition": "Premier League",
    }
    market = {
        "id": "pm-ftts-8b",
        "question": "First team to score",
        "sportsMarketType": "first team to score",
        "outcomes": '["Tottenham", "Everton", "No Goal"]',
        "clobTokenIds": '["h", "a", "n"]',
        "description": "Resolves based on 90 minutes of regulation time.",
    }
    books = {
        "h": {"asset_id": "h", "asks": [{"price": "0.28", "size": "200"}], "bids": [{"price": "0.26", "size": "200"}]},
        "a": {"asset_id": "a", "asks": [{"price": "0.28", "size": "200"}], "bids": [{"price": "0.26", "size": "200"}]},
        "n": {"asset_id": "n", "asks": [{"price": "0.22", "size": "200"}], "bids": [{"price": "0.20", "size": "200"}]},
    }
    return PolymarketObservationBuilder().build(
        event, market, books, observed_at=FTTS_OBSERVED, quote_age_ms=150
    )


def fixture_pair(
    venue_pair: VenuePair,
    *,
    solver: SolverKind = "simple",
) -> tuple[VenueMarketObservation, VenueMarketObservation]:
    if solver == "generalized":
        if venue_pair != "matchbook_polymarket":
            raise ValueError("generalized fixture replay is Matchbook↔Polymarket first-team-to-score only")
        return matchbook_ftts(), polymarket_ftts()
    if venue_pair == "matchbook_polymarket":
        return matchbook_btts(), polymarket_btts()
    if venue_pair == "matchbook_kalshi":
        return matchbook_btts(), kalshi_btts()
    if venue_pair == "polymarket_kalshi":
        return polymarket_btts(), kalshi_btts()
    raise ValueError(f"unsupported venue pair: {venue_pair}")


def fixture_venue_costs(
    left: VenueMarketObservation,
    right: VenueMarketObservation,
    *,
    captured_at: datetime | None = None,
) -> list[VenueCostSnapshot]:
    captured = captured_at or datetime.now(UTC)
    costs: list[VenueCostSnapshot] = []
    for observation in (left, right):
        costs.append(_opening_cost(observation.venue, captured_at=captured))
    return costs


def reverse_quotes_from_observations(
    observations: list[VenueMarketObservation],
    *,
    quoted_at: datetime | None = None,
) -> list[ReverseQuote]:
    """Build 8D reverse-side quotes from the same labelled fixture books.

    Kalshi SELL fees are not modelled in Phase 1; those quotes carry an
    explicit unknown closing cost and fail closed rather than inventing a fee.
    """

    when = quoted_at or datetime.now(UTC)
    quotes: list[ReverseQuote] = []
    for observation in observations:
        fingerprint = observation.market.settlement.deterministic_key()
        for book in observation.outcome_books:
            if not book.lay_levels:
                continue
            quotes.append(
                ReverseQuote(
                    venue=observation.venue,
                    source_event_id=str(observation.market.event.source_event_id),
                    source_market_id=observation.market.source_market_id,
                    source_runner_id=book.source_runner_id,
                    canonical_outcome=book.outcome.value,
                    settlement_fingerprint_key=fingerprint,
                    native_currency=observation.native_currency,
                    levels=list(book.lay_levels),
                    quote_age_ms=observation.quote_age_ms,
                    quote_age_basis=str(observation.metadata.get("quote_age_basis") or "retrieval"),
                    quoted_at=when,
                    closing_cost=_closing_cost(observation.venue, captured_at=when),
                )
            )
    return quotes


def _opening_cost(venue: VenueName, *, captured_at: datetime) -> VenueCostSnapshot:
    if venue is VenueName.MATCHBOOK:
        return VenueCostSnapshot.per_quote_profit_commission(
            venue,
            Decimal("0.02"),
            action=MarketAction.BACK,
            source="demo_fixture_replay",
            captured_at=captured_at,
            currency="GBP",
            detail="DEMO / FIXTURE REPLAY Matchbook opening commission",
        )
    if venue is VenueName.POLYMARKET:
        return VenueCostSnapshot.per_quote_profit_commission(
            venue,
            Decimal("0"),
            action=MarketAction.BUY,
            source="demo_fixture_replay",
            captured_at=captured_at,
            currency="USD",
            detail="assumed_zero DEMO / FIXTURE REPLAY Polymarket opening cost; not a verified venue fee",
        )
    if venue is VenueName.KALSHI:
        return kalshi_cost_from_series(KALSHI_SERIES, captured_at=captured_at)
    raise ValueError(f"unsupported demo venue: {venue}")


def _closing_cost(venue: VenueName, *, captured_at: datetime) -> VenueCostSnapshot:
    if venue is VenueName.MATCHBOOK:
        return VenueCostSnapshot.per_quote_profit_commission(
            venue,
            Decimal("0.02"),
            action=MarketAction.LAY,
            source="demo_fixture_replay",
            captured_at=captured_at,
            currency="GBP",
            detail="DEMO / FIXTURE REPLAY Matchbook closing lay commission",
        )
    if venue is VenueName.POLYMARKET:
        return VenueCostSnapshot(
            venue=venue,
            action=MarketAction.SELL,
            fee_basis=FeeBasis.NONE_CONFIRMED,
            known_status=CostKnownStatus.KNOWN,
            captured_at=captured_at,
            source="demo_fixture_replay",
            order_role=OrderRole.NOT_APPLICABLE,
            fee_scope=FeeScope.PER_QUOTE,
            currency="USD",
            detail="DEMO / FIXTURE REPLAY Polymarket closing sell; none_confirmed",
        )
    return VenueCostSnapshot(
        venue=venue,
        action=MarketAction.SELL,
        fee_basis=FeeBasis.UNKNOWN,
        known_status=CostKnownStatus.UNKNOWN,
        captured_at=captured_at,
        source="demo_fixture_replay",
        currency="USD",
        detail="Kalshi SELL close fees are not modelled; unwind fails closed",
    )

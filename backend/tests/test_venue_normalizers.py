from __future__ import annotations

from decimal import Decimal

import pytest

from sports_hedge.domain.football import (
    CanonicalOutcome,
    MarketFamily,
    SettlementScope,
)
from sports_hedge.normalization.venues import (
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
)


MATCHBOOK_EVENT = {
    "id": 1001,
    "name": "Newcastle United vs Arsenal",
    "start": "2026-09-20T15:00:00Z",
    "meta-tags": [{"type": "COMPETITION", "name": "Premier League"}],
}


POLYMARKET_EVENT = {
    "id": "poly-event-1",
    "title": "Newcastle United vs. Arsenal",
    "startDate": "2026-09-20T15:00:00Z",
    "series": [{"title": "Premier League"}],
}


def test_matchbook_normalizes_event_and_match_result_market() -> None:
    normalizer = MatchbookNormalizer()
    event = normalizer.normalize_event(MATCHBOOK_EVENT)
    market = normalizer.normalize_market(
        event,
        {
            "id": 2001,
            "name": "Match Odds",
            "runners": [
                {"id": 1, "name": "Newcastle United"},
                {"id": 2, "name": "Draw"},
                {"id": 3, "name": "Arsenal"},
            ],
        },
    )

    assert event.competition == "Premier League"
    assert event.home_team == "Newcastle United"
    assert event.away_team == "Arsenal"
    assert market.family == MarketFamily.MATCH_RESULT
    assert market.settlement.scope == SettlementScope.REGULATION_TIME
    assert market.settlement.extra_time_included is False
    assert [runner.outcome for runner in market.runners] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    ]


def test_matchbook_total_goals_preserves_line_and_push_semantics() -> None:
    normalizer = MatchbookNormalizer()
    event = normalizer.normalize_event(MATCHBOOK_EVENT)
    market = normalizer.normalize_market(
        event,
        {
            "id": 2002,
            "name": "Over/Under 2.5 Goals",
            "runners": [
                {"id": 10, "name": "Over 2.5"},
                {"id": 11, "name": "Under 2.5"},
            ],
        },
    )

    assert market.family == MarketFamily.TOTAL_GOALS
    assert market.line == Decimal("2.5")
    assert market.settlement.push_possible is False
    assert [runner.outcome for runner in market.runners] == [
        CanonicalOutcome.OVER,
        CanonicalOutcome.UNDER,
    ]


def test_matchbook_corners_and_cards_are_supported_for_market_intelligence() -> None:
    normalizer = MatchbookNormalizer()
    event = normalizer.normalize_event(MATCHBOOK_EVENT)

    corners = normalizer.normalize_market(
        event,
        {
            "id": 2003,
            "name": "Over/Under 10.5 Corners",
            "runners": [
                {"id": 20, "name": "Over 10.5"},
                {"id": 21, "name": "Under 10.5"},
            ],
        },
    )
    cards = normalizer.normalize_market(
        event,
        {
            "id": 2004,
            "name": "Over/Under 4.5 Cards",
            "runners": [
                {"id": 30, "name": "Over 4.5"},
                {"id": 31, "name": "Under 4.5"},
            ],
        },
    )

    assert corners.family == MarketFamily.CORNERS
    assert corners.line == Decimal("10.5")
    assert cards.family == MarketFamily.CARDS
    assert cards.line == Decimal("4.5")


def test_matchbook_rejects_ambiguous_fixture_or_unsupported_market() -> None:
    normalizer = MatchbookNormalizer()
    with pytest.raises(VenueNormalizationError, match="Cannot safely split"):
        normalizer.normalize_event({**MATCHBOOK_EVENT, "name": "Newcastle United Arsenal"})

    event = normalizer.normalize_event(MATCHBOOK_EVENT)
    with pytest.raises(VenueNormalizationError, match="Unsupported Matchbook market"):
        normalizer.normalize_market(
            event,
            {"id": 9999, "name": "Novelty Special", "runners": [{"id": 1, "name": "Yes"}]},
        )


def test_polymarket_normalizes_public_binary_contract_but_preserves_yes_no() -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(POLYMARKET_EVENT)
    market = normalizer.normalize_market(
        event,
        {
            "id": "market-1",
            "question": "Will Newcastle United win?",
            "sportsMarketType": "moneyline",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes-token", "no-token"]',
            "description": "This market resolves from the result after 90 minutes plus stoppage time.",
        },
    )

    assert event.competition == "Premier League"
    assert market.family == MarketFamily.MATCH_RESULT
    assert market.settlement.scope == SettlementScope.REGULATION_TIME
    assert [runner.source_runner_id for runner in market.runners] == ["yes-token", "no-token"]
    assert [runner.outcome for runner in market.runners] == [
        CanonicalOutcome.YES,
        CanonicalOutcome.NO,
    ]


def test_polymarket_corners_classifies_for_history_without_guessing_settlement() -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(POLYMARKET_EVENT)
    market = normalizer.normalize_market(
        event,
        {
            "id": "market-corners",
            "question": "Will there be over 9.5 corners?",
            "sportsMarketType": "totals",
            "outcomes": ["Yes", "No"],
            "clobTokenIds": ["over-ish", "under-ish"],
        },
    )

    assert market.family == MarketFamily.CORNERS
    assert market.line == Decimal("9.5")
    assert market.settlement.scope == SettlementScope.UNKNOWN
    assert market.confidence == 0.75


def test_polymarket_rejects_mismatched_outcomes_and_token_ids() -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(POLYMARKET_EVENT)
    with pytest.raises(VenueNormalizationError, match="outcome/token lengths differ"):
        normalizer.normalize_market(
            event,
            {
                "id": "bad-market",
                "question": "Will Newcastle United win?",
                "sportsMarketType": "moneyline",
                "outcomes": ["Yes", "No"],
                "clobTokenIds": ["only-one-token"],
            },
        )

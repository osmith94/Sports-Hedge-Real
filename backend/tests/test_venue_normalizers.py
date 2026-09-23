from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.domain.football import (
    CanonicalOutcome,
    MarketFamily,
    SettlementScope,
)
from sports_hedge.matching.events import EventMatcher
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
)

PINNED_GAMMA_934146 = (
    Path(__file__).resolve().parent / "fixtures" / "polymarket_gamma_event_934146_chelsea_hull.json"
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
    "startTime": "2026-09-20T15:00:00Z",
    "startDate": "2026-08-01T04:00:12Z",
    "endDate": "2026-09-20T15:00:00Z",
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


def test_matchbook_live_named_total_is_full_match_total_goals() -> None:
    normalizer = MatchbookNormalizer()
    event = normalizer.normalize_event(MATCHBOOK_EVENT)
    market = normalizer.normalize_market(
        event,
        {
            "id": 2008,
            "name": "Total",
            "market-type": "point-total",
            "runners": [
                {"id": 10, "name": "Over 2.5"},
                {"id": 11, "name": "Under 2.5"},
            ],
        },
    )

    assert market.family == MarketFamily.TOTAL_GOALS
    assert market.line == Decimal("2.5")
    assert market.period.value == "full_time"


def test_matchbook_named_team_total_is_not_full_match_total_goals() -> None:
    normalizer = MatchbookNormalizer()
    event = normalizer.normalize_event(MATCHBOOK_EVENT)
    market = normalizer.normalize_market(
        event,
        {
            "id": 2005,
            "name": "Newcastle United Over/Under 2.5 Goals",
            "market-type": "other",
            "runners": [
                {"id": 10, "name": "Over 2.5"},
                {"id": 11, "name": "Under 2.5"},
            ],
        },
    )
    assert market.family == MarketFamily.TEAM_TOTAL
    assert market.line == Decimal("2.5")


def test_matchbook_participant_id_total_is_not_full_match_total_goals() -> None:
    normalizer = MatchbookNormalizer()
    event = normalizer.normalize_event(MATCHBOOK_EVENT)
    market = normalizer.normalize_market(
        event,
        {
            "id": 34328274317601081,
            "name": "Over/Under 2.5 Goals",
            "market-type": "other",
            "event-participant-id": 34213468549700100,
            "runners": [
                {
                    "id": 1,
                    "name": "Over 2.5",
                    "event-participant-id": 34213468549700100,
                },
                {
                    "id": 2,
                    "name": "Under 2.5",
                    "event-participant-id": 34213468549700100,
                },
            ],
        },
    )
    assert market.family == MarketFamily.TEAM_TOTAL
    assert market.source_market_id == "34328274317601081"


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
    assert event.kickoff_utc == datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


def test_polymarket_pinned_gamma_934146_uses_fixture_start_not_listing_or_deadline() -> None:
    """PINNED RAW Gamma event 934146 Chelsea FC vs. Hull City AFC — kickoff regression."""

    fixture = json.loads(PINNED_GAMMA_934146.read_text(encoding="utf-8"))
    assert fixture["fixture_id"] == "polymarket-gamma-event-934146-chelsea-hull"
    assert fixture["source"] == "https://gamma-api.polymarket.com/events/934146"
    assert fixture["retrieved_at"] == "2026-09-12T14:36:20Z"
    payload = fixture["payload"]
    assert payload["startDate"] == "2026-08-30T04:00:12Z"
    assert payload["endDate"] == "2026-09-12T14:00:00Z"
    assert payload["startTime"] == "2026-09-12T14:00:00Z"

    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(payload)
    market = normalizer.normalize_market(event, payload["markets"][0])

    assert event.source_event_id == "934146"
    assert event.home_team == "Chelsea FC"
    assert event.away_team == "Hull City AFC"
    assert event.kickoff_utc == datetime(2026, 9, 12, 14, 0, tzinfo=UTC)
    assert [runner.outcome for runner in market.runners] == [
        CanonicalOutcome.YES,
        CanonicalOutcome.NO,
    ]


@pytest.mark.parametrize(
    ("event_id", "title", "start_time"),
    [
        ("934152", "Liverpool FC vs. Fulham FC", "2026-09-12T14:00:00Z"),
        ("934157", "AFC Bournemouth vs. Brentford FC", "2026-09-12T14:00:00Z"),
    ],
)
def test_polymarket_observed_epl_ids_ignore_listing_startdate(
    event_id: str,
    title: str,
    start_time: str,
) -> None:
    event = PolymarketNormalizer().normalize_event(
        {
            "id": event_id,
            "title": title,
            "startDate": "2026-08-30T04:00:12Z",
            "startTime": start_time,
            "endDate": start_time,
            "eventDate": "2026-09-12",
            "series": [{"title": "Premier League"}],
        }
    )
    assert event.kickoff_utc == datetime(2026, 9, 12, 14, 0, tzinfo=UTC)


def test_polymarket_unknown_or_date_only_fixture_time_stays_unmatched() -> None:
    normalizer = PolymarketNormalizer()
    listing_only = {
        **POLYMARKET_EVENT,
        "startTime": None,
        "startDate": "2026-08-30T04:00:12Z",
        "endDate": "2026-09-12T14:00:00Z",
        "eventDate": "2026-09-12",
    }
    listing_only.pop("startTime")
    with pytest.raises(VenueNormalizationError, match="no supported fixture start time"):
        normalizer.normalize_event(listing_only)

    with pytest.raises(VenueNormalizationError, match="date-only"):
        normalizer.normalize_event({**POLYMARKET_EVENT, "startTime": "2026-09-12"})


def test_polymarket_fixture_start_converts_aware_non_utc_and_keeps_raw_source() -> None:
    event = PolymarketNormalizer().normalize_event(
        {**POLYMARKET_EVENT, "startTime": "2026-09-12T10:00:00-04:00"}
    )
    assert event.kickoff_utc == datetime(2026, 9, 12, 14, 0, tzinfo=UTC)
    assert event.source_event_id == "poly-event-1"
    assert event.source_venue.value == "polymarket"


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


def test_fixture_title_strips_market_family_suffix_from_away_team() -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(
        {
            **POLYMARKET_EVENT,
            "title": "Leeds United vs. Leicester City - 1st Half Exact Score",
        }
    )
    assert event.home_team == "Leeds United"
    assert event.away_team == "Leicester City"

    more_markets = normalizer.normalize_event(
        {
            **POLYMARKET_EVENT,
            "title": "Leeds United vs. Leicester City - More Markets",
        }
    )
    assert more_markets.away_team == "Leicester City"

    kalshi = KalshiNormalizer().normalize_event(
        {
            "event_ticker": "KXEPLBTTS-26SEP20LEELEI",
            "title": "Leeds United vs Leicester City: BTTS",
            "strike_date": "2026-09-20T15:00:00Z",
            "product_metadata": {"competition": "EPL", "competition_scope": "Game"},
        }
    )
    assert kalshi.home_team == "Leeds United"
    assert kalshi.away_team == "Leicester City"
    match = EventMatcher().match(event, kalshi)
    assert match.matched is True


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


def _moneyline_payload(market_id: str, question: str, yes_token: str, no_token: str) -> dict:
    return {
        "id": market_id,
        "question": question,
        "sportsMarketType": "moneyline",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{yes_token}", "{no_token}"]',
        "description": "This market resolves from the result after 90 minutes plus stoppage time.",
    }


def test_lone_polymarket_moneyline_binary_is_not_assembled_into_three_way() -> None:
    from sports_hedge.normalization.venues import promote_polymarket_complete_match_result

    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(POLYMARKET_EVENT)
    payload = _moneyline_payload(
        "pm-home", "Will Newcastle United win on 2026-09-20?", "yes-home", "no-home"
    )
    market = normalizer.normalize_market(event, payload)
    promoted = promote_polymarket_complete_match_result([market], [payload])
    assert len(promoted) == 1
    assert [runner.outcome for runner in promoted[0].runners] == [
        CanonicalOutcome.YES,
        CanonicalOutcome.NO,
    ]


def test_polymarket_assembles_complete_home_draw_away_moneylines() -> None:
    from sports_hedge.normalization.venues import promote_polymarket_complete_match_result

    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(POLYMARKET_EVENT)
    payloads = [
        _moneyline_payload(
            "pm-home", "Will Newcastle United win on 2026-09-20?", "yes-home", "no-home"
        ),
        _moneyline_payload(
            "pm-draw", "Will the match be a draw on 2026-09-20?", "yes-draw", "no-draw"
        ),
        _moneyline_payload(
            "pm-away", "Will Arsenal win on 2026-09-20?", "yes-away", "no-away"
        ),
    ]
    markets = [normalizer.normalize_market(event, payload) for payload in payloads]
    promoted = promote_polymarket_complete_match_result(markets, payloads)
    assert len(promoted) == 1
    assert [runner.outcome for runner in promoted[0].runners] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    ]
    assert [runner.source_runner_id for runner in promoted[0].runners] == [
        "yes-home",
        "yes-draw",
        "yes-away",
    ]


def test_polymarket_moneyline_yes_outcome_fails_closed_on_ambiguous_titles() -> None:
    from sports_hedge.normalization.venues import polymarket_moneyline_yes_outcome

    assert (
        polymarket_moneyline_yes_outcome(
            "Will Newcastle United or Arsenal win?",
            home_team="Newcastle United",
            away_team="Arsenal",
        )
        is None
    )
    assert polymarket_moneyline_yes_outcome(
        "Will the match be a draw?",
        home_team="Newcastle United",
        away_team="Arsenal",
    ) is CanonicalOutcome.DRAW
    assert polymarket_moneyline_yes_outcome(
        "Will Newcastle United win?",
        home_team="Newcastle United",
        away_team="Arsenal",
    ) is CanonicalOutcome.HOME

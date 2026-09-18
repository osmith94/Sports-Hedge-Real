"""Issue #296: unsupported Matchbook exotic markets fail closed before observation.

Live-shaped runner structures from the 2026-09-18 owner PAPER scan. Deterministic
fixture data — not owner-live Matchbook, not historical quotes, not modelled
probabilities. PAPER MODE / execution disabled.

Do not weaken VenueMarketObservation uniqueness. Do not silently deduplicate
outcome_books. Do not remap compound runners onto HOME/DRAW/AWAY/YES/NO.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    OutcomeOrderBook,
    VenueMarketObservation,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_cycle_audit import (
    cycle_last_error,
    issue_is_provider_failure,
    issue_is_unsupported_market_skip,
)
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.normalization.venues import (
    MATCHBOOK_COMPOUND_FAMILY_REASON,
    MATCHBOOK_COMPOUND_RUNNER_REASON,
    MATCHBOOK_CORRECT_SCORE_BUCKET_REASON,
    MATCHBOOK_NON_UNIQUE_CANONICAL_REASON,
    MATCHBOOK_SLASH_COMPOUND_REASON,
    MatchbookNormalizer,
    VenueNormalizationError,
)
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
REGULATION = (
    "Resolves based on 90 minutes of regulation time. Extra time and penalties do not count."
)

WEST_HAM_EVENT = {
    "id": 296001,
    "name": "West Ham vs Chelsea",
    "start": KICKOFF.isoformat(),
    "competition-name": "Premier League",
}

BRIGHTON_EVENT = {
    "id": 296002,
    "name": "Brighton and Hove Albion vs Fulham",
    "start": KICKOFF.isoformat(),
    "competition-name": "Premier League",
}


def _back(odds: str = "2.10") -> dict[str, str]:
    return {"side": "back", "odds": odds, "available-amount": "50"}


def _runner(runner_id: int, name: str, odds: str = "2.10") -> dict[str, Any]:
    return {"id": runner_id, "name": name, "prices": [_back(odds)]}


# Owner-live runner shapes that collapsed onto CanonicalOutcome.OTHER.
_EXOTIC_CASES = (
    (
        "Correct Score",
        "correct_score",
        [
            _runner(1, "1-0"),
            _runner(2, "2-0"),
            _runner(3, "ANY OTHER DRAW"),
            _runner(4, "Any Other Home Win"),
        ],
        MarketFamily.CORRECT_SCORE,
        MATCHBOOK_NON_UNIQUE_CANONICAL_REASON,
        False,
    ),
    (
        "Half Time/Full Time",
        "ht_ft",
        [
            _runner(1, "WEST HAM/WEST HAM"),
            _runner(2, "WEST HAM/DRAW"),
            _runner(3, "WEST HAM/CHELSEA"),
            _runner(4, "DRAW/WEST HAM"),
        ],
        MarketFamily.HALF_TIME_FULL_TIME,
        MATCHBOOK_NON_UNIQUE_CANONICAL_REASON,
        False,
    ),
    (
        "Double Chance",
        "double_chance",
        [
            _runner(1, "Draw or West Ham"),
            _runner(2, "West Ham or Chelsea"),
            _runner(3, "Draw or Chelsea"),
        ],
        MarketFamily.DOUBLE_CHANCE,
        MATCHBOOK_NON_UNIQUE_CANONICAL_REASON,
        False,
    ),
    (
        "Result and Both Teams To Score",
        "other",
        [
            _runner(1, "West Ham and Yes"),
            _runner(2, "West Ham and No"),
            _runner(3, "Draw and Yes"),
            _runner(4, "Draw and No"),
            _runner(5, "Chelsea and Yes"),
            _runner(6, "Chelsea and No"),
        ],
        None,
        MATCHBOOK_COMPOUND_FAMILY_REASON,
        True,
    ),
    (
        "Match Odds and Both Teams To Score",
        "other",
        [
            _runner(1, "West Ham and No"),
            _runner(2, "West Ham and Yes"),
        ],
        None,
        MATCHBOOK_COMPOUND_FAMILY_REASON,
        True,
    ),
    (
        "Match Odds and Over/Under 3.5 Goals",
        "other",
        [
            _runner(1, "West Ham and Under 3.5"),
            _runner(2, "West Ham and Over 3.5"),
            _runner(3, "Draw and Under 3.5"),
            _runner(4, "Chelsea and Under 3.5"),
        ],
        None,
        MATCHBOOK_COMPOUND_FAMILY_REASON,
        True,
    ),
)


def test_observation_uniqueness_validator_is_not_weakened() -> None:
    event = MatchbookNormalizer().normalize_event(WEST_HAM_EVENT)
    market = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 1,
            "name": "Match Odds",
            "runners": [
                _runner(1, "West Ham"),
                _runner(2, "Draw"),
                _runner(3, "Chelsea"),
            ],
        },
    )
    books = [
        OutcomeOrderBook(
            outcome=CanonicalOutcome.HOME,
            source_runner_id="1",
            back_levels=[BookLevel(decimal_odds=Decimal("2.1"), available_stake=Decimal("10"))],
        ),
        OutcomeOrderBook(
            outcome=CanonicalOutcome.HOME,
            source_runner_id="2",
            back_levels=[BookLevel(decimal_odds=Decimal("3.1"), available_stake=Decimal("10"))],
        ),
    ]
    with pytest.raises(ValidationError, match="outcome_books must contain unique canonical outcomes"):
        VenueMarketObservation(
            market=market,
            observed_at=OBSERVED,
            native_currency="GBP",
            outcome_books=books,
        )


@pytest.mark.parametrize(
    ("name", "market_type", "runners", "family", "reason", "reject_at_normalize"),
    _EXOTIC_CASES,
)
def test_owner_live_exotic_shapes_fail_closed_before_uniqueness_validator(
    name: str,
    market_type: str,
    runners: list[dict[str, Any]],
    family: MarketFamily | None,
    reason: str,
    reject_at_normalize: bool,
) -> None:
    normalizer = MatchbookNormalizer()
    event = normalizer.normalize_event(WEST_HAM_EVENT)
    payload = {"id": 296100, "name": name, "market-type": market_type, "runners": runners}

    if reject_at_normalize:
        with pytest.raises(VenueNormalizationError, match=reason) as raised:
            normalizer.normalize_market(event, payload)
        detail = str(raised.value)
        assert "Unsupported Matchbook market:" in detail
        assert "outcome_books must contain unique canonical outcomes" not in detail
        assert f"[market_type={market_type}]" in detail
        with pytest.raises(VenueNormalizationError, match=reason):
            MatchbookObservationBuilder(normalizer).build(
                WEST_HAM_EVENT, payload, observed_at=OBSERVED
            )
        return

    market = normalizer.normalize_market(event, payload)
    assert market.family is family
    outcomes = [runner.outcome for runner in market.runners]
    assert len(outcomes) != len(set(outcomes))
    with pytest.raises(VenueNormalizationError, match=MATCHBOOK_NON_UNIQUE_CANONICAL_REASON) as raised:
        MatchbookObservationBuilder(normalizer).build(WEST_HAM_EVENT, payload, observed_at=OBSERVED)
    detail = str(raised.value)
    assert "Unsupported Matchbook market:" in detail
    assert "outcome_books must contain unique canonical outcomes" not in detail
    assert f"[market_type={market_type}]" in detail


def test_compound_btts_is_not_overclaimed_as_both_teams_to_score() -> None:
    event = MatchbookNormalizer().normalize_event(WEST_HAM_EVENT)
    with pytest.raises(VenueNormalizationError, match=MATCHBOOK_COMPOUND_FAMILY_REASON):
        MatchbookNormalizer().normalize_market(
            event,
            {
                "id": 296201,
                "name": "Result and Both Teams To Score",
                "runners": [
                    _runner(1, "West Ham and No"),
                    _runner(2, "Chelsea and Yes"),
                ],
            },
        )


def test_slash_and_bucket_runners_are_classified_as_exotic_not_1x2() -> None:
    event = MatchbookNormalizer().normalize_event(WEST_HAM_EVENT)
    htft = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 296202,
            "name": "Half Time/Full Time",
            "runners": [_runner(1, "WEST HAM/WEST HAM"), _runner(2, "DRAW/CHELSEA")],
        },
    )
    assert htft.family is MarketFamily.HALF_TIME_FULL_TIME
    assert {runner.outcome for runner in htft.runners} == {CanonicalOutcome.OTHER}

    correct = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 296203,
            "name": "Correct Score",
            "runners": [_runner(1, "ANY OTHER DRAW"), _runner(2, "1-1")],
        },
    )
    assert correct.family is MarketFamily.CORRECT_SCORE
    assert {runner.outcome for runner in correct.runners} == {CanonicalOutcome.OTHER}


def test_approved_live_equivalents_still_build_unique_observations() -> None:
    builder = MatchbookObservationBuilder()
    event = WEST_HAM_EVENT
    match_odds = builder.build(
        event,
        {
            "id": 296301,
            "name": "Match Odds",
            "runners": [
                _runner(1, "West Ham", "2.40"),
                _runner(2, "Draw", "3.40"),
                _runner(3, "Chelsea", "2.90"),
            ],
        },
        observed_at=OBSERVED,
    )
    assert [book.outcome for book in match_odds.outcome_books] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    ]

    btts = builder.build(
        event,
        {
            "id": 296302,
            "name": "Both Teams To Score",
            "runners": [_runner(1, "Yes", "1.90"), _runner(2, "No", "1.95")],
        },
        observed_at=OBSERVED,
    )
    assert [book.outcome for book in btts.outcome_books] == [
        CanonicalOutcome.YES,
        CanonicalOutcome.NO,
    ]

    totals = builder.build(
        event,
        {
            "id": 296303,
            "name": "Over/Under 2.5 Goals",
            "runners": [_runner(1, "Over 2.5", "1.83"), _runner(2, "Under 2.5", "2.10")],
        },
        observed_at=OBSERVED,
    )
    assert totals.market.family is MarketFamily.TOTAL_GOALS
    assert [book.outcome for book in totals.outcome_books] == [
        CanonicalOutcome.OVER,
        CanonicalOutcome.UNDER,
    ]

    ftts = builder.build(
        event,
        {
            "id": 296304,
            "name": "First Team To Score",
            "runners": [
                _runner(1, "West Ham", "2.20"),
                _runner(2, "Chelsea", "2.50"),
                _runner(3, "No Goal", "8.00"),
            ],
        },
        observed_at=OBSERVED,
    )
    assert [book.outcome for book in ftts.outcome_books] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.AWAY,
        CanonicalOutcome.NO_GOAL,
    ]

    dnb = builder.build(
        event,
        {
            "id": 296305,
            "name": "Draw No Bet",
            "runners": [_runner(1, "West Ham", "1.70"), _runner(2, "Chelsea", "2.20")],
        },
        observed_at=OBSERVED,
    )
    assert [book.outcome for book in dnb.outcome_books] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.AWAY,
    ]


def test_club_name_containing_and_is_not_treated_as_compound_exotic() -> None:
    observation = MatchbookObservationBuilder().build(
        BRIGHTON_EVENT,
        {
            "id": 296306,
            "name": "Match Odds",
            "runners": [
                _runner(1, "Brighton and Hove Albion", "1.90"),
                _runner(2, "Draw", "3.50"),
                _runner(3, "Fulham", "4.20"),
            ],
        },
        observed_at=OBSERVED,
    )
    assert [book.outcome for book in observation.outcome_books] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    ]


def test_token_double_chance_stays_inventory_visible_with_unique_mapping() -> None:
    event = MatchbookNormalizer().normalize_event(WEST_HAM_EVENT)
    market = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 296307,
            "name": "Double Chance",
            "runners": [
                _runner(1, "Home or Draw"),
                _runner(2, "Home or Away"),
                _runner(3, "Draw or Away"),
            ],
        },
    )
    assert market.family is MarketFamily.DOUBLE_CHANCE
    observation = MatchbookObservationBuilder().build(
        WEST_HAM_EVENT,
        {
            "id": 296307,
            "name": "Double Chance",
            "runners": [
                _runner(1, "Home or Draw"),
                _runner(2, "Home or Away"),
                _runner(3, "Draw or Away"),
            ],
        },
        observed_at=OBSERVED,
    )
    assert [book.outcome for book in observation.outcome_books] == [
        CanonicalOutcome.HOME_OR_DRAW,
        CanonicalOutcome.HOME_OR_AWAY,
        CanonicalOutcome.DRAW_OR_AWAY,
    ]


def test_generic_home_away_aliases_still_fail_closed_without_broadening() -> None:
    event = MatchbookNormalizer().normalize_event(WEST_HAM_EVENT)
    match_odds = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 296308,
            "name": "Match Odds",
            "runners": [
                {"id": 1, "name": "Home"},
                {"id": 2, "name": "Draw"},
                {"id": 3, "name": "Away"},
            ],
        },
    )
    assert [runner.outcome for runner in match_odds.runners] == [
        CanonicalOutcome.OTHER,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.OTHER,
    ]
    with pytest.raises(VenueNormalizationError, match=MATCHBOOK_NON_UNIQUE_CANONICAL_REASON):
        MatchbookObservationBuilder().build(
            WEST_HAM_EVENT,
            {
                "id": 296308,
                "name": "Match Odds",
                "runners": [
                    _runner(1, "Home"),
                    _runner(2, "Draw"),
                    _runner(3, "Away"),
                ],
            },
            observed_at=OBSERVED,
        )


def test_first_half_btts_is_not_a_compound_family() -> None:
    event = MatchbookNormalizer().normalize_event(WEST_HAM_EVENT)
    market = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 296309,
            "name": "1st Half Both Teams To Score",
            "runners": [_runner(1, "Yes"), _runner(2, "No")],
        },
    )
    assert market.family is MarketFamily.BOTH_TEAMS_TO_SCORE
    observation = MatchbookObservationBuilder().build(
        WEST_HAM_EVENT,
        {
            "id": 296309,
            "name": "1st Half Both Teams To Score",
            "runners": [_runner(1, "Yes"), _runner(2, "No")],
        },
        observed_at=OBSERVED,
    )
    assert [book.outcome for book in observation.outcome_books] == [
        CanonicalOutcome.YES,
        CanonicalOutcome.NO,
    ]


class _ExoticMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [WEST_HAM_EVENT]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {
            "markets": [
                {
                    "id": 296401,
                    "name": "Match Odds",
                    "runners": [
                        _runner(1, "West Ham", "2.40"),
                        _runner(2, "Draw", "3.40"),
                        _runner(3, "Chelsea", "2.90"),
                    ],
                },
                {
                    "id": 296402,
                    "name": "Both Teams To Score",
                    "runners": [_runner(11, "Yes", "1.90"), _runner(12, "No", "1.95")],
                },
                {
                    "id": 296403,
                    "name": "Correct Score",
                    "market-type": "correct_score",
                    "runners": [
                        _runner(21, "ANY OTHER DRAW"),
                        _runner(22, "1-0"),
                        _runner(23, "2-1"),
                    ],
                },
                {
                    "id": 296404,
                    "name": "Half Time/Full Time",
                    "market-type": "ht_ft",
                    "runners": [
                        _runner(31, "WEST HAM/WEST HAM"),
                        _runner(32, "CHELSEA/CHELSEA"),
                    ],
                },
                {
                    "id": 296405,
                    "name": "Result and Both Teams To Score",
                    "market-type": "other",
                    "runners": [
                        _runner(41, "West Ham and No"),
                        _runner(42, "Draw and Yes"),
                        _runner(43, "Chelsea and No"),
                    ],
                },
                {
                    "id": 296406,
                    "name": "Double Chance",
                    "market-type": "double_chance",
                    "runners": [
                        _runner(51, "Draw or West Ham"),
                        _runner(52, "West Ham or Chelsea"),
                        _runner(53, "Draw or Chelsea"),
                    ],
                },
            ]
        }


class _ExoticPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            {
                "id": "pm-whu-che",
                "title": "West Ham vs Chelsea",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
            }
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return [
            {
                "id": "pm-whu-che-1x2",
                "question": "Match result?",
                "sportsMarketType": "moneyline",
                "outcomes": '["West Ham", "Draw", "Chelsea"]',
                "clobTokenIds": '["h", "d", "a"]',
                "description": REGULATION,
            }
        ]

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        return {
            "timestamp": int(OBSERVED.timestamp() * 1000),
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.42", "size": "100"}],
            "asset_id": str(outcome_id),
        }


@pytest.mark.asyncio
async def test_live_scan_exotics_are_unsupported_skips_not_observation_validation_errors() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=_ExoticMatchbook(),
        polymarket=_ExoticPolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75")),
                FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1")),
            ],
            maximum_execution_risk=100,
        )
    finally:
        repository.close()

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

    fixture = report.discovered_fixtures[0]
    rows = report.fixture_markets[fixture.canonical_event_id]
    families = {row.family for row in rows if row.family}
    assert "match_result" in families
    assert "both_teams_to_score" in families
    assert "correct_score" in families
    assert "half_time_full_time" in families
    assert "double_chance" in families
    compound_btts = [
        row
        for row in rows
        if "Result and Both Teams To Score" in row.display_name
        or (row.matchbook and row.matchbook.raw_market_name == "Result and Both Teams To Score")
    ]
    assert compound_btts
    assert all(row.family is None for row in compound_btts)
    quoted_ids = {
        row.matchbook.source_market_id
        for row in rows
        if row.matchbook and row.matchbook.best_backs
    }
    assert "296401" in quoted_ids
    assert "296402" in quoted_ids
    assert "296403" not in quoted_ids
    assert "296404" not in quoted_ids
    assert "296405" not in quoted_ids
    assert "296406" not in quoted_ids


def test_scan_audit_treats_observation_unique_refusal_as_unsupported_skip() -> None:
    from sports_hedge.application.collector import CollectorIssue

    issue = CollectorIssue(
        stage="build_observation",
        venue=VenueName.MATCHBOOK,
        source_id="296403",
        detail=(
            "Unsupported Matchbook market: Correct Score "
            f"({MATCHBOOK_NON_UNIQUE_CANONICAL_REASON}) [market_type=correct_score]"
        ),
    )
    assert issue_is_unsupported_market_skip(issue)
    assert issue_is_provider_failure(issue) is False


def test_compound_runner_reason_tokens_are_precise() -> None:
    assert MATCHBOOK_COMPOUND_RUNNER_REASON == "compound_exotic_runners"
    assert MATCHBOOK_SLASH_COMPOUND_REASON == "slash_separated_compound_runner"
    assert MATCHBOOK_CORRECT_SCORE_BUCKET_REASON == "correct_score_bucket_runner"

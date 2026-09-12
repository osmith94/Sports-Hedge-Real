from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sports_hedge.domain.football import CanonicalOutcome, MarketFamily, SettlementScope
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.venues import MatchbookNormalizer, PolymarketNormalizer


FIXTURES = Path(__file__).resolve().parent / "fixtures"
MATCHBOOK = FIXTURES / "matchbook_event_chelsea_hull.json"
POLYMARKET = FIXTURES / "polymarket_gamma_event_934146_chelsea_hull.json"


def test_matchbook_event_to_canonical_to_supported_polymarket_equivalent() -> None:
    """Deterministic provider-fixture path. Does not authenticate to Matchbook."""

    mb_fixture = json.loads(MATCHBOOK.read_text(encoding="utf-8"))
    pm_fixture = json.loads(POLYMARKET.read_text(encoding="utf-8"))
    assert mb_fixture["source"] == "fixture"
    assert pm_fixture["retrieved_at"] == "2026-09-12T14:36:20Z"
    assert mb_fixture["retrieved_at"] == "2026-09-12T13:59:50Z"

    mb_event = MatchbookNormalizer().normalize_event(mb_fixture["payload"])
    pm_event = PolymarketNormalizer().normalize_event(pm_fixture["payload"])
    event_match = EventMatcher().match(mb_event, pm_event)
    assert event_match.matched is True
    assert mb_event.kickoff_utc == datetime(2026, 9, 12, 14, 0, tzinfo=UTC)
    assert pm_event.kickoff_utc == mb_event.kickoff_utc

    mb_1x2 = MatchbookNormalizer().normalize_market(mb_event, mb_fixture["payload"]["markets"][0])
    pm_yes_no = PolymarketNormalizer().normalize_market(pm_event, pm_fixture["payload"]["markets"][0])
    assert [runner.outcome for runner in mb_1x2.runners] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    ]
    assert [runner.outcome for runner in pm_yes_no.runners] == [
        CanonicalOutcome.YES,
        CanonicalOutcome.NO,
    ]
    loosened = MarketMatcher().match(mb_1x2, pm_yes_no)
    assert loosened.matched is False
    assert "outcome_space_mismatch" in loosened.reasons

    mb_btts = MatchbookNormalizer().normalize_market(mb_event, mb_fixture["payload"]["markets"][1])
    pm_btts = PolymarketNormalizer().normalize_market(
        pm_event,
        {
            "id": "pm-btts-che-hul",
            "question": "Both teams to score?",
            "sportsMarketType": "both teams to score",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes-token", "no-token"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
    )
    assert mb_btts.family == MarketFamily.BOTH_TEAMS_TO_SCORE
    assert pm_btts.family == MarketFamily.BOTH_TEAMS_TO_SCORE
    assert mb_btts.settlement.scope == SettlementScope.REGULATION_TIME
    assert pm_btts.settlement.scope == SettlementScope.REGULATION_TIME
    assert mb_btts.settlement.is_economically_complete()
    assert pm_btts.settlement.is_economically_complete()
    equivalent = MarketMatcher().match(mb_btts, pm_btts)
    assert equivalent.matched is True

    docs = (Path(__file__).resolve().parents[2] / "docs" / "DEMO_READINESS.md").read_text(
        encoding="utf-8"
    )
    assert "Gamma pagination" in docs or "pagination" in docs.lower()

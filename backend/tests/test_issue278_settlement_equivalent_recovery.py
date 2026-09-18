"""Issue #278: recover market-specific Kalshi result-scope evidence.

Captured public Kalshi Trade API nested-list wording from 2026-09-18. Does not
infer regulation from GAMEWIN / series / title / REG TIME labels. Catalogue
admission still requires complete matching fingerprints. PAPER / execution off.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.equivalence_diagnostics import (
    CATALOGUE_PARAMETER_MISMATCH,
    CATALOGUE_REVIEW_REQUIRED,
    MARKET_SPECIFIC_RULES_MISSING,
    NO_FAMILY_OVERLAP,
    SETTLEMENT_FINGERPRINT_MISMATCH,
    zero_equivalent_reason_counts,
    zero_equivalent_reason_from_inventory,
)
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
    VenueMarketFacts,
    assemble_fixture_inventory,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.catalogue.admission import assess_catalogue_admission, catalogue_allows_solver
from sports_hedge.catalogue.classify import (
    PayloadSide,
    classify_pair,
    classify_payload_pair,
    normalize_payload_side,
)
from sports_hedge.catalogue.corpus import (
    GAMEWIN_TEMPLATE,
    KALSHI_GAMEWIN_SERIES,
    _kalshi,
    _kalshi_1x2,
    _mb,
    _mb_1x2,
    _mb_btts,
    _mb_ftts,
    _mb_totals,
)
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.config import Settings
from sports_hedge.domain.football import (
    CanonicalOutcome,
    FootballPeriod,
    MarketFamily,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.venues import (
    KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON,
    KalshiNormalizer,
    MatchbookNormalizer,
    classify_kalshi_rule_structure,
    classify_settlement_wording,
    kalshi_documented_result_scope_fingerprint,
    resolve_kalshi_settlement_from_rule_fields,
)
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CAPTURED_BAYERN = FIXTURES / "kalshi_trade_api_bundesliga_bayern_union_2026-09-18.json"
KICKOFF = datetime(2026, 9, 18, 18, 30, tzinfo=UTC)
CURRENT_PRIMARY = (
    "If Bayern Munich wins the Bayern Munich vs Union Berlin professional "
    "Bundesliga soccer game originally scheduled for Sep 18, 2026 after 90 "
    "minutes plus stoppage time (does not include extra time or penalties), "
    "then the market resolves to Yes."
)
CURRENT_SECONDARY = (
    "The following market refers to the Bayern Munich vs Union Berlin "
    "professional Bundesliga soccer game originally scheduled for Sep 18, 2026 "
    "after 90 minutes plus stoppage time (does not include extra time or "
    "penalties). If the game ends in a tie, the market called \"Tie\" resolves "
    "to Yes. If the game is cancelled or rescheduled to over 48 hours away, "
    "the market will resolve to a fair price in accordance with the rules."
)
CURRENT_BTTS_PRIMARY = (
    "If Bayern Munich and Union Berlin both score a goal in the Bayern Munich "
    "vs Union Berlin Bundesliga match originally scheduled for Sep 18, 2026 "
    "after 90 minutes plus stoppage time (does not include extra time or "
    "penalties), then the market resolves to Yes."
)
CURRENT_TOTAL_PRIMARY = (
    "If over 2.5 goals are scored in the Bayern Munich vs Union Berlin "
    "professional Bundesliga soccer game originally scheduled for Sep 18, 2026 "
    "after 90 minutes plus stoppage time (does not include extra time or "
    "penalties), then the market resolves to Yes."
)
CURRENT_FTTS_PRIMARY = (
    "If Bayern Munich records the first goal during the entire game "
    "(regulation, stoppage and any extra time periods) of the Bayern Munich vs "
    "Union Berlin professional Bundesliga soccer game originally scheduled for "
    "Sep 18, 2026, then the market resolves to Yes."
)
GAMEWIN_SERIES = {
    "ticker": "KXBUNDESLIGAGAME",
    "title": "Bundesliga",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "contract_terms_url": "https://assets.kalshi.com/contract_terms/SOCCERGAMEWIN.pdf",
    "contract_family": dict(KALSHI_GAMEWIN_SERIES["contract_family"]),
}


def _captured() -> dict[str, Any]:
    return json.loads(CAPTURED_BAYERN.read_text(encoding="utf-8"))


def _mb_event() -> dict[str, Any]:
    return {
        "id": 27801,
        "name": "Bayern Munich vs Union Berlin",
        "start": KICKOFF.isoformat(),
        "competition-name": "Bundesliga",
    }


def _mb_match_odds() -> dict[str, Any]:
    return {
        "id": 27810,
        "name": "Match Odds",
        "runners": [
            {"id": 1, "name": "Bayern Munich", "prices": [{"side": "back", "odds": "1.20", "available-amount": "80"}]},
            {"id": 2, "name": "Draw", "prices": [{"side": "back", "odds": "7.50", "available-amount": "80"}]},
            {"id": 3, "name": "Union Berlin", "prices": [{"side": "back", "odds": "15.00", "available-amount": "80"}]},
        ],
    }


def test_captured_payload_is_honest_public_shape() -> None:
    captured = _captured()
    assert captured["data_class"] == "captured_public_payload_shape"
    assert "not live quotes" in captured["identified_as"].casefold()
    assert captured["source"].startswith("https://external-api.kalshi.com/trade-api/v2/events")
    assert "does not include extra time or penalties" in captured["payload"]["markets"][0]["rules_primary"]
    assert captured["payload"]["markets"][2]["yes_sub_title"] == "Tie"


def test_current_kalshi_90_minute_clause_classifies_as_regulation() -> None:
    scope, extra_time, penalties = classify_settlement_wording(CURRENT_PRIMARY)
    assert scope is SettlementScope.REGULATION_TIME
    assert extra_time is False
    assert penalties is False
    structure = classify_kalshi_rule_structure(CURRENT_PRIMARY)
    assert structure["looks_like_generic_contract_template"] is False
    assert structure["contains_result_scope_placeholder"] is False
    fingerprint = resolve_kalshi_settlement_from_rule_fields(CURRENT_PRIMARY, "")
    assert fingerprint == (SettlementScope.REGULATION_TIME, False, False)
    sibling = resolve_kalshi_settlement_from_rule_fields(CURRENT_PRIMARY, CURRENT_SECONDARY)
    assert sibling == (SettlementScope.UNKNOWN, None, None)
    assert kalshi_documented_result_scope_fingerprint(
        {"rules_primary": CURRENT_PRIMARY, "yes_sub_title": "Bayern Munich", "subtitle": "REG TIME"}
    ) is None


def test_generic_gamewin_template_remains_unknown() -> None:
    scope, extra_time, penalties = classify_settlement_wording(GAMEWIN_TEMPLATE)
    assert scope is SettlementScope.UNKNOWN
    assert extra_time is None
    assert penalties is None
    structure = classify_kalshi_rule_structure(GAMEWIN_TEMPLATE)
    assert structure["looks_like_generic_contract_template"] is True
    assert resolve_kalshi_settlement_from_rule_fields(GAMEWIN_TEMPLATE, GAMEWIN_TEMPLATE) == (
        SettlementScope.UNKNOWN,
        None,
        None,
    )


def test_home_draw_away_binaries_assemble_to_one_three_way() -> None:
    captured = _captured()["payload"]
    event = KalshiNormalizer().normalize_event(captured)
    markets = KalshiNormalizer().assemble_canonical_markets(
        event, list(captured["markets"]), series=GAMEWIN_SERIES, event_payload=captured
    )
    assert len(markets) == 1
    market = markets[0]
    assert market.family is MarketFamily.MATCH_RESULT
    assert market.period is FootballPeriod.FULL_TIME
    assert {runner.outcome for runner in market.runners} == {
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    }
    assert market.settlement.is_economically_complete() is False
    assert market.settlement.unknown_reason == KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON


def test_production_path_mb_k_current_payload_is_paper_assumed_for_fair_price() -> None:
    captured = _captured()["payload"]
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
    assert assessment.matcher_matched is True
    assert assessment.settlement_complete is False
    assert assessment.execution_eligible is False
    assert assessment.paper_mode_admitted is True
    assert assessment.catalogue_shared_by == ("hot", "universe")
    mb = normalize_payload_side(left)
    kalshi = normalize_payload_side(right)
    match = MarketMatcher().match(mb, kalshi)
    assert match.matched is True
    assert catalogue_allows_solver(mb, kalshi) is True
    assert scan_eligible_pair(mb, kalshi, match) is True
    admission = assess_catalogue_admission(mb, kalshi)
    assert admission.allowed is True
    assert admission.live_execution_eligible is False
    assert admission.catalogue_shared_by == ("hot", "universe")


def test_generic_gamewin_without_scope_is_paper_assumed_not_approved() -> None:
    assessment = classify_payload_pair(
        _mb([_mb_1x2()]),
        _kalshi(_kalshi_1x2(rules=GAMEWIN_TEMPLATE), series=KALSHI_GAMEWIN_SERIES),
    )
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.reason == "paper_assumed_equivalent"
    assert assessment.matcher_admits_unknown_1x2 is True
    assert assessment.settlement_assumption == "regulation_time"
    assert assessment.execution_eligible is False
    left = normalize_payload_side(_mb([_mb_1x2()]))
    right = normalize_payload_side(
        _kalshi(_kalshi_1x2(rules=GAMEWIN_TEMPLATE), series=KALSHI_GAMEWIN_SERIES)
    )
    assert catalogue_allows_solver(left, right) is True
    assert scan_eligible_pair(left, right, MarketMatcher().match(left, right)) is True


def test_title_reg_time_does_not_approve_gamewin_unknown() -> None:
    markets = _kalshi_1x2(rules=GAMEWIN_TEMPLATE)
    for item in markets:
        item["subtitle"] = "REG TIME"
        item["title"] = f"{item['title']} REG TIME"
    assessment = classify_payload_pair(
        _mb([_mb_1x2()]),
        _kalshi(markets, series=KALSHI_GAMEWIN_SERIES),
    )
    assert assessment.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.settlement_complete is False


def test_current_btts_and_half_line_totals_remain_approved() -> None:
    btts = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[
            {
                "id": 27820,
                "name": "Both Teams To Score",
                "runners": [{"id": 1, "name": "Yes"}, {"id": 2, "name": "No"}],
            }
        ]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_captured()["payload"],
            markets=[
                {
                    "ticker": "KXBUNDESLIGABTTS-26SEP18BMUUNI-BTTS",
                    "title": "Both Teams To Score",
                    "yes_sub_title": "Both Teams To Score",
                    "rules_primary": CURRENT_BTTS_PRIMARY,
                    "rules_secondary": "Own goals count towards the team awarded the goal.",
                }
            ],
            series=GAMEWIN_SERIES,
        ),
    )
    assert btts.state is CatalogueApprovalState.APPROVED_EQUIVALENT
    totals = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[
            {
                "id": 27830,
                "name": "Over/Under 2.5 Goals",
                "line": "2.5",
                "runners": [{"id": 1, "name": "Over 2.5"}, {"id": 2, "name": "Under 2.5"}],
            }
        ]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_captured()["payload"],
            markets=[
                {
                    "ticker": "KXBUNDESLIGATOTAL-26SEP18BMUUNI-3",
                    "title": "Will over 2.5 goals be scored?",
                    "yes_sub_title": "Over 2.5 goals scored",
                    "rules_primary": CURRENT_TOTAL_PRIMARY,
                    "strike": "2.5",
                    "floor_strike": 2.5,
                }
            ],
            series={"ticker": "KXBUNDESLIGATOTAL", "title": "Bundesliga"},
        ),
    )
    assert totals.state is CatalogueApprovalState.APPROVED_EQUIVALENT
    corpus_btts = classify_payload_pair(_mb([_mb_btts()]), _kalshi([
        {
            "ticker": "KXEPL-BTTS",
            "title": "Both Teams To Score",
            "yes_sub_title": "Yes",
            "rules_primary": "Resolves on 90 minutes of regulation time. Extra time and penalties do not count.",
        }
    ]))
    assert corpus_btts.state is CatalogueApprovalState.APPROVED_EQUIVALENT
    corpus_totals = classify_payload_pair(_mb([_mb_totals("2.5")]), _kalshi([
        {
            "ticker": "KXEPL-TOTAL-2.5",
            "title": "Tottenham vs Everton Total Goals 2.5",
            "yes_sub_title": "Over 2.5",
            "rules_primary": "Resolves on 90 minutes of regulation time. Extra time and penalties do not count.",
            "strike": "2.5",
        }
    ]))
    assert corpus_totals.state is CatalogueApprovalState.APPROVED_EQUIVALENT


def test_parameter_mismatch_and_contradiction_remain_blocked() -> None:
    captured = _captured()["payload"]
    mb_full = normalize_payload_side(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_match_odds()])
    )
    kalshi_full = normalize_payload_side(
        PayloadSide(
            venue=VenueName.KALSHI,
            event=captured,
            markets=list(captured["markets"]),
            series=GAMEWIN_SERIES,
        )
    )
    first_half = kalshi_full.model_copy(deep=True)
    first_half.period = FootballPeriod.FIRST_HALF
    first_half.settlement = kalshi_full.settlement.model_copy(
        update={"period": FootballPeriod.FIRST_HALF}
    )
    period = classify_pair(mb_full, first_half)
    assert period.state is CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH
    line = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[
            {
                "id": 27831,
                "name": "Over/Under 2.5 Goals",
                "line": "2.5",
                "runners": [{"id": 1, "name": "Over 2.5"}, {"id": 2, "name": "Under 2.5"}],
            }
        ]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=captured,
            markets=[
                {
                    "ticker": "KXBUNDESLIGATOTAL-26SEP18BMUUNI-4",
                    "title": "Will over 3.5 goals be scored?",
                    "yes_sub_title": "Over 3.5 goals scored",
                    "rules_primary": CURRENT_TOTAL_PRIMARY.replace("2.5", "3.5"),
                    "strike": "3.5",
                    "floor_strike": 3.5,
                }
            ],
            series={"ticker": "KXBUNDESLIGATOTAL", "title": "Bundesliga"},
        ),
    )
    assert line.state is CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH
    et_markets = []
    for item in captured["markets"]:
        updated = dict(item)
        updated["rules_primary"] = "Resolves including extra time and penalties."
        updated["rules_secondary"] = ""
        et_markets.append(updated)
    contradiction = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=captured,
            markets=et_markets,
            series=GAMEWIN_SERIES,
        ),
    )
    assert contradiction.state is CatalogueApprovalState.KNOWN_CONTRADICTION
    ftts = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[
            {
                "id": 27840,
                "name": "First Team To Score",
                "runners": [
                    {"id": 1, "name": "Bayern Munich"},
                    {"id": 2, "name": "Union Berlin"},
                    {"id": 3, "name": "No Goal"},
                ],
            }
        ]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=captured,
            markets=[
                {
                    "ticker": "KXBUNDESLIGAFTTS-26SEP18BMUUNI-BMU",
                    "title": "First Team To Score",
                    "yes_sub_title": "Bayern Munich",
                    "rules_primary": CURRENT_FTTS_PRIMARY,
                },
                {
                    "ticker": "KXBUNDESLIGAFTTS-26SEP18BMUUNI-UNI",
                    "title": "First Team To Score",
                    "yes_sub_title": "Union Berlin",
                    "rules_primary": CURRENT_FTTS_PRIMARY.replace("Bayern Munich records", "Union Berlin records"),
                },
                {
                    "ticker": "KXBUNDESLIGAFTTS-26SEP18BMUUNI-NONE",
                    "title": "First Team To Score",
                    "yes_sub_title": "No Goal",
                    "rules_primary": CURRENT_FTTS_PRIMARY.replace(
                        "If Bayern Munich records the first goal",
                        "If no team records the first goal",
                    ),
                },
            ],
        ),
    )
    assert ftts.state in {
        CatalogueApprovalState.REVIEW_REQUIRED,
        CatalogueApprovalState.KNOWN_CONTRADICTION,
    }
    assert ftts.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    corpus_ftts = classify_payload_pair(_mb([_mb_ftts()]), _kalshi([
        {
            "ticker": "KX-FTTS-H",
            "title": "First team to score",
            "yes_sub_title": "Tottenham",
            "rules_primary": "Resolves on 90 minutes of regulation time. Extra time and penalties do not count.",
        },
        {
            "ticker": "KX-FTTS-A",
            "title": "First team to score",
            "yes_sub_title": "Everton",
            "rules_primary": "Resolves on 90 minutes of regulation time. Extra time and penalties do not count.",
        },
        {
            "ticker": "KX-FTTS-N",
            "title": "First team to score",
            "yes_sub_title": "No Goal",
            "rules_primary": "Resolves on 90 minutes of regulation time. Extra time and penalties do not count.",
        },
    ]))
    assert corpus_ftts.state is CatalogueApprovalState.APPROVED_EQUIVALENT


def test_hot_and_universe_share_the_current_payload_gate() -> None:
    captured = _captured()["payload"]
    left = normalize_payload_side(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_match_odds()])
    )
    right = normalize_payload_side(
        PayloadSide(
            venue=VenueName.KALSHI,
            event=captured,
            markets=list(captured["markets"]),
            series=GAMEWIN_SERIES,
        )
    )
    hot = assess_catalogue_admission(left, right)
    universe = assess_catalogue_admission(left, right)
    assert hot.allowed is True
    assert universe.allowed is True
    assert hot.live_execution_eligible is False
    assert universe.live_execution_eligible is False
    assert hot.catalogue_shared_by == universe.catalogue_shared_by == ("hot", "universe")
    assert hot.assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert universe.assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert hot.assessment.reason == "paper_assumed_equivalent"
    assert universe.assessment.reason == "paper_assumed_equivalent"


def test_zero_equivalent_diagnostics_state_the_actual_reason() -> None:
    mb_event = MatchbookNormalizer().normalize_event(_mb_event())
    mb_1x2 = MatchbookNormalizer().normalize_market(mb_event, _mb_match_odds())
    gamewin_event = KalshiNormalizer().normalize_event(_captured()["payload"])
    gamewin_markets = _kalshi_1x2(rules=GAMEWIN_TEMPLATE)
    for item in gamewin_markets:
        item["yes_sub_title"] = {
            "KXEPL-TOT": "Bayern Munich",
            "KXEPL-DRAW": "Tie",
            "KXEPL-EVE": "Union Berlin",
        }[item["ticker"]]
    kalshi_unknown = KalshiNormalizer().assemble_canonical_markets(
        gamewin_event, gamewin_markets, series=KALSHI_GAMEWIN_SERIES
    )[0]
    from sports_hedge.application.fixture_inventory import InventoryMarket

    rows = assemble_fixture_inventory(
        [
            InventoryMarket(
                venue=VenueName.MATCHBOOK,
                source_event_id="27801",
                source_market_id="27810",
                raw_name="Match Odds",
                canonical=mb_1x2,
            )
        ],
        [],
        kalshi_markets=[
            InventoryMarket(
                venue=VenueName.KALSHI,
                source_event_id=gamewin_event.source_event_id,
                source_market_id=kalshi_unknown.source_market_id,
                raw_name="GAMEWIN",
                canonical=kalshi_unknown,
            )
        ],
    )
    assert any(
        row.comparison_status is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
        for row in rows
    )
    family_rows = [
        FixtureMarketInventoryRow(
            display_name="Match Odds",
            family="match_result",
            comparison_status=InventoryComparisonStatus.VENUE_ONLY,
            matchbook=VenueMarketFacts(
                venue=VenueName.MATCHBOOK,
                source_event_id="mb",
                source_market_id="1",
                family="match_result",
            ),
        ),
        FixtureMarketInventoryRow(
            display_name="BTTS",
            family="both_teams_to_score",
            comparison_status=InventoryComparisonStatus.VENUE_ONLY,
            polymarket=VenueMarketFacts(
                venue=VenueName.POLYMARKET,
                source_event_id="pm",
                source_market_id="2",
                family="both_teams_to_score",
            ),
        ),
    ]
    assert zero_equivalent_reason_from_inventory(family_rows) == NO_FAMILY_OVERLAP
    mismatch_rows = [
        FixtureMarketInventoryRow(
            display_name="1X2",
            family="match_result",
            comparison_status=InventoryComparisonStatus.SETTLEMENT_MISMATCH,
            reason="settlement_mismatch",
            match_reasons=["settlement_mismatch"],
            matchbook=VenueMarketFacts(
                venue=VenueName.MATCHBOOK,
                source_event_id="mb",
                source_market_id="1",
                family="match_result",
            ),
            kalshi=VenueMarketFacts(
                venue=VenueName.KALSHI,
                source_event_id="k",
                source_market_id="2",
                family="match_result",
            ),
        )
    ]
    assert zero_equivalent_reason_from_inventory(mismatch_rows) == SETTLEMENT_FINGERPRINT_MISMATCH
    param_rows = [
        FixtureMarketInventoryRow(
            display_name="1X2",
            family="match_result",
            comparison_status=InventoryComparisonStatus.OTHER,
            reason="catalogue_approved_parameter_mismatch",
            rejection_reasons=["catalogue_approved_parameter_mismatch", "period_mismatch"],
            matchbook=VenueMarketFacts(
                venue=VenueName.MATCHBOOK,
                source_event_id="mb",
                source_market_id="1",
                family="match_result",
            ),
            kalshi=VenueMarketFacts(
                venue=VenueName.KALSHI,
                source_event_id="k",
                source_market_id="2",
                family="match_result",
            ),
        )
    ]
    assert zero_equivalent_reason_from_inventory(param_rows) == CATALOGUE_PARAMETER_MISMATCH
    review_rows = [
        FixtureMarketInventoryRow(
            display_name="1X2",
            family="match_result",
            comparison_status=InventoryComparisonStatus.OTHER,
            reason="catalogue_review_required",
            rejection_reasons=["catalogue_review_required", "incomplete_settlement"],
            matchbook=VenueMarketFacts(
                venue=VenueName.MATCHBOOK,
                source_event_id="mb",
                source_market_id="1",
                family="match_result",
            ),
            kalshi=VenueMarketFacts(
                venue=VenueName.KALSHI,
                source_event_id="k",
                source_market_id="2",
                family="match_result",
            ),
        )
    ]
    assert zero_equivalent_reason_from_inventory(review_rows) == CATALOGUE_REVIEW_REQUIRED
    counts = zero_equivalent_reason_counts(
        [
            type(
                "F",
                (),
                {
                    "matchbook_matched": True,
                    "polymarket_matched": False,
                    "kalshi_matched": True,
                    "matched_equivalent_count": 0,
                    "canonical_event_id": "fx-1",
                    "no_comparison_reason": MARKET_SPECIFIC_RULES_MISSING,
                },
            )()
        ],
        {"fx-1": rows},
    )
    assert counts == {MARKET_SPECIFIC_RULES_MISSING: 1}


def test_execution_remains_disabled() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False


class _BayernMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [_mb_event()]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": [_mb_match_odds()]}


class _DisabledPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []


class _BayernKalshi:
    def __init__(self) -> None:
        self.get_market_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [_captured()["payload"]]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.get_market_calls.append(ticker)
        raise AssertionError("nested current-realistic rules must skip Get Market when complete")

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
                "yes_dollars": [["0.90", "100.00"]],
                "no_dollars": [["0.09", "200.00"]],
            }
        }

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        return {**GAMEWIN_SERIES, "ticker": series_ticker}


@pytest.mark.asyncio
async def test_collector_nested_current_payload_is_not_inventory_equivalent() -> None:
    repository = SqliteMarketIntelligenceRepository()
    kalshi = _BayernKalshi()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=_BayernMatchbook(),
        polymarket=_DisabledPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=[
                *matchbook_polymarket_costs(),
                kalshi_cost_from_series(GAMEWIN_SERIES, captured_at=datetime.now(UTC)),
            ],
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx"),
                FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency"),
            ],
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        )
        assert kalshi.get_market_calls == []
        fixture = report.discovered_fixtures[0]
        assert fixture.matchbook_matched is True
        assert fixture.kalshi_matched is True
        assert fixture.matched_equivalent_count == 0
        rows = report.fixture_markets[fixture.canonical_event_id]
        assert not any(
            row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
            and row.entered_solver
            for row in rows
        )
        coverage = report.scan_diagnostics["matching_coverage"]
        assert coverage["equivalent_markets"] == 0
        assert Settings().sports_hedge_execution_enabled is False
    finally:
        repository.close()

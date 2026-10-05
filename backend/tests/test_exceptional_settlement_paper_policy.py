"""Owner PAPER policy: exceptional settlement is not an admission gate.

Fixture and synthetic markets only. Live execution stays disabled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from test_execution_reprice_before_paper_entry import _decision
from test_mlb_stage1 import _moneyline_markets, _total_market
from test_ncaab_paper_onboard import _gw_market, _ncaab_event
from test_nba_paper_markets import _normalize_kalshi_game, _normalize_pm_family
from test_phase2_three_venue_pipeline import (
    ENGLAND_SPAIN,
    _market,
    _mb_k_board,
    _moneyline,
)
from test_phase3a_catalogue_native_identity import (
    _kalshi,
    _mb,
    _pair,
    _persist,
    _polymarket,
)
from test_tennis_stage1 import _pair_markets

from sports_hedge.application.capture_replay import FORBIDDEN_WRITE_METHODS
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.execution_reprice import execution_reprice_permitted
from sports_hedge.catalogue.classify import classify_pair
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalOutcome, FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import (
    canonical_key_for_market,
    registered_canonical_key,
)
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.mlb.constants import (
    CANONICAL_MLB_GAME_WINNER,
    MLB_SETTLEMENT_NOT_EXECUTABLE,
)
from sports_hedge.mlb.settlement import mlb_pair_non_executable_reason
from sports_hedge.nba.constants import NBA_EXCEPTIONAL_SETTLEMENT_CAVEAT
from sports_hedge.ncaab.constants import (
    NCAAB_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NCAAB_PAIR_UNAPPROVED_REASON,
)
from sports_hedge.ncaab.settlement import ncaab_family_settlement_blocker
from sports_hedge.nfl.constants import NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT
from sports_hedge.nfl.settlement import (
    nfl_automatic_settlement_lifecycle_blocker,
    nfl_exceptional_status_blocker,
    nfl_paper_audit_reasons,
)
from sports_hedge.normalization.venues import PolymarketNormalizer
from sports_hedge.paper.canonical_results import NFL_EXCEPTIONAL_TIE_BLOCKER
from sports_hedge.paper.trades import PaperTradeAuditEvent, PaperTradeAuditEventType
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.tennis.constants import TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT
from sports_hedge.tennis.settlement import tennis_executable_block_reason
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _promoted_match_result():
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(ENGLAND_SPAIN)
    payloads = [
        _moneyline("4521504", "Will England win on 2026-09-26?", "yes-home"),
        _moneyline("4521505", "Will England vs. Spain end in a draw?", "yes-draw"),
        _moneyline("4521506", "Will Spain win on 2026-09-26?", "yes-away"),
    ]
    promoted = normalizer.assemble_canonical_markets(event, payloads)
    assert len(promoted) == 1
    return promoted[0]


def test_football_polymarket_kalshi_match_result_is_paper_admitted() -> None:
    promoted = _promoted_match_result()
    _left, kalshi = _mb_k_board()
    match = MarketMatcher().match(promoted, kalshi[0])
    assert registered_canonical_key(promoted, kalshi[0]) == "MATCH_RESULT_FT"
    assert scan_eligible_pair(promoted, kalshi[0], match) is True
    assessment = classify_pair(promoted, kalshi[0])
    assert assessment.paper_mode_admitted is True
    assert assessment.execution_eligible is True
    assert NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT not in match.reasons


def test_football_polymarket_matchbook_match_result_is_paper_admitted() -> None:
    promoted = _promoted_match_result()
    matchbook, _kalshi_board = _mb_k_board()
    match = MarketMatcher().match(matchbook[0], promoted)
    assert registered_canonical_key(matchbook[0], promoted) == "MATCH_RESULT_FT"
    assert scan_eligible_pair(matchbook[0], promoted, match) is True
    assert classify_pair(matchbook[0], promoted).paper_mode_admitted is True
    assert classify_pair(matchbook[0], promoted).execution_eligible is True


def test_one_football_row_yields_pairwise_paper_without_native_id_overwrite() -> None:
    matchbook, kalshi, polymarket = _mb(), _kalshi(), _polymarket()
    pairs = (
        registered_canonical_key(matchbook, kalshi),
        registered_canonical_key(matchbook, polymarket),
        registered_canonical_key(polymarket, kalshi),
    )
    assert pairs == ("MATCH_RESULT_FT", "MATCH_RESULT_FT", "MATCH_RESULT_FT")
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        _persist(store, [_pair(matchbook=matchbook, kalshi=kalshi, polymarket=polymarket)])
        rows = store.list_active()
        assert len(rows) == 1
        original = rows[0]
        assert original.polymarket_event_id == "1016065"
        assert original.matchbook_market_id == "mb-1x2"
        conflicts: list = []
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
        held = store.get_row(original.catalogue_row_id)
        assert held is not None
        assert held.kalshi_event_ticker == original.kalshi_event_ticker
        assert held.polymarket_event_id == original.polymarket_event_id
        assert held.matchbook_market_id == original.matchbook_market_id
        assert len(store.list_active()) == 1
    finally:
        store.close()


def test_football_line_period_and_outcome_mismatches_stay_rejected() -> None:
    matchbook, kalshi = _mb_k_board()
    total_25 = next(market for market in matchbook if market.line == Decimal("2.5"))
    total_35 = next(market for market in kalshi if market.line == Decimal("3.5"))
    assert registered_canonical_key(total_25, total_35) is None
    first_half = total_25.model_copy(update={"period": FootballPeriod.FIRST_HALF})
    assert canonical_key_for_market(first_half) is None
    two_way = _market(
        total_25.event,
        family=MarketFamily.MATCH_RESULT,
        source_id="mb-two-way",
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY],
    )
    assert canonical_key_for_market(two_way) is None
    integer = total_25.model_copy(update={"line": Decimal("2")})
    assert canonical_key_for_market(integer) is None
    ftts_complete = _market(
        total_25.event,
        family=MarketFamily.FIRST_TEAM_TO_SCORE,
        source_id="pm-ftts",
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY, CanonicalOutcome.NO_GOAL],
    )
    ftts_complete = ftts_complete.model_copy(update={"source_venue": VenueName.POLYMARKET})
    assert canonical_key_for_market(ftts_complete) == "FTTS_FT"
    ftts_incomplete = _market(
        total_25.event,
        family=MarketFamily.FIRST_TEAM_TO_SCORE,
        source_id="pm-ftts-2",
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY],
    )
    ftts_incomplete = ftts_incomplete.model_copy(update={"source_venue": VenueName.POLYMARKET})
    assert canonical_key_for_market(ftts_incomplete) is None


def test_nfl_exceptional_lifecycle_does_not_block_paper_audit() -> None:
    assert nfl_exceptional_status_blocker("postponed", "cancelled", "50-50") is None
    assert nfl_automatic_settlement_lifecycle_blocker(object(), "postponed") is None
    reasons = nfl_paper_audit_reasons()
    assert NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT not in reasons
    assert "nfl_paper_not_live_execution_equivalent" not in reasons
    assert "settlement_assumption=normal_full_game_completion" in reasons


def test_mlb_approved_families_are_not_blocked_by_settlement_equivalence() -> None:
    kalshi, polymarket, matchbook = _moneyline_markets()
    assert registered_canonical_key(kalshi, matchbook) == CANONICAL_MLB_GAME_WINNER
    assert MLB_SETTLEMENT_NOT_EXECUTABLE not in MarketMatcher().match(kalshi, polymarket).reasons
    assert mlb_pair_non_executable_reason(kalshi, matchbook) != MLB_SETTLEMENT_NOT_EXECUTABLE
    kalshi_total = _total_market("kalshi", "8.5")
    polymarket_total = _total_market("polymarket", "8.5")
    assert registered_canonical_key(kalshi_total, polymarket_total) == "MLB_TOTAL_RUNS_FT:8.5"
    assert classify_pair(kalshi, matchbook).paper_mode_admitted is True
    assert classify_pair(kalshi, matchbook).execution_eligible is True


def test_tennis_match_winner_is_not_blocked_only_by_retirement() -> None:
    left, right = _pair_markets()
    assert tennis_executable_block_reason(left, right) is None
    matched = MarketMatcher().match(left, right)
    assert TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT not in matched.reasons
    assert scan_eligible_pair(left, right, matched) is True
    assert classify_pair(left, right).execution_eligible is True


def test_nba_and_ncaab_ordinary_contract_blocks_remain() -> None:
    _event, kalshi = _normalize_kalshi_game()
    _pm_event, spread, _raw = _normalize_pm_family("spreads")
    kalshi_spread = kalshi.model_copy(
        update={"family": MarketFamily.POINT_SPREAD, "line": spread.line, "runners": spread.runners}
    )
    same_fixture_spread = spread.model_copy(update={"event": kalshi.event})
    assert registered_canonical_key(kalshi_spread, same_fixture_spread) is None
    assessment = classify_pair(kalshi_spread, same_fixture_spread)
    assert assessment.paper_mode_admitted is False
    assert NBA_EXCEPTIONAL_SETTLEMENT_CAVEAT not in assessment.notes
    assert assessment.reason != NBA_EXCEPTIONAL_SETTLEMENT_CAVEAT
    assert assessment.reason == "not_registered"
    home, away = "Xavier Musketeers", "Duke Blue Devils"
    left = _gw_market(
        _ncaab_event(home=home, away=away, venue=VenueName.KALSHI, source_id="k-1"),
        VenueName.KALSHI,
        "k-ml",
    )
    right = _gw_market(
        _ncaab_event(home=home, away=away, venue=VenueName.POLYMARKET, source_id="pm-1"),
        VenueName.POLYMARKET,
        "pm-ml",
    )
    assert registered_canonical_key(left, right) is None
    ncaab = classify_pair(left, right)
    assert ncaab.paper_mode_admitted is False
    assert NCAAB_EXCEPTIONAL_SETTLEMENT_CAVEAT not in ncaab.notes
    assert ncaab.reason == "not_registered"
    trade = type("Trade", (), {"competition": "NCAA Men's Basketball"})()
    assert ncaab_family_settlement_blocker(trade) == NCAAB_PAIR_UNAPPROVED_REASON


def test_historical_exceptional_audit_strings_remain_readable() -> None:
    historical = PaperTradeAuditEvent(
        event_id="trade-old:exceptional_settlement_mismatch_possible",
        occurred_at=NOW,
        event_type=PaperTradeAuditEventType.TRADE_OPENED,
        detail=(
            f"{NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT}: PAPER comparison is for a normal completed game"
        ),
    )
    assert "exceptional_settlement_mismatch_possible" in historical.event_id
    assert "exceptional_settlement_mismatch_possible" in historical.detail
    assert MLB_SETTLEMENT_NOT_EXECUTABLE == "mlb_settlement_equivalence_not_proven"
    assert TENNIS_RETIREMENT_SETTLEMENT_NOT_EQUIVALENT == "tennis_retirement_settlement_not_equivalent"
    assert NFL_EXCEPTIONAL_TIE_BLOCKER == "nfl_exceptional_tie_fail_closed"
    assert NCAAB_EXCEPTIONAL_SETTLEMENT_CAVEAT == "exceptional_settlement_mismatch_possible"


def test_live_order_api_stays_unreachable() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)


def test_execution_time_exact_id_reprice_stays_mandatory() -> None:
    stale = _decision()
    assert stale.eligible_for_paper_simulation is False
    assert "stale_quote" in stale.rejection_reasons
    assert execution_reprice_permitted(stale) is True
    missing_identity = stale.model_copy(update={"canonical_market_id": ""})
    assert execution_reprice_permitted(missing_identity) is False
    fresh = _decision(eligible_for_paper_simulation=True, rejection_reasons=[])
    assert execution_reprice_permitted(fresh) is True
    below_edge = _decision(payoff_scan=None, rejection_reasons=["stale_quote"])
    assert execution_reprice_permitted(below_edge) is False

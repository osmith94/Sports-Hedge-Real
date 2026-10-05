"""One admission decision and one accepted Price-2 entry vote.

Fixture data only. Does not enable execution or place an order.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from test_nfl_stage1b_paper_markets import (
    _normalize_kalshi_game,
    _normalize_kalshi_total,
)
from test_real_registered_admission_cleanup import _corpus_markets
from test_real_wave3_live_dispatch import test_unknown_fill_size_is_not_stored_as_zero

from sports_hedge.application.execution_reprice import (
    capture_with_execution_reprice,
    execution_entry_block,
    execution_reprice_permitted,
    price2_entry_authorized,
)
from sports_hedge.application.execution_snapshot import ExecutionSnapshot
from sports_hedge.arbitrage.models import PayoffSolution
from sports_hedge.arbitrage.payoff_scan import PayoffScanResult
from sports_hedge.arbitrage.watchlist.models import (
    NearOpportunity,
    OpportunityClassification,
    OpportunityStatus,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.catalogue.admission import (
    CatalogueAdmission,
    assess_catalogue_admission,
    catalogue_allows_live_execution,
    catalogue_allows_solver,
)
from sports_hedge.catalogue.classify import CataloguePairAssessment, classify_pair
from sports_hedge.catalogue.legacy_paper_labels import (
    LEGACY_DIAGNOSTIC_REJECTION_LABELS,
    persisted_rejection_reasons,
)
from sports_hedge.execution.attempts import LiveExecutionAttemptStore
from sports_hedge.matching.markets import MarketMatcher, MarketMatchResult
from sports_hedge.matching.paper_assumed import PAPER_NONBLOCKING_REJECTION_REASONS
from sports_hedge.paper.models import PaperScanDecision


def test_diagnostic_labels_cannot_veto_a_persisted_admitted_pair() -> None:
    stored = [
        "paper_assumed_equivalent",
        "registered_equivalent_settlement_not_independently_proven",
        "settlement_assumption=normal_full_game_completion",
        "nfl_paper_not_live_execution_equivalent",
        "stale_quote",
    ]
    assert persisted_rejection_reasons(stored) == ["stale_quote"]
    now = datetime(2026, 9, 20, 13, tzinfo=UTC)
    repository = SqliteWatchlistRepository(":memory:")
    try:
        repository.upsert_opportunity(
            NearOpportunity(
                opportunity_id="watch:mkt",
                canonical_event_id="evt",
                canonical_market_id="mkt",
                venues=[],
                status=OpportunityStatus.TRIGGERED,
                classification=OpportunityClassification.TRIGGERED_OPPORTUNITY,
                is_arbitrage=True,
                trigger_net_edge=Decimal("0.01"),
                current_net_edge=Decimal("0.02"),
                first_seen_at=now,
                last_seen_at=now,
                rejection_reasons=stored,
            )
        )
        loaded = repository.get("watch:mkt")
        assert loaded is not None
        assert loaded.rejection_reasons == ["stale_quote"]
        assert "paper_assumed_equivalent" not in loaded.rejection_reasons
    finally:
        repository.close()


def test_live_modules_do_not_filter_on_the_legacy_label_set() -> None:
    assert PAPER_NONBLOCKING_REJECTION_REASONS is LEGACY_DIAGNOSTIC_REJECTION_LABELS
    from sports_hedge.application import execution_reprice, paper_operations, paper_scan
    from sports_hedge.arbitrage.watchlist import economics

    for module in (paper_scan, economics, paper_operations, execution_reprice):
        assert "PAPER_NONBLOCKING_REJECTION_REASONS" not in inspect.getsource(module)


def test_admission_aliases_follow_the_canonical_decision() -> None:
    left, right = _corpus_markets("bad-1x2-mb-k-gamewin-unknown")
    assessment = classify_pair(left, right)
    assert assessment.admitted is True
    assert assessment.execution_eligible is assessment.admitted
    assert assessment.paper_mode_admitted is assessment.admitted
    admission = assess_catalogue_admission(left, right)
    assert admission.admitted is assessment.admitted
    assert admission.allowed is admission.admitted
    assert admission.paper_mode_admitted is admission.admitted
    assert admission.live_execution_eligible is admission.admitted
    assert catalogue_allows_solver(left, right) is admission.admitted
    assert catalogue_allows_live_execution(left, right) is admission.admitted

    payload = assessment.model_dump()
    payload["execution_eligible"] = False
    payload["paper_mode_admitted"] = False
    restored = CataloguePairAssessment.model_validate(payload)
    assert restored.admitted is True
    assert restored.execution_eligible is True
    assert restored.paper_mode_admitted is True

    body = admission.model_dump()
    body["allowed"] = False
    body["paper_mode_admitted"] = False
    body["live_execution_eligible"] = False
    reloaded = CatalogueAdmission.model_validate(body)
    assert reloaded.admitted is True
    assert reloaded.allowed is True
    assert reloaded.live_execution_eligible is True

    flipped = assessment.model_copy(update={"admitted": False})
    assert flipped.execution_eligible is False
    assert flipped.paper_mode_admitted is False


def test_register_key_is_not_resolved_again_after_the_matcher() -> None:
    left, right = _corpus_markets("bad-1x2-mb-k-gamewin-unknown")
    calls = {"n": 0}
    from sports_hedge.matching import markets as markets_module

    real = markets_module.registered_canonical_key

    def wrapped(one, two):
        calls["n"] += 1
        return real(one, two)

    markets_module.registered_canonical_key = wrapped
    try:
        match = MarketMatcher().match(left, right)
        assert calls["n"] == 1
        assert match.register_key == "MATCH_RESULT_FT"
        assert classify_pair(left, right, match=match).admitted is True
        assert assess_catalogue_admission(left, right, match=match).admitted is True
        assert catalogue_allows_solver(left, right, match=match) is True
        assert calls["n"] == 1
    finally:
        markets_module.registered_canonical_key = real


def test_unregistered_and_mismatched_pairs_fail_closed() -> None:
    _, game = _normalize_kalshi_game()
    _, total = _normalize_kalshi_total()
    assert catalogue_allows_solver(game, total) is False
    assert catalogue_allows_live_execution(game, total) is False
    left, right = _corpus_markets("bad-1x2-et-contradiction")
    admission = assess_catalogue_admission(left, right)
    assert admission.admitted is False
    assert admission.allowed is False
    assert admission.live_execution_eligible is False
    assert admission.rejection_reason


def test_duplicate_reservation_and_unknown_fill_stay_intact() -> None:
    store = LiveExecutionAttemptStore()
    record = {
        "package_id": "pkg-1",
        "trade_id": "trade-1",
        "tranche_id": "open",
        "opportunity_id": "opp-1",
        "created_at": datetime(2026, 9, 20, tzinfo=UTC),
    }
    assert store.reserve(record) is True
    assert store.reserve(record) is False
    test_unknown_fill_size_is_not_stored_as_zero()


def test_accepted_price2_snapshot_is_not_redecided_later() -> None:
    source = inspect.getsource(capture_with_execution_reprice)
    assert "execution_entry_block" not in source
    assert "price2_entry_authorized" in source
    snapshot = ExecutionSnapshot(
        catalogue_row_id="row",
        started_at=datetime(2026, 9, 20, tzinfo=UTC),
        retrievals=(),
    )
    assert price2_entry_authorized(snapshot) is False
    snapshot.accepted = True
    assert price2_entry_authorized(snapshot) is True


@pytest.mark.asyncio
async def test_capture_trusts_an_accepted_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    discovery = PaperScanDecision(
        scanned_at=datetime(2026, 9, 20, tzinfo=UTC),
        canonical_market_id="mkt-exec",
        market_match=MarketMatchResult(
            matched=True,
            confidence=1.0,
            reasons=["register"],
            register_key="MATCH_RESULT_FT",
        ),
        payoff_scan=PayoffScanResult(
            solution=PayoffSolution(
                is_arbitrage=True,
                roi=Decimal("0.41"),
                minimum_state_pnl=Decimal("1"),
                numerically_validated=True,
            )
        ),
        minimum_net_edge=Decimal("0.01"),
        solver_model="strict_complete_set",
        eligible_for_paper_simulation=True,
        rejection_reasons=[],
    )
    refreshed_decision = discovery.model_copy(
        update={
            "eligible_for_paper_simulation": False,
            "rejection_reasons": ["stale_quote", "paper_assumed_equivalent"],
        }
    )
    assert execution_reprice_permitted(discovery) is True
    assert execution_entry_block(refreshed_decision) == "execution_reprice_stale"
    snapshot = ExecutionSnapshot(
        catalogue_row_id="row",
        started_at=datetime(2026, 9, 20, tzinfo=UTC),
        retrievals=(),
        accepted=True,
    )
    votes = {"block": 0, "persist": 0}

    def counting_block(item: PaperScanDecision) -> str | None:
        votes["block"] += 1
        return execution_entry_block(item)

    monkeypatch.setattr(
        "sports_hedge.application.execution_reprice.execution_entry_block",
        counting_block,
    )

    async def reprice(*_args, **_kwargs):
        return SimpleNamespace(
            duplicate=False,
            decision=refreshed_decision,
            reason=None,
            snapshot=snapshot,
            diagnostics=None,
            refreshed_venues=(),
        )

    def persist(*_args, **kwargs):
        votes["persist"] += 1
        assert kwargs["execution_authoritative"] is True
        assert "accepted" in kwargs["execution_snapshot_json"]
        return []

    async def _continue(**_kwargs):
        return None

    monkeypatch.setattr(
        "sports_hedge.api.paper.persist_price_engine_item_capture",
        persist,
    )
    monkeypatch.setattr(
        "sports_hedge.application.execution_reprice.continue_iterative_paper_fills",
        _continue,
    )
    monkeypatch.setattr(
        "sports_hedge.application.execution_reprice.record_execution_snapshot_attempt",
        lambda *_args, **_kwargs: None,
    )
    watchlist = SimpleNamespace(
        has_active_bound_attempt=lambda _opportunity_id: False,
        observe_paper_decision=lambda *_args, **_kwargs: None,
        note_execution_reprice_miss=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("accepted snapshot must not be recorded as a miss")
        ),
    )
    service = SimpleNamespace(
        market_intelligence=SimpleNamespace(market_history=lambda **_kwargs: [])
    )
    result = await capture_with_execution_reprice(
        discovery,
        runtime=None,
        engine=SimpleNamespace(reprice_for_paper_entry=reprice),
        service=service,
        watchlist=watchlist,  # type: ignore[arg-type]
        pricing_lane=None,
    )
    assert votes["block"] == 0
    assert votes["persist"] == 1
    assert result.entry_decision is refreshed_decision

"""Wave 41: elapsed Price-2 quote age is fail-closed at the live submit seam.

Fake transports only. No venue I/O. Operator Max Event / Opportunity /
One-Time stay on the existing settings store.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sports_hedge.application.paper_operations import PaperOperationsError
from sports_hedge.config import Settings
from sports_hedge.execution.live_canary import (
    LiveCanaryTradingFacts,
    disarm_live_runtime,
    reset_live_canary_runtime,
    set_live_canary_trading_facts,
    set_live_runtime_arm,
)
from sports_hedge.execution.package import (
    LIVE_SUBMIT_CLOCK_INVALID,
    LIVE_SUBMIT_EVALUATION_TIME_INVALID,
    LIVE_SUBMIT_EVALUATION_TIME_UNKNOWN,
    LIVE_SUBMIT_QUOTE_AGE_ELAPSED,
    live_submit_freshness_refusal,
)
from sports_hedge.execution.runtime import set_execution_runtime
from sports_hedge.persistence.operator_scanner_settings import (
    bind_runtime_operator_scanner_settings_store,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_wave40_live_canary import (
    PERMITTED,
    _arm,
    _canary_settings,
    _ops,
    _plan,
    _runtime,
    _snapshot,
)

NOW = datetime(2026, 10, 11, 13, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset() -> None:
    set_execution_runtime(None)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(None)
    bind_runtime_operator_scanner_settings_store(None)
    yield
    set_execution_runtime(None)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(None)
    bind_runtime_operator_scanner_settings_store(None)


def _expired_facts() -> LiveCanaryTradingFacts:
    return LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=True,
        polymarket_geoblock_reason="geoblock_permitted",
        polymarket_credentials_present=True,
        polymarket_wallet_valid=True,
        polymarket_balance_readable=True,
        polymarket_trading_ready=True,
        matchbook_credentials_present=True,
        matchbook_authenticated=True,
        matchbook_free_funds_positive=True,
        matchbook_currency_compatible=True,
        collected_at=datetime.now(UTC) - timedelta(minutes=10),
        injected=False,
    )


def test_slow_preflight_does_not_submit_elapsed_price2(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _canary_settings(tmp_path)
    set_live_runtime_arm(True)
    set_live_canary_trading_facts(_expired_facts())

    async def _slow(_settings: Settings) -> LiveCanaryTradingFacts:
        await asyncio.sleep(3.05)
        return PERMITTED

    monkeypatch.setattr(
        "sports_hedge.execution.live_canary.collect_live_canary_facts",
        _slow,
    )
    runtime, mb, pm = _runtime()
    plan = _plan(snapshot=_snapshot(stake="1.50", oldest_quote_age_ms=200))
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "slow.sqlite"), plan)
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert mb.calls == []
    assert pm.calls == []
    assert live_submit_freshness_refusal(
        plan,
        settings=settings,
        now=datetime.now(UTC),
    ) == LIVE_SUBMIT_QUOTE_AGE_ELAPSED


def test_fast_preflight_still_submits_both_legs(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    _arm(settings)
    runtime, mb, pm = _runtime()
    plan = _plan(snapshot=_snapshot(stake="1.50", oldest_quote_age_ms=120))
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "fast.sqlite"), plan)
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert len(mb.calls) == 1
    assert len(pm.calls) == 1


def test_disarm_during_slow_preflight_denies_both_legs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _canary_settings(tmp_path)
    set_live_runtime_arm(True)
    set_live_canary_trading_facts(_expired_facts())

    async def _disarm_then_sleep(_settings: Settings) -> LiveCanaryTradingFacts:
        disarm_live_runtime(settings)
        await asyncio.sleep(0.05)
        return PERMITTED

    monkeypatch.setattr(
        "sports_hedge.execution.live_canary.collect_live_canary_facts",
        _disarm_then_sleep,
    )
    runtime, mb, pm = _runtime()
    plan = _plan()
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "halt.sqlite"), plan)
    try:
        ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    except PaperOperationsError:
        pass
    assert mb.calls == []
    assert pm.calls == []


def test_missing_and_invalid_evaluation_times_fail_closed(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    _arm(settings)
    runtime, mb, _pm = _runtime()
    missing = _plan(snapshot=_snapshot(stake="1.50", evaluated_at=None, started_at=None))
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "miss.sqlite"), missing)
    ops.capture_opening_fill(missing.opportunity_id, now=NOW)
    assert mb.calls == []
    assert (
        live_submit_freshness_refusal(missing, settings=settings)
        == LIVE_SUBMIT_EVALUATION_TIME_UNKNOWN
    )
    naive = _plan(
        snapshot=_snapshot(stake="1.50", evaluated_at="2026-10-11T13:00:00", snapshot_id="exec:naive")
    )
    ops._plans[naive.opportunity_id] = naive
    ops.capture_opening_fill(naive.opportunity_id, now=NOW)
    assert mb.calls == []
    assert (
        live_submit_freshness_refusal(naive, settings=settings)
        == LIVE_SUBMIT_EVALUATION_TIME_INVALID
    )
    future = _plan(
        snapshot=_snapshot(
            stake="1.50",
            evaluated_at=(datetime.now(UTC) + timedelta(seconds=30)).isoformat(),
            snapshot_id="exec:future",
        )
    )
    ops._plans[future.opportunity_id] = future
    ops.capture_opening_fill(future.opportunity_id, now=NOW)
    assert mb.calls == []
    assert (
        live_submit_freshness_refusal(future, settings=settings)
        == LIVE_SUBMIT_EVALUATION_TIME_INVALID
    )
    assert (
        live_submit_freshness_refusal(_plan(), settings=settings, now=datetime(2026, 10, 11, 13))
        == LIVE_SUBMIT_CLOCK_INVALID
    )


def test_fresh_price2_that_is_now_older_than_two_seconds_is_refused() -> None:
    settings = Settings(
        sports_hedge_mode="real",
        sports_hedge_execution_enabled=False,
        paper_entry_max_quote_age_ms=2000,
        paper_execution_max_snapshot_skew_ms=500,
    )
    set_live_runtime_arm(True)
    evaluated = datetime.now(UTC)
    plan = _plan(
        snapshot=_snapshot(
            stake="1.50",
            oldest_quote_age_ms=180,
            evaluated_at=evaluated.isoformat(),
        )
    )
    later = evaluated + timedelta(milliseconds=2500)
    assert (
        live_submit_freshness_refusal(plan, settings=settings, now=later)
        == LIVE_SUBMIT_QUOTE_AGE_ELAPSED
    )
    assert live_submit_freshness_refusal(plan, settings=settings, now=evaluated) is None

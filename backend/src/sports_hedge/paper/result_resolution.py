"""Resolve a PAPER trade's canonical winner from read-only provider evidence.

Never infers a football result from elapsed kickoff time. Fail closed on void,
postponed, abandoned, incomplete, ambiguous, or conflicting evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sports_hedge.domain.football import (
    CanonicalOutcome,
    FootballPeriod,
    MarketFamily,
    line_push_possible,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.paper_assumed import LOCKED_PAPER_FAMILIES
from sports_hedge.paper.canonical_results import is_valid_canonical_settlement_outcome
from sports_hedge.paper.trades import PaperTrade, PaperTradeState

PAPER_AUTO_SETTLEMENT_SOURCE = "paper_auto_settlement"

# Market/event statuses that mean an official result exists. ``closed`` is not
# enough: Matchbook closed means betting stopped, not necessarily graded.
GRADED_RESULT_STATUSES = frozenset(
    {
        "graded",
        "settled",
        "determined",
        "finished",
        "final",
        "completed",
        "complete",
        "paid",
        "settled-complete",
    }
)
VOID_OR_EXCEPTION_STATUSES = frozenset(
    {
        "void",
        "voided",
        "cancelled",
        "canceled",
        "abandoned",
        "postponed",
        "delayed",
        "rescheduled",
        "abandoned-postponed",
    }
)
WINNER_RUNNER_STATUSES = frozenset({"winner", "won", "winning", "successful", "paid"})
LOSER_RUNNER_STATUSES = frozenset({"loser", "lost", "losing", "unsuccessful"})
VOID_RUNNER_STATUSES = frozenset({"void", "voided", "push", "pushed", "dead-heat", "deadheat"})
KALSHI_YES = frozenset({"yes", "true", "1"})
KALSHI_NO = frozenset({"no", "false", "0"})

SUPPORTED_PERIODS = frozenset({FootballPeriod.FULL_TIME, None})


@dataclass(frozen=True)
class ProviderOutcomeEvidence:
    venue: str
    source_event_id: str | None
    source_market_id: str | None
    source_result_id: str | None
    status: str | None
    winning_outcome: str | None
    blocker: str | None
    observed: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ScoreEvidence:
    home_score: int
    away_score: int
    status: str | None
    source_event_id: str | None
    live_score_supported: bool


@dataclass(frozen=True)
class SettlementResolution:
    """Either a labelled winner or a diagnosable fail-closed blocker."""

    winning_outcome: str | None
    blocker: str | None
    source_id: str
    detail: str
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def is_ready(self) -> bool:
        return self.winning_outcome is not None and self.blocker is None


def resolve_paper_trade_settlement(
    trade: PaperTrade,
    *,
    matchbook_market: Mapping[str, Any] | None = None,
    matchbook_event: Mapping[str, Any] | None = None,
    kalshi_markets: Mapping[str, Mapping[str, Any]] | None = None,
) -> SettlementResolution:
    """Map exact-ID provider payloads onto the trade's canonical outcomes."""

    identity_blocker = _identity_blocker(trade)
    if identity_blocker is not None:
        return _blocked(trade, identity_blocker, matchbook_market, matchbook_event, kalshi_markets)

    family_blocker = _family_blocker(trade)
    if family_blocker is not None:
        return _blocked(trade, family_blocker, matchbook_market, matchbook_event, kalshi_markets)

    scores = _score_evidence(matchbook_event, matchbook_market)
    score_outcome, score_blocker = _outcome_from_scores(trade, scores)
    matchbook = _matchbook_market_evidence(trade, matchbook_market)
    kalshi = _kalshi_market_evidence(trade, kalshi_markets or {})
    event_status = _payload_status(matchbook_event)

    from sports_hedge.nfl.settlement import (
        collect_nfl_lifecycle_tokens,
        is_nfl_paper_trade,
        nfl_automatic_settlement_lifecycle_blocker,
        nfl_exceptional_status_blocker,
        nfl_tied_score_blocker,
        NFL_SETTLEMENT_FAIL_CLOSED_REASON,
    )

    if is_nfl_paper_trade(trade):
        nfl_exception = nfl_exceptional_status_blocker(
            matchbook.status,
            kalshi.status,
            event_status,
            None if scores is None else scores.status,
            matchbook.winning_outcome,
            kalshi.winning_outcome,
            matchbook.blocker,
            kalshi.blocker,
        )
        if nfl_exception is not None:
            return _blocked(
                trade,
                nfl_exception,
                matchbook_market,
                matchbook_event,
                kalshi_markets,
                scores,
            )
        if scores is not None:
            tied = nfl_tied_score_blocker(scores.home_score, scores.away_score)
            if tied is not None:
                return _blocked(
                    trade,
                    tied,
                    matchbook_market,
                    matchbook_event,
                    kalshi_markets,
                    scores,
                )
        if trade.market_family is MarketFamily.GAME_WINNER and score_outcome == "draw":
            return _blocked(
                trade,
                NFL_SETTLEMENT_FAIL_CLOSED_REASON,
                matchbook_market,
                matchbook_event,
                kalshi_markets,
                scores,
            )
        lifecycle_blocker = nfl_automatic_settlement_lifecycle_blocker(
            trade,
            *collect_nfl_lifecycle_tokens(
                matchbook_market,
                matchbook_event,
                *list((kalshi_markets or {}).values()),
            ),
            matchbook.status,
            kalshi.status,
            event_status,
            None if scores is None else scores.status,
        )
        if lifecycle_blocker is not None:
            return _blocked(
                trade,
                lifecycle_blocker,
                matchbook_market,
                matchbook_event,
                kalshi_markets,
                scores,
            )

    exception = _exception_blocker(matchbook, kalshi, scores, event_status=event_status)
    if exception is not None:
        return _blocked(trade, exception, matchbook_market, matchbook_event, kalshi_markets, scores)

    derived: list[tuple[str, str]] = []
    if matchbook.winning_outcome:
        derived.append(("matchbook", matchbook.winning_outcome))
    if kalshi.winning_outcome:
        derived.append(("kalshi", kalshi.winning_outcome))
    if score_outcome:
        derived.append(("matchbook_scores", score_outcome))

    if not derived:
        specific = [
            reason
            for reason in (score_blocker, matchbook.blocker, kalshi.blocker)
            if reason and reason != "incomplete_provider_result"
        ]
        reason = next(iter(specific), None) or (
            matchbook.blocker or kalshi.blocker or score_blocker or "incomplete_provider_result"
        )
        return _blocked(trade, reason, matchbook_market, matchbook_event, kalshi_markets, scores)

    winners = {outcome for _source, outcome in derived}
    if len(winners) != 1:
        return _blocked(
            trade,
            "conflicting_provider_results",
            matchbook_market,
            matchbook_event,
            kalshi_markets,
            scores,
            extra={"derived": [{"source": source, "outcome": outcome} for source, outcome in derived]},
        )

    winner = next(iter(winners))
    if not is_valid_canonical_settlement_outcome(trade, winner):
        return _blocked(
            trade,
            "settlement_outcome_not_on_trade",
            matchbook_market,
            matchbook_event,
            kalshi_markets,
            scores,
        )

    if not _authoritative_completion(matchbook, kalshi, scores, score_outcome=score_outcome):
        return _blocked(
            trade,
            "incomplete_provider_result",
            matchbook_market,
            matchbook_event,
            kalshi_markets,
            scores,
        )

    evidence = _evidence_payload(
        trade,
        matchbook_market,
        matchbook_event,
        kalshi_markets,
        scores,
        extra={
            "winning_outcome": winner,
            "derived": [{"source": source, "outcome": outcome} for source, outcome in derived],
            "matchbook": matchbook.observed,
            "kalshi": kalshi.observed,
        },
    )
    source_id = _source_id(trade, winner, matchbook, kalshi, scores)
    return SettlementResolution(
        winning_outcome=winner,
        blocker=None,
        source_id=source_id,
        detail=_detail(evidence),
        evidence=evidence,
    )


def _identity_blocker(trade: PaperTrade) -> str | None:
    if trade.state is PaperTradeState.AWAITING_MANUAL_EXTERNAL:
        return "cannot_settle_unconfirmed_external"
    if trade.state is PaperTradeState.CLOSED:
        return "already_closed"
    if not any(leg.filled_stake > 0 for leg in trade.legs):
        return "cannot_settle_unfilled_trade"
    has_matchbook = any(
        leg.venue is VenueName.MATCHBOOK and _usable_id(leg.source_market_id)
        for leg in trade.legs
        if leg.filled_stake > 0
    )
    has_kalshi = any(
        leg.venue is VenueName.KALSHI
        and _usable_id(leg.source_contract_id or leg.source_market_id)
        for leg in trade.legs
        if leg.filled_stake > 0
    )
    if not has_matchbook and not has_kalshi:
        return "missing_durable_provider_identity"
    if has_matchbook and _matchbook_identity_missing(trade):
        return "missing_durable_provider_identity"
    if has_kalshi and not _kalshi_tickers(trade):
        return "missing_durable_provider_identity"
    return None


def _matchbook_identity_missing(trade: PaperTrade) -> bool:
    event_id = _first_source_event(trade, VenueName.MATCHBOOK)
    market_id = next(
        (
            _usable_id(leg.source_market_id)
            for leg in trade.legs
            if leg.venue is VenueName.MATCHBOOK and leg.filled_stake > 0
        ),
        None,
    )
    if not event_id or not market_id:
        return True
    canonical = str(trade.canonical_event_id or "").strip()
    if event_id == canonical and not event_id.isdigit():
        return True
    return False


def _usable_id(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text or text.casefold() in {"unknown", "none", "null"}:
        return None
    return text


def _kalshi_tickers(trade: PaperTrade) -> list[str]:
    tickers: list[str] = []
    for leg in trade.legs:
        if leg.venue is not VenueName.KALSHI:
            continue
        ticker = _usable_id(leg.source_contract_id or leg.source_market_id)
        if ticker and ticker not in tickers:
            tickers.append(ticker)
    return tickers


def _family_blocker(trade: PaperTrade) -> str | None:
    from sports_hedge.nfl.detect import is_nfl_market_family
    from sports_hedge.nfl.markets import is_exact_half_line
    from sports_hedge.nfl.settlement import is_nfl_paper_trade

    family = trade.market_family
    if is_nfl_paper_trade(trade) or is_nfl_market_family(family):
        if family not in {
            MarketFamily.GAME_WINNER,
            MarketFamily.POINT_SPREAD,
            MarketFamily.TOTAL_POINTS,
        }:
            return "unsupported_market_family"
        if trade.period not in SUPPORTED_PERIODS and trade.period is not FootballPeriod.FULL_TIME:
            return "unsupported_settlement_period"
        if family in {MarketFamily.POINT_SPREAD, MarketFamily.TOTAL_POINTS}:
            line = _line_from_trade(trade)
            if line is None or not is_exact_half_line(line):
                return "unsupported_nfl_line"
        return None
    if family is None or family not in LOCKED_PAPER_FAMILIES:
        return "unsupported_market_family"
    if trade.period not in SUPPORTED_PERIODS and trade.period is not FootballPeriod.FULL_TIME:
        return "unsupported_settlement_period"
    scope, extra_time, penalties = _fingerprint_flags(trade.settlement_key)
    if scope in {"including_extra_time", "including_penalties"}:
        return "unsupported_settlement_scope"
    if extra_time is True or penalties is True:
        return "unsupported_settlement_scope"
    if family is MarketFamily.TOTAL_GOALS:
        line = _line_from_trade(trade)
        if line is None:
            return "missing_total_line"
        if line_push_possible(line) is not False:
            return "unsupported_total_line"
    return None


def _exception_blocker(
    matchbook: ProviderOutcomeEvidence,
    kalshi: ProviderOutcomeEvidence,
    scores: ScoreEvidence | None,
    *,
    event_status: str | None = None,
) -> str | None:
    for status in (
        matchbook.status,
        kalshi.status,
        event_status,
        None if scores is None else scores.status,
    ):
        normalized = _norm(status)
        if normalized in VOID_OR_EXCEPTION_STATUSES:
            return f"provider_status_{normalized}"
    if matchbook.blocker and matchbook.blocker.startswith("provider_status_"):
        return matchbook.blocker
    if kalshi.blocker and kalshi.blocker.startswith("provider_status_"):
        return kalshi.blocker
    if matchbook.blocker in {"ambiguous_matchbook_winners", "void_matchbook_runner"}:
        return matchbook.blocker
    if kalshi.blocker in {"ambiguous_kalshi_results", "conflicting_kalshi_results"}:
        return kalshi.blocker
    return None


def _authoritative_completion(
    matchbook: ProviderOutcomeEvidence,
    kalshi: ProviderOutcomeEvidence,
    scores: ScoreEvidence | None,
    *,
    score_outcome: str | None,
) -> bool:
    """Require a completed/graded/settled provider state, never elapsed time."""

    scores_done = (
        scores is not None and _is_graded(scores.status) and score_outcome is not None
    )
    if scores_done:
        return True
    matchbook_done = matchbook.winning_outcome is not None and _is_graded(matchbook.status)
    kalshi_done = kalshi.winning_outcome is not None and _is_graded(kalshi.status)
    if matchbook_done and kalshi_done:
        return True
    if matchbook_done and not kalshi.source_market_id:
        return True
    if kalshi_done and not matchbook.source_market_id:
        return True
    return False


def _matchbook_market_evidence(
    trade: PaperTrade,
    payload: Mapping[str, Any] | None,
) -> ProviderOutcomeEvidence:
    market = extract_matchbook_market_payload(payload) if payload is not None else None
    mb_legs = [leg for leg in trade.legs if leg.venue is VenueName.MATCHBOOK]
    event_id = _first_source_event(trade, VenueName.MATCHBOOK)
    market_id = next((str(leg.source_market_id) for leg in mb_legs if leg.source_market_id), None)
    if market is None:
        return ProviderOutcomeEvidence(
            venue=VenueName.MATCHBOOK.value,
            source_event_id=event_id,
            source_market_id=market_id,
            source_result_id=None,
            status=None,
            winning_outcome=None,
            blocker=None if not mb_legs else "incomplete_provider_result",
            observed={},
        )
    status = _payload_status(market)
    if status in VOID_OR_EXCEPTION_STATUSES:
        return ProviderOutcomeEvidence(
            venue=VenueName.MATCHBOOK.value,
            source_event_id=event_id,
            source_market_id=str(market.get("id") or market_id or ""),
            source_result_id=str(market.get("id") or ""),
            status=status,
            winning_outcome=None,
            blocker=f"provider_status_{status}",
            observed={"status": status},
        )
    runners = market.get("runners") if isinstance(market.get("runners"), list) else []
    winners: list[str] = []
    voided = False
    for leg in mb_legs:
        runner = _runner_for_leg(runners, leg.source_runner_id, leg.outcome)
        if runner is None:
            continue
        runner_status = _runner_status(runner)
        if runner_status in VOID_RUNNER_STATUSES:
            voided = True
        if _runner_is_winner(runner):
            winners.append(leg.outcome)
    if voided:
        return ProviderOutcomeEvidence(
            venue=VenueName.MATCHBOOK.value,
            source_event_id=event_id,
            source_market_id=str(market.get("id") or market_id or ""),
            source_result_id=str(market.get("id") or ""),
            status=status,
            winning_outcome=None,
            blocker="void_matchbook_runner",
            observed={"status": status, "winners": winners},
        )
    unique = sorted(set(winners))
    if len(unique) > 1:
        return ProviderOutcomeEvidence(
            venue=VenueName.MATCHBOOK.value,
            source_event_id=event_id,
            source_market_id=str(market.get("id") or market_id or ""),
            source_result_id=str(market.get("id") or ""),
            status=status,
            winning_outcome=None,
            blocker="ambiguous_matchbook_winners",
            observed={"status": status, "winners": unique},
        )
    winner = unique[0] if len(unique) == 1 and _is_graded(status) else None
    return ProviderOutcomeEvidence(
        venue=VenueName.MATCHBOOK.value,
        source_event_id=event_id,
        source_market_id=str(market.get("id") or market_id or ""),
        source_result_id=str(market.get("id") or ""),
        status=status,
        winning_outcome=winner,
        blocker=None if winner or not _is_graded(status) else "incomplete_provider_result",
        observed={"status": status, "winners": unique},
    )


def _kalshi_market_evidence(
    trade: PaperTrade,
    markets: Mapping[str, Mapping[str, Any]],
) -> ProviderOutcomeEvidence:
    kalshi_legs = [leg for leg in trade.legs if leg.venue is VenueName.KALSHI]
    event_id = _first_source_event(trade, VenueName.KALSHI)
    tickers = _kalshi_tickers(trade)
    if not kalshi_legs:
        return ProviderOutcomeEvidence(
            venue=VenueName.KALSHI.value,
            source_event_id=event_id,
            source_market_id=None,
            source_result_id=None,
            status=None,
            winning_outcome=None,
            blocker=None,
            observed={},
        )
    if not markets:
        return ProviderOutcomeEvidence(
            venue=VenueName.KALSHI.value,
            source_event_id=event_id,
            source_market_id=tickers[0] if tickers else None,
            source_result_id=None,
            status=None,
            winning_outcome=None,
            blocker="incomplete_provider_result",
            observed={},
        )
    by_ticker: dict[str, list[Any]] = {}
    for leg in kalshi_legs:
        ticker = str(leg.source_contract_id or leg.source_market_id or "").strip()
        if not ticker:
            continue
        by_ticker.setdefault(ticker, []).append(leg)
    yes_outcomes: list[str] = []
    statuses: list[str] = []
    result_ids: list[str] = []
    observed_results: dict[str, str | None] = {}
    for ticker, legs in by_ticker.items():
        payload = _kalshi_market_object(markets.get(ticker))
        if payload is None:
            continue
        status = _payload_status(payload)
        if status:
            statuses.append(status)
        if status in VOID_OR_EXCEPTION_STATUSES:
            return ProviderOutcomeEvidence(
                venue=VenueName.KALSHI.value,
                source_event_id=event_id,
                source_market_id=ticker,
                source_result_id=str(payload.get("ticker") or ticker),
                status=status,
                winning_outcome=None,
                blocker=f"provider_status_{status}",
                observed={"status": status, "ticker": ticker},
            )
        result = _norm(payload.get("result") or payload.get("settlement_result"))
        observed_results[ticker] = result
        result_ids.append(str(payload.get("ticker") or ticker))
        winner = _kalshi_ticker_winner(legs, result)
        if winner:
            yes_outcomes.append(winner)
    unique_yes = sorted(set(yes_outcomes))
    if len(unique_yes) > 1:
        return ProviderOutcomeEvidence(
            venue=VenueName.KALSHI.value,
            source_event_id=event_id,
            source_market_id=tickers[0] if tickers else None,
            source_result_id=",".join(result_ids),
            status=_strictest_status(statuses),
            winning_outcome=None,
            blocker="conflicting_kalshi_results",
            observed={"yes": unique_yes, "statuses": statuses, "results": observed_results},
        )
    status = _strictest_status(statuses)
    winner = unique_yes[0] if len(unique_yes) == 1 and _is_graded(status) else None
    if _is_graded(status) and not unique_yes:
        return ProviderOutcomeEvidence(
            venue=VenueName.KALSHI.value,
            source_event_id=event_id,
            source_market_id=tickers[0] if tickers else None,
            source_result_id=",".join(result_ids),
            status=status,
            winning_outcome=None,
            blocker="incomplete_provider_result",
            observed={"yes": unique_yes, "statuses": statuses, "results": observed_results},
        )
    return ProviderOutcomeEvidence(
        venue=VenueName.KALSHI.value,
        source_event_id=event_id,
        source_market_id=tickers[0] if tickers else None,
        source_result_id=",".join(result_ids),
        status=status,
        winning_outcome=winner,
        blocker=None,
        observed={"yes": unique_yes, "statuses": statuses, "results": observed_results},
    )


def _kalshi_ticker_winner(legs: list[Any], result: str | None) -> str | None:
    outcomes = [str(leg.outcome) for leg in legs]
    if result in KALSHI_YES:
        for candidate in (
            CanonicalOutcome.YES.value,
            CanonicalOutcome.OVER.value,
        ):
            if candidate in outcomes:
                return candidate
        if len(outcomes) == 1:
            return outcomes[0]
        return None
    if result in KALSHI_NO:
        for candidate in (
            CanonicalOutcome.NO.value,
            CanonicalOutcome.UNDER.value,
        ):
            if candidate in outcomes:
                return candidate
        complements = {
            CanonicalOutcome.YES.value: CanonicalOutcome.NO.value,
            CanonicalOutcome.OVER.value: CanonicalOutcome.UNDER.value,
        }
        for outcome in outcomes:
            complement = complements.get(outcome)
            if complement:
                return complement
        return None
    return None


def _score_evidence(
    event_payload: Mapping[str, Any] | None,
    market_payload: Mapping[str, Any] | None,
) -> ScoreEvidence | None:
    candidates: list[Mapping[str, Any]] = []
    if isinstance(event_payload, Mapping):
        nested = event_payload.get("event")
        if isinstance(nested, Mapping):
            candidates.append(nested)
        events = event_payload.get("events")
        if isinstance(events, list):
            for item in events:
                if isinstance(item, Mapping):
                    candidates.append(item)
        candidates.append(event_payload)
    if isinstance(market_payload, Mapping):
        nested = market_payload.get("event")
        if isinstance(nested, Mapping):
            candidates.append(nested)
        candidates.append(market_payload)
    for payload in candidates:
        home, away = _explicit_scores(payload)
        if home is None or away is None:
            continue
        return ScoreEvidence(
            home_score=home,
            away_score=away,
            status=_payload_status(payload),
            source_event_id=str(payload.get("id") or payload.get("event-id") or "") or None,
            live_score_supported=True,
        )
    return None


def _explicit_scores(payload: Mapping[str, Any]) -> tuple[int | None, int | None]:
    home = _optional_non_negative_int(
        _first_present(payload, "home-score", "home_score", "homeScore")
    )
    away = _optional_non_negative_int(
        _first_present(payload, "away-score", "away_score", "awayScore")
    )
    nested = payload.get("score")
    if isinstance(nested, Mapping):
        if home is None:
            home = _optional_non_negative_int(
                _first_present(nested, "home", "home-score", "home_score")
            )
        if away is None:
            away = _optional_non_negative_int(
                _first_present(nested, "away", "away-score", "away_score")
            )
    return home, away


def _first_present(payload: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed


def _outcome_from_scores(
    trade: PaperTrade,
    scores: ScoreEvidence | None,
) -> tuple[str | None, str | None]:
    if scores is None:
        return None, None
    if scores.status in VOID_OR_EXCEPTION_STATUSES:
        return None, f"provider_status_{scores.status}"
    if not _is_graded(scores.status):
        return None, "incomplete_provider_result"
    family = trade.market_family
    home, away = scores.home_score, scores.away_score
    if family is MarketFamily.MATCH_RESULT:
        if home > away:
            return CanonicalOutcome.HOME.value, None
        if away > home:
            return CanonicalOutcome.AWAY.value, None
        return CanonicalOutcome.DRAW.value, None
    if family is MarketFamily.GAME_WINNER:
        from sports_hedge.nfl.constants import NFL_SETTLEMENT_FAIL_CLOSED_REASON

        if home > away:
            return CanonicalOutcome.HOME.value, None
        if away > home:
            return CanonicalOutcome.AWAY.value, None
        return None, NFL_SETTLEMENT_FAIL_CLOSED_REASON
    if family is MarketFamily.POINT_SPREAD:
        from sports_hedge.nfl.markets import is_exact_half_line

        line = _line_from_trade(trade)
        if line is None or not is_exact_half_line(line):
            return None, "unsupported_nfl_line"
        margin = Decimal(home - away)
        if margin + line > 0:
            return CanonicalOutcome.HOME.value, None
        if margin + line < 0:
            return CanonicalOutcome.AWAY.value, None
        from sports_hedge.nfl.constants import NFL_SETTLEMENT_FAIL_CLOSED_REASON

        return None, NFL_SETTLEMENT_FAIL_CLOSED_REASON
    if family is MarketFamily.TOTAL_POINTS:
        from sports_hedge.nfl.markets import is_exact_half_line

        line = _line_from_trade(trade)
        if line is None or not is_exact_half_line(line):
            return None, "unsupported_nfl_line"
        total = Decimal(home + away)
        if total > line:
            return CanonicalOutcome.OVER.value, None
        if total < line:
            return CanonicalOutcome.UNDER.value, None
        from sports_hedge.nfl.constants import NFL_SETTLEMENT_FAIL_CLOSED_REASON

        return None, NFL_SETTLEMENT_FAIL_CLOSED_REASON
    if family is MarketFamily.BOTH_TEAMS_TO_SCORE:
        if home > 0 and away > 0:
            return CanonicalOutcome.YES.value, None
        return CanonicalOutcome.NO.value, None
    if family is MarketFamily.TOTAL_GOALS:
        line = _line_from_trade(trade)
        if line is None:
            return None, "missing_total_line"
        if line_push_possible(line) is not False:
            return None, "unsupported_total_line"
        total = Decimal(home + away)
        if total > line:
            return CanonicalOutcome.OVER.value, None
        if total < line:
            return CanonicalOutcome.UNDER.value, None
        return None, "ambiguous_total_push"
    if family is MarketFamily.FIRST_TEAM_TO_SCORE:
        if home == 0 and away == 0:
            return CanonicalOutcome.NO_GOAL.value, None
        if home > 0 and away == 0:
            return CanonicalOutcome.HOME.value, None
        if away > 0 and home == 0:
            return CanonicalOutcome.AWAY.value, None
        return None, "ftts_requires_first_goal_evidence"
    return None, "unsupported_market_family"


def _blocked(
    trade: PaperTrade,
    reason: str,
    matchbook_market: Mapping[str, Any] | None,
    matchbook_event: Mapping[str, Any] | None,
    kalshi_markets: Mapping[str, Mapping[str, Any]] | None,
    scores: ScoreEvidence | None = None,
    extra: dict[str, Any] | None = None,
) -> SettlementResolution:
    evidence = _evidence_payload(
        trade, matchbook_market, matchbook_event, kalshi_markets, scores, extra=extra
    )
    evidence["blocker"] = reason
    return SettlementResolution(
        winning_outcome=None,
        blocker=reason,
        source_id=_source_id(trade, reason, None, None, scores),
        detail=_detail(evidence),
        evidence=evidence,
    )


def _evidence_payload(
    trade: PaperTrade,
    matchbook_market: Mapping[str, Any] | None,
    matchbook_event: Mapping[str, Any] | None,
    kalshi_markets: Mapping[str, Mapping[str, Any]] | None,
    scores: ScoreEvidence | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "trade_id": trade.trade_id,
        "canonical_event_id": trade.canonical_event_id,
        "canonical_market_id": trade.canonical_market_id,
        "market_family": None if trade.market_family is None else trade.market_family.value,
        "period": None if trade.period is None else trade.period.value,
        "matchbook_event_id": _first_source_event(trade, VenueName.MATCHBOOK),
        "matchbook_market_id": _first_source_market(trade, VenueName.MATCHBOOK),
        "kalshi_event_id": _first_source_event(trade, VenueName.KALSHI),
        "kalshi_tickers": _kalshi_tickers(trade),
        "matchbook_market_status": _payload_status(extract_matchbook_market_payload(matchbook_market))
        if matchbook_market
        else None,
        "matchbook_event_status": _payload_status(matchbook_event) if matchbook_event else None,
        "scores": None
        if scores is None
        else {
            "home": scores.home_score,
            "away": scores.away_score,
            "status": scores.status,
            "source_event_id": scores.source_event_id,
        },
        "kickoff_not_used": True,
    }
    if extra:
        payload.update(extra)
    if kalshi_markets:
        payload["kalshi_statuses"] = {
            ticker: _payload_status(_kalshi_market_object(item))
            for ticker, item in kalshi_markets.items()
        }
        payload["kalshi_results"] = {
            ticker: _norm((_kalshi_market_object(item) or {}).get("result"))
            for ticker, item in kalshi_markets.items()
        }
    return payload


def _source_id(
    trade: PaperTrade,
    token: str,
    matchbook: ProviderOutcomeEvidence | None,
    kalshi: ProviderOutcomeEvidence | None,
    scores: ScoreEvidence | None,
) -> str:
    mb = None if matchbook is None else matchbook.source_result_id or matchbook.source_market_id
    ks = None if kalshi is None else kalshi.source_result_id
    score_id = None if scores is None else f"{scores.home_score}-{scores.away_score}"
    parts = [trade.trade_id, token, mb or "", ks or "", score_id or ""]
    return "auto:" + ":".join(part for part in parts if part)


def _detail(evidence: dict[str, Any]) -> str:
    family = evidence.get("market_family") or "unknown"
    winner = evidence.get("winning_outcome")
    blocker = evidence.get("blocker")
    scores = evidence.get("scores") or {}
    score_label = ""
    if scores.get("home") is not None and scores.get("away") is not None:
        score_label = f" score={scores['home']}-{scores['away']}"
    if winner:
        return (
            f"PAPER auto-settlement family={family} outcome={winner}{score_label} "
            f"matchbook={evidence.get('matchbook_event_id')}/{evidence.get('matchbook_market_id')} "
            f"kalshi={','.join(evidence.get('kalshi_tickers') or ())}"
        )
    return (
        f"PAPER auto-settlement blocked reason={blocker} family={family}{score_label} "
        f"matchbook={evidence.get('matchbook_event_id')}/{evidence.get('matchbook_market_id')}"
    )


def _line_from_trade(trade: PaperTrade) -> Decimal | None:
    if trade.line is not None:
        return trade.line
    parts = (trade.settlement_key or "").split("|")
    if len(parts) >= 3 and parts[2]:
        try:
            return Decimal(parts[2])
        except (InvalidOperation, ValueError):
            pass
    label = str(trade.market_label or "")
    for token in label.replace(",", " ").split():
        try:
            value = Decimal(token)
        except (InvalidOperation, ValueError):
            continue
        if value > 0:
            return value
    return None


def _fingerprint_flags(key: str | None) -> tuple[str | None, bool | None, bool | None]:
    parts = (key or "").split("|")
    scope = parts[0] or None if parts else None
    penalties = _optional_bool(parts[4]) if len(parts) > 4 else None
    extra_time = _optional_bool(parts[5]) if len(parts) > 5 else None
    return scope, extra_time, penalties


def _optional_bool(value: str | None) -> bool | None:
    text = str(value or "").strip().casefold()
    if text in {"true", "1"}:
        return True
    if text in {"false", "0"}:
        return False
    return None


def _first_source_event(trade: PaperTrade, venue: VenueName) -> str | None:
    for leg in trade.legs:
        if leg.venue is venue and str(leg.source_event_id or "").strip():
            return str(leg.source_event_id)
    return None


def _first_source_market(trade: PaperTrade, venue: VenueName) -> str | None:
    for leg in trade.legs:
        if leg.venue is venue and str(leg.source_market_id or "").strip():
            return str(leg.source_market_id)
    return None


def _payload_status(payload: Mapping[str, Any] | None) -> str | None:
    if not isinstance(payload, Mapping):
        return None
    return _norm(payload.get("status") or payload.get("state") or payload.get("venue_status"))


def _is_graded(status: str | None) -> bool:
    return _norm(status) in GRADED_RESULT_STATUSES


def _norm(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().casefold()
    return text or None


def _runner_for_leg(runners: list[Any], runner_id: str | None, outcome: str) -> Mapping[str, Any] | None:
    wanted = str(runner_id or "").strip()
    for item in runners:
        if not isinstance(item, Mapping):
            continue
        if wanted and str(item.get("id") or "").strip() == wanted:
            return item
    if wanted:
        return None
    for item in runners:
        if not isinstance(item, Mapping):
            continue
        name = _norm(item.get("name") or item.get("runner-name"))
        if name == _norm(outcome):
            return item
    return None


def _runner_status(runner: Mapping[str, Any]) -> str | None:
    return _norm(runner.get("status") or runner.get("state") or runner.get("result-status"))


def _runner_is_winner(runner: Mapping[str, Any]) -> bool:
    if runner.get("winner") is True:
        return True
    status = _runner_status(runner)
    if status in WINNER_RUNNER_STATUSES:
        return True
    result = _norm(runner.get("result"))
    return result in WINNER_RUNNER_STATUSES | {"win", "won"}


def _kalshi_market_object(payload: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    nested = payload.get("market")
    if isinstance(nested, Mapping):
        return dict(nested)
    return dict(payload)


def _strictest_status(statuses: list[str]) -> str | None:
    normalized = [_norm(item) for item in statuses if _norm(item)]
    for status in VOID_OR_EXCEPTION_STATUSES:
        if status in normalized:
            return status
    for status in GRADED_RESULT_STATUSES:
        if status in normalized:
            return status
    return normalized[0] if normalized else None


def extract_matchbook_market_payload(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    market = payload.get("market")
    if isinstance(market, dict) and _looks_like_matchbook_market(market):
        return market
    markets = payload.get("markets")
    if isinstance(markets, list):
        for item in markets:
            if isinstance(item, dict) and _looks_like_matchbook_market(item):
                return item
    if _looks_like_matchbook_market(payload):
        return dict(payload)
    return None


def _looks_like_matchbook_market(payload: Mapping[str, Any]) -> bool:
    if payload.get("runners") is not None:
        return True
    return payload.get("id") is not None and (
        payload.get("name") is not None or payload.get("status") is not None
    )

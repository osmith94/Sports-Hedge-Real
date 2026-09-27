"""Evidence-backed Kalshi / Polymarket / Matchbook NCAAB normalisation.

Structurally supports GAME_WINNER and exact x.5 spread/total. PAPER admission
stays with the empty register.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.ncaab.constants import NCAAB_COMPETITION, NCAAB_SPORT
from sports_hedge.ncaab.detect import (
    approved_kalshi_ncaab_series,
    is_ncaab_kalshi_ticker,
    rejected_kalshi_ncaab_series,
)
from sports_hedge.ncaab.labels import ncaab_operator_market_label
from sports_hedge.ncaab.markets import (
    is_exact_half_line,
    ncaab_period_from_text,
    parse_over_points_line,
    parse_wins_by_over_line,
)
from sports_hedge.ncaab.settlement import ncaab_paper_settlement
from sports_hedge.ncaab.teams import require_resolved_ncaab_team, resolve_ncaab_team
from sports_hedge.normalization.text import is_money_line_label, normalize_text
from sports_hedge.normalization.venues import VenueNormalizationError, _first, _list_field, _parse_datetime


def _required_ncaab_team(value: str | None) -> str:
    try:
        return require_resolved_ncaab_team(value)
    except ValueError as exc:
        raise VenueNormalizationError(str(exc)) from exc


_NCAAB_PERIOD_MARKERS = (
    "1st half",
    "first half",
    "2nd half",
    "second half",
    "1st quarter",
    "2nd quarter",
    "3rd quarter",
    "4th quarter",
    "q1",
    "q2",
    "q3",
    "q4",
)


def _kickoff(value: Any) -> datetime:
    parsed = _parse_datetime(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _require_half_line(line: Decimal | None, *, what: str) -> Decimal:
    if line is None or not is_exact_half_line(line):
        raise VenueNormalizationError(f"NCAAB {what} requires an exact half-point line")
    return line


def _reject_period_or_prop(text: str, *, series_head: str | None = None) -> None:
    combined = normalize_text(text)
    if series_head and rejected_kalshi_ncaab_series(series_head):
        raise VenueNormalizationError(f"unsupported NCAAB Kalshi series: {series_head}")
    if any(token in combined for token in ("player", "prop", "first score", "winning margin", "exact margin")):
        raise VenueNormalizationError("NCAAB player props and derivatives are unsupported")
    if "team total" in combined:
        raise VenueNormalizationError("NCAAB team totals are unsupported")
    if ncaab_period_from_text(text) is None:
        raise VenueNormalizationError("NCAAB period / overtime-only markets are unsupported")
    for marker in _NCAAB_PERIOD_MARKERS:
        if marker in combined:
            raise VenueNormalizationError("NCAAB period / overtime-only markets are unsupported")


def kalshi_ncaab_event(
    payload: dict[str, Any],
    *,
    series: dict[str, Any] | None = None,
) -> CanonicalEvent:
    source_id = str(_first(payload, "event_ticker", "ticker", "id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Kalshi NCAAB event has no event_ticker")
    series_ticker = str(
        payload.get("series_ticker") or (series or {}).get("ticker") or source_id
    )
    if rejected_kalshi_ncaab_series(series_ticker) or rejected_kalshi_ncaab_series(source_id):
        raise VenueNormalizationError(f"unsupported NCAAB Kalshi series: {series_ticker}")
    if approved_kalshi_ncaab_series(series_ticker) is None and not is_ncaab_kalshi_ticker(source_id):
        raise VenueNormalizationError("Kalshi payload is not an approved NCAAB series")
    home, away, kickoff = _kalshi_home_away_kickoff(payload)
    return CanonicalEvent(
        sport=NCAAB_SPORT,
        competition=NCAAB_COMPETITION,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=VenueName.KALSHI,
        source_event_id=source_id,
        confidence=1.0,
    )


def kalshi_ncaab_markets(
    event: CanonicalEvent,
    payloads: list[dict[str, Any]],
    *,
    series: dict[str, Any] | None = None,
    event_payload: dict[str, Any] | None = None,
) -> list[CanonicalMarket]:
    series_ticker = str(
        (event_payload or {}).get("series_ticker")
        or (series or {}).get("ticker")
        or event.source_event_id
    )
    family_head = approved_kalshi_ncaab_series(series_ticker)
    if family_head is None:
        raise VenueNormalizationError(f"unsupported NCAAB Kalshi series: {series_ticker}")
    if family_head == "KXNCAAMBGAME":
        return [_assemble_kalshi_game_winner(event, payloads)]
    markets: list[CanonicalMarket] = []
    for payload in payloads:
        markets.append(_kalshi_binary_market(event, payload, series_head=family_head))
    return markets


def polymarket_ncaab_event(payload: dict[str, Any]) -> CanonicalEvent:
    source_id = str(_first(payload, "id", "event_id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Polymarket NCAAB event has no id")
    teams = payload.get("teams")
    if not isinstance(teams, list) or len(teams) < 2:
        raise VenueNormalizationError("Polymarket NCAAB event is missing teams[].ordering")
    by_order: dict[str, str] = {}
    for item in teams:
        if not isinstance(item, dict):
            continue
        ordering = normalize_text(str(item.get("ordering") or ""))
        name = str(item.get("name") or item.get("alias") or "").strip()
        if ordering in {"home", "away"} and name:
            by_order[ordering] = _required_ncaab_team(name)
    if "home" not in by_order or "away" not in by_order:
        raise VenueNormalizationError("Polymarket NCAAB home/away ordering is incomplete")
    start = _first(payload, "startTime", "start_time")
    if not start:
        raise VenueNormalizationError("Polymarket NCAAB event has no startTime tipoff")
    return CanonicalEvent(
        sport=NCAAB_SPORT,
        competition=NCAAB_COMPETITION,
        home_team=by_order["home"],
        away_team=by_order["away"],
        kickoff_utc=_kickoff(start),
        source_venue=VenueName.POLYMARKET,
        source_event_id=source_id,
        confidence=1.0,
    )


def is_fabricated_polymarket_clob_token(
    token: str,
    *,
    condition_id: str = "",
    market_id: str = "",
) -> bool:
    text = str(token or "").strip()
    if not text:
        return True
    condition = str(condition_id or "").strip()
    market = str(market_id or "").strip()
    fabricated = set()
    for prefix in (condition, market):
        if not prefix:
            continue
        fabricated.update({f"{prefix}:0", f"{prefix}:1", f"{prefix}:YES", f"{prefix}:NO"})
    if text in fabricated:
        return True
    if ":" in text:
        head, tail = text.rsplit(":", 1)
        if tail in {"0", "1", "YES", "NO"} and (
            head == condition or head == market or head.startswith("0x")
        ):
            return True
    return False


def exact_polymarket_clob_token_ids(
    payload: dict[str, Any],
    *,
    market_id: str = "",
    required: int | None = None,
) -> list[str]:
    tokens = [
        str(item).strip()
        for item in _list_field(
            _first(payload, "clobTokenIds", "clob_token_ids", "tokenIds", "token_ids")
        )
        if str(item).strip()
    ]
    condition_id = str(_first(payload, "conditionId", "condition_id") or "").strip()
    source_market_id = str(market_id or _first(payload, "id") or "").strip()
    if required is not None and len(tokens) != required:
        raise VenueNormalizationError(
            "Polymarket NCAAB market is missing exact CLOB token IDs"
        )
    if not tokens:
        raise VenueNormalizationError(
            "Polymarket NCAAB market is missing exact CLOB token IDs"
        )
    for token in tokens:
        if is_fabricated_polymarket_clob_token(
            token, condition_id=condition_id, market_id=source_market_id
        ):
            raise VenueNormalizationError(
                "Polymarket NCAAB CLOB token IDs are not executable provider-native IDs"
            )
    return tokens


def polymarket_ncaab_market(event: CanonicalEvent, payload: dict[str, Any]) -> CanonicalMarket:
    source_market_id = str(_first(payload, "id", "conditionId", "condition_id") or "").strip()
    if not source_market_id:
        raise VenueNormalizationError("Polymarket NCAAB market has no id")
    question = str(_first(payload, "question", "title", "groupItemTitle") or "")
    sports_type = normalize_text(str(_first(payload, "sportsMarketType", "sports_market_type") or ""))
    if sports_type not in {"moneyline", "spreads", "spread", "totals", "total"}:
        raise VenueNormalizationError(f"unsupported Polymarket NCAAB market type: {sports_type or question}")
    _reject_period_or_prop(f"{sports_type} {question} {payload.get('slug') or ''}")
    outcomes = [str(item) for item in _list_field(payload.get("outcomes"))]
    token_ids = exact_polymarket_clob_token_ids(
        payload, market_id=source_market_id, required=len(outcomes) if outcomes else 2
    )
    if sports_type == "moneyline":
        family = MarketFamily.GAME_WINNER
        line = None
        runners = _team_runners(
            event,
            outcomes,
            token_ids,
            source_market_id,
        )
    elif sports_type in {"spreads", "spread"}:
        family = MarketFamily.POINT_SPREAD
        line = _polymarket_named_spread_line(event, payload, question)
        runners = _spread_runners_from_named_outcomes(
            event, outcomes, token_ids, source_market_id
        )
    elif sports_type in {"totals", "total"}:
        family = MarketFamily.TOTAL_POINTS
        raw_line = payload.get("line")
        try:
            line = _require_half_line(Decimal(str(raw_line)), what="total")
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise VenueNormalizationError("Polymarket NCAAB total has no exact half-point line") from exc
        runners = _over_under_runners(outcomes, token_ids, source_market_id)
    else:
        raise VenueNormalizationError(f"unsupported Polymarket NCAAB market type: {sports_type or question}")
    if len({runner.outcome for runner in runners}) != 2:
        raise VenueNormalizationError("Polymarket NCAAB market is not a two-team / two-side book")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.POLYMARKET,
        source_market_id=source_market_id,
        family=family,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=ncaab_paper_settlement(family=family, line=line),
        runners=runners,
        confidence=1.0,
    )


def matchbook_ncaab_event(payload: dict[str, Any]) -> CanonicalEvent:
    source_id = str(payload.get("id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Matchbook NCAAB event has no id")
    title = str(payload.get("name") or "").strip()
    away, home = _split_away_at_home(title)
    start = _first(payload, "start", "start-time", "start_time")
    if not start:
        raise VenueNormalizationError("Matchbook NCAAB event has no start tipoff")
    return CanonicalEvent(
        sport=NCAAB_SPORT,
        competition=NCAAB_COMPETITION,
        home_team=_required_ncaab_team(home),
        away_team=_required_ncaab_team(away),
        kickoff_utc=_kickoff(start),
        source_venue=VenueName.MATCHBOOK,
        source_event_id=source_id,
        confidence=1.0,
    )


def matchbook_ncaab_market(event: CanonicalEvent, payload: dict[str, Any]) -> CanonicalMarket:
    source_market_id = str(payload.get("id") or "").strip()
    if not source_market_id:
        raise VenueNormalizationError("Matchbook NCAAB market has no id")
    name = str(payload.get("name") or "")
    market_type = normalize_text(str(payload.get("market-type") or payload.get("market_type") or ""))
    _reject_period_or_prop(f"{name} {market_type}")
    runners_payload = [item for item in (payload.get("runners") or []) if isinstance(item, dict)]
    if not runners_payload:
        raise VenueNormalizationError(f"Matchbook NCAAB market {source_market_id} has no runners")
    if is_money_line_label(market_type) or is_money_line_label(name):
        family = MarketFamily.GAME_WINNER
        line = None
        runners = [
            CanonicalRunner(
                source_runner_id=str(item.get("id") or ""),
                outcome=_team_outcome(event, str(item.get("name") or "")),
                label=str(item.get("name") or ""),
            )
            for item in runners_payload
        ]
    elif market_type == "handicap" or normalize_text(name) == "handicap":
        family = MarketFamily.POINT_SPREAD
        line, runners = _matchbook_spread(event, runners_payload)
    elif market_type == "total" or normalize_text(name) == "total":
        family = MarketFamily.TOTAL_POINTS
        line, runners = _matchbook_total(runners_payload)
    else:
        raise VenueNormalizationError(f"unsupported Matchbook NCAAB market: {name}")
    outcomes = {runner.outcome for runner in runners}
    if CanonicalOutcome.OTHER in outcomes:
        raise VenueNormalizationError("Matchbook NCAAB market has an unmapped runner")
    if family is MarketFamily.GAME_WINNER and outcomes != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
        raise VenueNormalizationError("Matchbook NCAAB moneyline is not a two-team winner")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.MATCHBOOK,
        source_market_id=source_market_id,
        family=family,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=ncaab_paper_settlement(family=family, line=line),
        runners=runners,
        confidence=1.0,
    )


def _kalshi_home_away_kickoff(payload: dict[str, Any]) -> tuple[str, str, datetime]:
    milestone = payload.get("milestone")
    if not isinstance(milestone, dict):
        raise VenueNormalizationError(
            "Kalshi NCAAB event has no milestone; expiration/occurrence is not tipoff"
        )
    start = milestone.get("start_date")
    if not start:
        raise VenueNormalizationError("Kalshi NCAAB milestone has no start_date tipoff")
    details = milestone.get("details") if isinstance(milestone.get("details"), dict) else {}
    home_id = str(details.get("home_team_id") or "").strip()
    away_id = str(details.get("away_team_id") or "").strip()
    uuid_names = _kalshi_team_uuid_names(payload)
    home = uuid_names.get(home_id)
    away = uuid_names.get(away_id)
    if not home or not away:
        title = str(payload.get("title") or milestone.get("title") or "")
        # Kalshi NCAAB titles are "Away at Home" (census: Michigan at Arizona).
        away_label, home_label = _split_away_at_home(title)
        home = home or home_label
        away = away or away_label
    return _required_ncaab_team(home), _required_ncaab_team(away), _kickoff(start)


def _kalshi_team_uuid_names(payload: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for market in payload.get("markets") or []:
        if not isinstance(market, dict):
            continue
        strike = market.get("custom_strike")
        if not isinstance(strike, dict):
            continue
        team_id = str(strike.get("basketball_team") or strike.get("football_team") or "").strip()
        raw = str(_first(market, "yes_sub_title", "title") or "").strip()
        cleaned = re.split(r"\s+wins\b", raw, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if team_id and cleaned:
            resolved = resolve_ncaab_team(cleaned)
            if resolved.ok and resolved.canonical:
                mapping[team_id] = resolved.canonical
    return mapping


def _assemble_kalshi_game_winner(
    event: CanonicalEvent, payloads: list[dict[str, Any]]
) -> CanonicalMarket:
    runners: list[CanonicalRunner] = []
    for payload in payloads:
        ticker = str(_first(payload, "ticker", "market_ticker") or "").strip()
        title = str(_first(payload, "title", "yes_sub_title") or "")
        _reject_period_or_prop(title, series_head=ticker)
        outcome = _team_outcome(event, title.replace(" wins", ""))
        runners.append(
            CanonicalRunner(
                source_runner_id=f"{ticker}:YES",
                outcome=outcome,
                label=str(_first(payload, "yes_sub_title", "title") or outcome.value),
            )
        )
    outcomes = {runner.outcome for runner in runners}
    if outcomes != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
        raise VenueNormalizationError("Kalshi NCAAB GAME is not a two-team winner")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.KALSHI,
        source_market_id=f"{event.source_event_id}:game_winner",
        family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        line=None,
        settlement=ncaab_paper_settlement(family=MarketFamily.GAME_WINNER),
        runners=runners,
        confidence=1.0,
    )


def _kalshi_binary_market(
    event: CanonicalEvent,
    payload: dict[str, Any],
    *,
    series_head: str,
) -> CanonicalMarket:
    ticker = str(_first(payload, "ticker", "market_ticker") or "").strip()
    title = str(_first(payload, "title", "yes_sub_title") or "")
    _reject_period_or_prop(title, series_head=ticker)
    if series_head == "KXNCAAMBSPREAD":
        covering_line = parse_wins_by_over_line(title)
        if covering_line is None:
            raise VenueNormalizationError("Kalshi NCAAB spread title is not '{team} wins by over N.5'")
        covering_team = title.split(" wins by over")[0].strip()
        covering_outcome = _team_outcome(event, covering_team)
        home_line = covering_line if covering_outcome is CanonicalOutcome.HOME else -covering_line
        home_line = _require_half_line(home_line, what="spread")
        other = (
            CanonicalOutcome.AWAY if covering_outcome is CanonicalOutcome.HOME else CanonicalOutcome.HOME
        )
        runners = [
            CanonicalRunner(source_runner_id=f"{ticker}:YES", outcome=covering_outcome, label=title),
            CanonicalRunner(source_runner_id=f"{ticker}:NO", outcome=other, label=f"NO {title}"),
        ]
        return CanonicalMarket(
            event=event,
            source_venue=VenueName.KALSHI,
            source_market_id=ticker,
            family=MarketFamily.POINT_SPREAD,
            period=FootballPeriod.FULL_TIME,
            line=home_line,
            settlement=ncaab_paper_settlement(family=MarketFamily.POINT_SPREAD, line=home_line),
            runners=runners,
            confidence=1.0,
        )
    if series_head == "KXNCAAMBTOTAL":
        line = parse_over_points_line(title)
        line = _require_half_line(line, what="total")
        runners = [
            CanonicalRunner(source_runner_id=f"{ticker}:YES", outcome=CanonicalOutcome.OVER, label=title),
            CanonicalRunner(
                source_runner_id=f"{ticker}:NO",
                outcome=CanonicalOutcome.UNDER,
                label=f"NO {title}",
            ),
        ]
        return CanonicalMarket(
            event=event,
            source_venue=VenueName.KALSHI,
            source_market_id=ticker,
            family=MarketFamily.TOTAL_POINTS,
            period=FootballPeriod.FULL_TIME,
            line=line,
            settlement=ncaab_paper_settlement(family=MarketFamily.TOTAL_POINTS, line=line),
            runners=runners,
            confidence=1.0,
        )
    raise VenueNormalizationError(f"unsupported NCAAB Kalshi series: {series_head}")


def _polymarket_named_spread_line(
    event: CanonicalEvent, payload: dict[str, Any], question: str
) -> Decimal:
    raw = payload.get("line")
    try:
        named_line = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VenueNormalizationError("Polymarket NCAAB spread has no signed line") from exc
    named_line = _require_half_line(named_line, what="spread")
    named_team = question.split(":")[-1]
    named_team = named_team.replace("(", " ").replace(")", " ")
    words = [part for part in named_team.replace("-", " -").split() if not _is_number(part)]
    team_label = " ".join(words).strip() or question
    named_outcome = _team_outcome(event, team_label)
    if named_outcome is CanonicalOutcome.HOME:
        return named_line
    return -named_line


def _is_number(value: str) -> bool:
    try:
        Decimal(value)
        return True
    except InvalidOperation:
        return False


def _team_runners(
    event: CanonicalEvent,
    outcomes: list[str],
    token_ids: list[str],
    source_market_id: str,
) -> list[CanonicalRunner]:
    if len(outcomes) != 2:
        raise VenueNormalizationError("NCAAB game winner must have exactly two outcomes")
    runners: list[CanonicalRunner] = []
    for index, label in enumerate(outcomes):
        runners.append(
            CanonicalRunner(
                source_runner_id=_required_clob_token(token_ids, index, source_market_id),
                outcome=_team_outcome(event, label),
                label=str(label),
            )
        )
    return runners


def _spread_runners_from_named_outcomes(
    event: CanonicalEvent,
    outcomes: list[str],
    token_ids: list[str],
    source_market_id: str,
) -> list[CanonicalRunner]:
    if len(outcomes) != 2:
        raise VenueNormalizationError("NCAAB spread must have exactly two outcomes")
    runners: list[CanonicalRunner] = []
    for index, label in enumerate(outcomes):
        runners.append(
            CanonicalRunner(
                source_runner_id=_required_clob_token(token_ids, index, source_market_id),
                outcome=_team_outcome(event, label),
                label=str(label),
            )
        )
    if {runner.outcome for runner in runners} != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
        raise VenueNormalizationError("NCAAB spread outcomes are not both teams")
    return runners


def _over_under_runners(
    outcomes: list[str], token_ids: list[str], source_market_id: str
) -> list[CanonicalRunner]:
    runners: list[CanonicalRunner] = []
    for index, label in enumerate(outcomes):
        text = normalize_text(str(label))
        if text.startswith("over"):
            outcome = CanonicalOutcome.OVER
        elif text.startswith("under"):
            outcome = CanonicalOutcome.UNDER
        else:
            raise VenueNormalizationError(f"NCAAB total outcome is not Over/Under: {label}")
        runners.append(
            CanonicalRunner(
                source_runner_id=_required_clob_token(token_ids, index, source_market_id),
                outcome=outcome,
                label=str(label),
            )
        )
    if {runner.outcome for runner in runners} != {CanonicalOutcome.OVER, CanonicalOutcome.UNDER}:
        raise VenueNormalizationError("NCAAB total is not a complete Over/Under book")
    return runners


def _required_clob_token(token_ids: list[str], index: int, source_market_id: str) -> str:
    if index >= len(token_ids):
        raise VenueNormalizationError("Polymarket NCAAB market is missing exact CLOB token IDs")
    token = str(token_ids[index]).strip()
    if is_fabricated_polymarket_clob_token(token, market_id=source_market_id):
        raise VenueNormalizationError(
            "Polymarket NCAAB CLOB token IDs are not executable provider-native IDs"
        )
    return token


def _matchbook_spread(
    event: CanonicalEvent, runners_payload: list[dict[str, Any]]
) -> tuple[Decimal, list[CanonicalRunner]]:
    by_outcome: dict[CanonicalOutcome, Decimal] = {}
    runners: list[CanonicalRunner] = []
    for item in runners_payload:
        label = str(item.get("name") or "")
        parts = label.rsplit(" ", 1)
        team_label = parts[0] if len(parts) == 2 else label
        outcome = _team_outcome(event, team_label)
        raw = item.get("handicap")
        try:
            signed = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise VenueNormalizationError("Matchbook NCAAB handicap runner has no signed line") from exc
        signed = _require_half_line(signed, what="spread")
        by_outcome[outcome] = signed
        runners.append(
            CanonicalRunner(
                source_runner_id=str(item.get("id") or ""),
                outcome=outcome,
                label=label,
            )
        )
    if CanonicalOutcome.HOME not in by_outcome:
        raise VenueNormalizationError("Matchbook NCAAB handicap is missing the home runner")
    home_line = by_outcome[CanonicalOutcome.HOME]
    if CanonicalOutcome.AWAY in by_outcome and by_outcome[CanonicalOutcome.AWAY] != -home_line:
        raise VenueNormalizationError("Matchbook NCAAB handicap sides are not exact opposites")
    return home_line, runners


def _matchbook_total(
    runners_payload: list[dict[str, Any]],
) -> tuple[Decimal, list[CanonicalRunner]]:
    lines: set[Decimal] = set()
    runners: list[CanonicalRunner] = []
    for item in runners_payload:
        label = str(item.get("name") or "")
        raw = item.get("handicap")
        try:
            line = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise VenueNormalizationError("Matchbook NCAAB total runner has no line") from exc
        line = _require_half_line(line, what="total")
        lines.add(line)
        text = normalize_text(label)
        if text.startswith("over"):
            outcome = CanonicalOutcome.OVER
        elif text.startswith("under"):
            outcome = CanonicalOutcome.UNDER
        else:
            raise VenueNormalizationError(f"Matchbook NCAAB total runner is not Over/Under: {label}")
        runners.append(
            CanonicalRunner(
                source_runner_id=str(item.get("id") or ""),
                outcome=outcome,
                label=label,
            )
        )
    if len(lines) != 1:
        raise VenueNormalizationError("Matchbook NCAAB total runners do not share one half-point line")
    return next(iter(lines)), runners


def _team_outcome(event: CanonicalEvent, label: str) -> CanonicalOutcome:
    resolved = resolve_ncaab_team(label)
    if not resolved.ok or resolved.canonical is None:
        raise VenueNormalizationError(resolved.reason or f"unresolved NCAAB team: {label}")
    if resolved.canonical == event.home_team:
        return CanonicalOutcome.HOME
    if resolved.canonical == event.away_team:
        return CanonicalOutcome.AWAY
    raise VenueNormalizationError(f"NCAAB team {resolved.canonical} is not in this fixture")


def _split_away_at_home(title: str) -> tuple[str, str]:
    cleaned = _strip_family_suffix(title)
    lowered = f" {cleaned} "
    if " at " not in lowered.casefold():
        return _split_away_vs_home(cleaned)
    parts = [part.strip() for part in re.split(r"\s+at\s+", cleaned, maxsplit=1, flags=re.IGNORECASE)]
    parts = [part for part in parts if part]
    if len(parts) != 2:
        raise VenueNormalizationError(f"Cannot parse NCAAB Matchbook title: {title}")
    return parts[0], parts[1]


def _strip_family_suffix(title: str) -> str:
    return re.sub(
        r"\s*[:\-]\s*(spread|total points|totals?|moneyline|game winner).*$",
        "",
        title or "",
        flags=re.IGNORECASE,
    ).strip()


def _split_away_vs_home(title: str) -> tuple[str, str]:
    cleaned = _strip_family_suffix(title)
    parts = [
        part.strip(" -")
        for part in re.split(r"\s+(?:vs\.?|v)\s+", cleaned, flags=re.IGNORECASE)
        if part.strip(" -")
    ]
    if len(parts) != 2:
        raise VenueNormalizationError(f"Cannot parse NCAAB away vs home title: {title}")
    return parts[0], parts[1]


def operator_label_for(market: CanonicalMarket) -> str:
    return ncaab_operator_market_label(market)

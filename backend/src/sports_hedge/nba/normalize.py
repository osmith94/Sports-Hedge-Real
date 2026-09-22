"""Evidence-backed Kalshi / Polymarket / Matchbook NBA normalisation."""

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
from sports_hedge.nba.constants import NBA_COMPETITION, NBA_KALSHI_GAME_SERIES, NBA_SPORT
from sports_hedge.nba.detect import (
    approved_kalshi_nba_series,
    is_nba_kalshi_ticker,
    rejected_kalshi_nba_series,
)
from sports_hedge.nba.labels import nba_operator_market_label
from sports_hedge.nba.markets import (
    is_exact_half_line,
    nba_period_from_text,
    parse_over_points_line,
    parse_wins_by_over_line,
)
from sports_hedge.nba.settlement import nba_paper_settlement
from sports_hedge.nba.teams import require_resolved_nba_team, resolve_nba_team
from sports_hedge.normalization.text import normalize_text
from sports_hedge.normalization.venues import VenueNormalizationError, _first, _list_field, _parse_datetime


def _required_nba_team(value: str | None) -> str:
    try:
        return require_resolved_nba_team(value)
    except ValueError as exc:
        raise VenueNormalizationError(str(exc)) from exc

_NBA_PERIOD_MARKERS = (
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
_OUTRIGHT_MARKERS = (
    "championship",
    "outright",
    "futures",
    "winner 20",
    "series winner",
    "conference winner",
    "mvp",
    "summer league",
)


def _tipoff(value: Any) -> datetime:
    parsed = _parse_datetime(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _require_half_line(line: Decimal | None, *, what: str) -> Decimal:
    if line is None or not is_exact_half_line(line):
        raise VenueNormalizationError(f"NBA {what} requires an exact half-point line")
    return line


def _reject_period_or_prop(text: str, *, series_head: str | None = None) -> None:
    combined = normalize_text(text)
    if series_head and rejected_kalshi_nba_series(series_head):
        raise VenueNormalizationError(f"unsupported NBA Kalshi series: {series_head}")
    if any(token in combined for token in _OUTRIGHT_MARKERS):
        raise VenueNormalizationError("NBA outrights, series, and summer-league markets are unsupported")
    if any(token in combined for token in ("player", "prop", "first basket", "odd even", "odd/even", "exact margin")):
        raise VenueNormalizationError("NBA player props and derivatives are unsupported")
    if "team total" in combined:
        raise VenueNormalizationError("NBA team totals are unsupported")
    if nba_period_from_text(text) is None:
        raise VenueNormalizationError("NBA period / overtime-only markets are unsupported")
    for marker in _NBA_PERIOD_MARKERS:
        if marker in combined:
            raise VenueNormalizationError("NBA period / overtime-only markets are unsupported")


def kalshi_nba_event(
    payload: dict[str, Any],
    *,
    series: dict[str, Any] | None = None,
) -> CanonicalEvent:
    source_id = str(_first(payload, "event_ticker", "ticker", "id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Kalshi NBA event has no event_ticker")
    series_ticker = str(
        payload.get("series_ticker") or (series or {}).get("ticker") or source_id
    )
    if rejected_kalshi_nba_series(series_ticker) or rejected_kalshi_nba_series(source_id):
        raise VenueNormalizationError(f"unsupported NBA Kalshi series: {series_ticker}")
    if approved_kalshi_nba_series(series_ticker) is None and not is_nba_kalshi_ticker(source_id):
        raise VenueNormalizationError("Kalshi payload is not an approved NBA series")
    home, away, tipoff = _kalshi_home_away_tipoff(payload)
    return CanonicalEvent(
        sport=NBA_SPORT,
        competition=NBA_COMPETITION,
        home_team=home,
        away_team=away,
        kickoff_utc=tipoff,
        source_venue=VenueName.KALSHI,
        source_event_id=source_id,
        confidence=1.0,
    )


def kalshi_nba_markets(
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
    family_head = approved_kalshi_nba_series(series_ticker)
    if family_head is None:
        raise VenueNormalizationError(f"unsupported NBA Kalshi series: {series_ticker}")
    if family_head == NBA_KALSHI_GAME_SERIES:
        return [_assemble_kalshi_game_winner(event, payloads)]
    markets: list[CanonicalMarket] = []
    for payload in payloads:
        markets.append(_kalshi_binary_market(event, payload, series_head=family_head))
    return markets


def polymarket_nba_event(payload: dict[str, Any]) -> CanonicalEvent:
    source_id = str(_first(payload, "id", "event_id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Polymarket NBA event has no id")
    teams = payload.get("teams")
    if not isinstance(teams, list) or len(teams) < 2:
        raise VenueNormalizationError("Polymarket NBA event is missing teams[].ordering")
    by_order: dict[str, str] = {}
    for item in teams:
        if not isinstance(item, dict):
            continue
        ordering = normalize_text(str(item.get("ordering") or ""))
        name = str(
            item.get("name") or item.get("alias") or item.get("abbreviation") or ""
        ).strip()
        if ordering in {"home", "away"} and name:
            by_order[ordering] = _required_nba_team(name)
    if "home" not in by_order or "away" not in by_order:
        raise VenueNormalizationError("Polymarket NBA home/away ordering is incomplete")
    start = _first(payload, "startTime", "start_time")
    if not start:
        raise VenueNormalizationError("Polymarket NBA event has no startTime tipoff")
    return CanonicalEvent(
        sport=NBA_SPORT,
        competition=NBA_COMPETITION,
        home_team=by_order["home"],
        away_team=by_order["away"],
        kickoff_utc=_tipoff(start),
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
    """True for invented ``condition_id:0/1`` (or market-id) placeholders."""

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
    """Return exact CLOB token IDs or raise. Never invent ``condition_id:0/1``."""

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
            "Polymarket NBA market is missing exact CLOB token IDs"
        )
    if not tokens:
        raise VenueNormalizationError(
            "Polymarket NBA market is missing exact CLOB token IDs"
        )
    for token in tokens:
        if is_fabricated_polymarket_clob_token(
            token, condition_id=condition_id, market_id=source_market_id
        ):
            raise VenueNormalizationError(
                "Polymarket NBA CLOB token IDs are not executable provider-native IDs"
            )
    return tokens


def polymarket_nba_market(event: CanonicalEvent, payload: dict[str, Any]) -> CanonicalMarket:
    source_market_id = str(_first(payload, "id", "conditionId", "condition_id") or "").strip()
    if not source_market_id:
        raise VenueNormalizationError("Polymarket NBA market has no id")
    question = str(_first(payload, "question", "title", "groupItemTitle") or "")
    sports_type = normalize_text(str(_first(payload, "sportsMarketType", "sports_market_type") or ""))
    if sports_type not in {"moneyline", "spreads", "spread", "totals", "total"}:
        raise VenueNormalizationError(f"unsupported Polymarket NBA market type: {sports_type or question}")
    # Settlement copy may mention overtime inclusion; that is full-game wording,
    # not an overtime-only market. Period gates use title/type/slug only.
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
            family=family,
        )
    elif sports_type in {"spreads", "spread"}:
        family = MarketFamily.POINT_SPREAD
        named_line = _polymarket_named_spread_line(event, payload, question)
        line = named_line
        runners = _spread_runners_from_named_outcomes(
            event, outcomes, token_ids, source_market_id, home_line=line
        )
    elif sports_type in {"totals", "total"}:
        family = MarketFamily.TOTAL_POINTS
        raw_line = payload.get("line")
        try:
            line = _require_half_line(Decimal(str(raw_line)), what="total")
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise VenueNormalizationError("Polymarket NBA total has no exact half-point line") from exc
        runners = _over_under_runners(outcomes, token_ids, source_market_id)
    else:
        raise VenueNormalizationError(f"unsupported Polymarket NBA market type: {sports_type or question}")
    if len({runner.outcome for runner in runners}) != 2:
        raise VenueNormalizationError("Polymarket NBA market is not a two-team / two-side book")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.POLYMARKET,
        source_market_id=source_market_id,
        family=family,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=nba_paper_settlement(family=family, line=line),
        runners=runners,
        confidence=1.0,
    )


def matchbook_nba_event(payload: dict[str, Any]) -> CanonicalEvent:
    source_id = str(payload.get("id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Matchbook NBA event has no id")
    title = str(payload.get("name") or "").strip()
    _reject_period_or_prop(title)
    away, home = _split_away_at_home(title)
    start = _first(payload, "start", "start-time", "start_time")
    if not start:
        raise VenueNormalizationError("Matchbook NBA event has no start tipoff")
    return CanonicalEvent(
        sport=NBA_SPORT,
        competition=NBA_COMPETITION,
        home_team=_required_nba_team(home),
        away_team=_required_nba_team(away),
        kickoff_utc=_tipoff(start),
        source_venue=VenueName.MATCHBOOK,
        source_event_id=source_id,
        confidence=1.0,
    )


def matchbook_nba_market(event: CanonicalEvent, payload: dict[str, Any]) -> CanonicalMarket:
    source_market_id = str(payload.get("id") or "").strip()
    if not source_market_id:
        raise VenueNormalizationError("Matchbook NBA market has no id")
    name = str(payload.get("name") or "")
    market_type = normalize_text(str(payload.get("market-type") or payload.get("market_type") or ""))
    _reject_period_or_prop(f"{name} {market_type}")
    runners_payload = [item for item in (payload.get("runners") or []) if isinstance(item, dict)]
    if not runners_payload:
        raise VenueNormalizationError(f"Matchbook NBA market {source_market_id} has no runners")
    if market_type in {"money_line", "moneyline"} or normalize_text(name) == "moneyline":
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
        raise VenueNormalizationError(f"unsupported Matchbook NBA market: {name}")
    outcomes = {runner.outcome for runner in runners}
    if CanonicalOutcome.OTHER in outcomes:
        raise VenueNormalizationError("Matchbook NBA market has an unmapped runner")
    if family is MarketFamily.GAME_WINNER and outcomes != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
        raise VenueNormalizationError("Matchbook NBA moneyline is not a two-team winner")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.MATCHBOOK,
        source_market_id=source_market_id,
        family=family,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=nba_paper_settlement(family=family, line=line),
        runners=runners,
        confidence=1.0,
    )


def _kalshi_home_away_tipoff(payload: dict[str, Any]) -> tuple[str, str, datetime]:
    milestone = payload.get("milestone")
    if not isinstance(milestone, dict):
        raise VenueNormalizationError(
            "Kalshi NBA event has no milestone; occurrence_datetime is not tipoff"
        )
    start = milestone.get("start_date")
    if not start:
        raise VenueNormalizationError("Kalshi NBA milestone has no start_date tipoff")
    details = milestone.get("details") if isinstance(milestone.get("details"), dict) else {}
    home_id = str(details.get("home_team_id") or "").strip()
    away_id = str(details.get("away_team_id") or "").strip()
    uuid_names = _kalshi_team_uuid_names(payload)
    home = uuid_names.get(home_id)
    away = uuid_names.get(away_id)
    if not home or not away:
        subtitle = str(payload.get("sub_title") or milestone.get("title") or "")
        try:
            away_label, home_label = _split_away_at_home(subtitle) if " at " in f" {subtitle} ".casefold() else _split_away_vs_home(subtitle)
        except VenueNormalizationError:
            title = str(payload.get("title") or milestone.get("title") or "")
            away_label, home_label = _split_away_vs_home(title)
        home = home or home_label
        away = away or away_label
    return _required_nba_team(home), _required_nba_team(away), _tipoff(start)


def _kalshi_ticker_team_suffix(ticker: str) -> str | None:
    head_and_rest = str(ticker or "").strip().upper().split("-")
    if len(head_and_rest) < 2:
        return None
    suffix = head_and_rest[-1]
    resolved = resolve_nba_team(suffix)
    if resolved.ok:
        return resolved.canonical
    return None


def _kalshi_team_uuid_names(payload: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for market in payload.get("markets") or []:
        if not isinstance(market, dict):
            continue
        strike = market.get("custom_strike")
        if not isinstance(strike, dict):
            continue
        team_id = str(strike.get("basketball_team") or strike.get("football_team") or "").strip()
        ticker = str(_first(market, "ticker", "market_ticker") or "")
        canonical = _kalshi_ticker_team_suffix(ticker)
        if canonical is None:
            raw = str(_first(market, "yes_sub_title", "title") or "").strip()
            cleaned = re.split(r"\s+wins\b", raw, maxsplit=1, flags=re.IGNORECASE)[0].strip()
            resolved = resolve_nba_team(cleaned)
            if resolved.ok and resolved.canonical:
                canonical = resolved.canonical
        if team_id and canonical:
            mapping[team_id] = canonical
    return mapping


def _assemble_kalshi_game_winner(
    event: CanonicalEvent, payloads: list[dict[str, Any]]
) -> CanonicalMarket:
    runners: list[CanonicalRunner] = []
    for payload in payloads:
        ticker = str(_first(payload, "ticker", "market_ticker") or "").strip()
        title = str(_first(payload, "title", "yes_sub_title") or "")
        _reject_period_or_prop(title, series_head=ticker)
        label = title.replace(" wins", "")
        ticker_team = _kalshi_ticker_team_suffix(ticker)
        if ticker_team:
            outcome = _canonical_team_outcome(event, ticker_team)
        else:
            outcome = _team_outcome(event, label)
        runners.append(
            CanonicalRunner(
                source_runner_id=f"{ticker}:YES",
                outcome=outcome,
                label=str(_first(payload, "yes_sub_title", "title") or outcome.value),
            )
        )
    outcomes = {runner.outcome for runner in runners}
    if outcomes != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
        raise VenueNormalizationError("Kalshi NBA GAME is not a two-team winner")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.KALSHI,
        source_market_id=f"{event.source_event_id}:game_winner",
        family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        line=None,
        settlement=nba_paper_settlement(family=MarketFamily.GAME_WINNER),
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
    if series_head == "KXNBASPREAD":
        covering_line = parse_wins_by_over_line(title)
        if covering_line is None:
            raise VenueNormalizationError("Kalshi NBA spread title is not '{team} wins by over N.5'")
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
            settlement=nba_paper_settlement(family=MarketFamily.POINT_SPREAD, line=home_line),
            runners=runners,
            confidence=1.0,
        )
    if series_head == "KXNBATOTAL":
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
            settlement=nba_paper_settlement(family=MarketFamily.TOTAL_POINTS, line=line),
            runners=runners,
            confidence=1.0,
        )
    raise VenueNormalizationError(f"unsupported NBA Kalshi series: {series_head}")


def _polymarket_named_spread_line(
    event: CanonicalEvent, payload: dict[str, Any], question: str
) -> Decimal:
    raw = payload.get("line")
    try:
        named_line = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VenueNormalizationError("Polymarket NBA spread has no signed line") from exc
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
    *,
    family: MarketFamily,
) -> list[CanonicalRunner]:
    if len(outcomes) != 2:
        raise VenueNormalizationError("NBA game winner must have exactly two outcomes")
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
    *,
    home_line: Decimal,
) -> list[CanonicalRunner]:
    if len(outcomes) != 2:
        raise VenueNormalizationError("NBA spread must have exactly two outcomes")
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
        raise VenueNormalizationError("NBA spread outcomes are not both teams")
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
            raise VenueNormalizationError(f"NBA total outcome is not Over/Under: {label}")
        runners.append(
            CanonicalRunner(
                source_runner_id=_required_clob_token(token_ids, index, source_market_id),
                outcome=outcome,
                label=str(label),
            )
        )
    if {runner.outcome for runner in runners} != {CanonicalOutcome.OVER, CanonicalOutcome.UNDER}:
        raise VenueNormalizationError("NBA total is not a complete Over/Under book")
    return runners


def _required_clob_token(token_ids: list[str], index: int, source_market_id: str) -> str:
    if index >= len(token_ids):
        raise VenueNormalizationError("Polymarket NBA market is missing exact CLOB token IDs")
    token = str(token_ids[index]).strip()
    if is_fabricated_polymarket_clob_token(token, market_id=source_market_id):
        raise VenueNormalizationError(
            "Polymarket NBA CLOB token IDs are not executable provider-native IDs"
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
            raise VenueNormalizationError("Matchbook NBA handicap runner has no signed line") from exc
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
        raise VenueNormalizationError("Matchbook NBA handicap is missing the home runner")
    home_line = by_outcome[CanonicalOutcome.HOME]
    if CanonicalOutcome.AWAY in by_outcome and by_outcome[CanonicalOutcome.AWAY] != -home_line:
        raise VenueNormalizationError("Matchbook NBA handicap sides are not exact opposites")
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
            raise VenueNormalizationError("Matchbook NBA total runner has no line") from exc
        line = _require_half_line(line, what="total")
        lines.add(line)
        text = normalize_text(label)
        if text.startswith("over"):
            outcome = CanonicalOutcome.OVER
        elif text.startswith("under"):
            outcome = CanonicalOutcome.UNDER
        else:
            raise VenueNormalizationError(f"Matchbook NBA total runner is not Over/Under: {label}")
        runners.append(
            CanonicalRunner(
                source_runner_id=str(item.get("id") or ""),
                outcome=outcome,
                label=label,
            )
        )
    if len(lines) != 1:
        raise VenueNormalizationError("Matchbook NBA total runners do not share one half-point line")
    return next(iter(lines)), runners


def _canonical_team_outcome(event: CanonicalEvent, canonical: str) -> CanonicalOutcome:
    if canonical == event.home_team:
        return CanonicalOutcome.HOME
    if canonical == event.away_team:
        return CanonicalOutcome.AWAY
    raise VenueNormalizationError(f"NBA team {canonical} is not in this fixture")


def _team_outcome(event: CanonicalEvent, label: str) -> CanonicalOutcome:
    resolved = resolve_nba_team(label)
    if not resolved.ok or resolved.canonical is None:
        raise VenueNormalizationError(resolved.reason or f"unresolved NBA team: {label}")
    return _canonical_team_outcome(event, resolved.canonical)


def _split_away_at_home(title: str) -> tuple[str, str]:
    cleaned = _strip_nba_family_suffix(title)
    lowered = f" {cleaned} "
    if " at " not in lowered.casefold():
        return _split_away_vs_home(cleaned)
    parts = re_split_at(cleaned)
    if len(parts) != 2:
        raise VenueNormalizationError(f"Cannot parse NBA Matchbook title: {title}")
    return parts[0], parts[1]


def re_split_at(title: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\s+at\s+", title, maxsplit=1, flags=re.IGNORECASE)]
    return [part for part in parts if part]


def _strip_nba_family_suffix(title: str) -> str:
    return re.sub(
        r"\s*[:\-]\s*(spread|total points|totals?|moneyline|game winner).*$",
        "",
        title or "",
        flags=re.IGNORECASE,
    ).strip()


def _split_away_vs_home(title: str) -> tuple[str, str]:
    cleaned = _strip_nba_family_suffix(title)
    parts = [
        part.strip(" -")
        for part in re.split(r"\s+(?:vs\.?|v)\s+", cleaned, flags=re.IGNORECASE)
        if part.strip(" -")
    ]
    if len(parts) != 2:
        raise VenueNormalizationError(f"Cannot parse NBA away vs home title: {title}")
    return parts[0], parts[1]


def operator_label_for(market: CanonicalMarket) -> str:
    return nba_operator_market_label(market)

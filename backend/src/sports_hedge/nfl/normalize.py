"""Evidence-backed Kalshi / Polymarket / Matchbook NFL normalisation."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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
from sports_hedge.nfl.constants import NFL_COMPETITION, NFL_SPORT
from sports_hedge.nfl.detect import (
    approved_kalshi_nfl_series,
    is_nfl_kalshi_ticker,
    rejected_kalshi_nfl_series,
)
from sports_hedge.nfl.labels import nfl_operator_market_label
from sports_hedge.nfl.markets import (
    is_exact_half_line,
    nfl_period_from_text,
    parse_over_points_line,
    parse_wins_by_over_line,
)
from sports_hedge.nfl.settlement import nfl_paper_settlement
from sports_hedge.nfl.venue_mapping import (
    require_kalshi_nfl_mapping,
    require_matchbook_nfl_mapping,
    require_polymarket_nfl_mapping,
)
from sports_hedge.nfl.teams import (
    nfl_away_home_from_event_ticker,
    require_resolved_nfl_team,
    resolve_nfl_team,
)
from sports_hedge.normalization.text import normalize_text
from sports_hedge.normalization.venues import (
    VenueNormalizationError,
    _first,
    _list_field,
    _parse_datetime,
)


def _required_nfl_team(value: str | None) -> str:
    try:
        return require_resolved_nfl_team(value)
    except ValueError as exc:
        raise VenueNormalizationError(str(exc)) from exc

_NFL_PERIOD_MARKERS = (
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
        raise VenueNormalizationError(f"NFL {what} requires an exact half-point line")
    return line


def _reject_period_or_prop(text: str, *, series_head: str | None = None) -> None:
    combined = normalize_text(text)
    if series_head and rejected_kalshi_nfl_series(series_head):
        raise VenueNormalizationError(f"unsupported NFL Kalshi series: {series_head}")
    if any(token in combined for token in ("player", "prop", "touchdown", "first td", "exact margin")):
        raise VenueNormalizationError("NFL player props and derivatives are unsupported")
    if "team total" in combined:
        raise VenueNormalizationError("NFL team totals are unsupported")
    if nfl_period_from_text(text) is None:
        raise VenueNormalizationError("NFL period / overtime-only markets are unsupported")
    for marker in _NFL_PERIOD_MARKERS:
        if marker in combined:
            raise VenueNormalizationError("NFL period / overtime-only markets are unsupported")


def kalshi_nfl_event(
    payload: dict[str, Any],
    *,
    series: dict[str, Any] | None = None,
) -> CanonicalEvent:
    source_id = str(_first(payload, "event_ticker", "ticker", "id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Kalshi NFL event has no event_ticker")
    series_ticker = str(
        payload.get("series_ticker") or (series or {}).get("ticker") or source_id
    )
    if rejected_kalshi_nfl_series(series_ticker) or rejected_kalshi_nfl_series(source_id):
        raise VenueNormalizationError(f"unsupported NFL Kalshi series: {series_ticker}")
    if approved_kalshi_nfl_series(series_ticker) is None and not is_nfl_kalshi_ticker(source_id):
        raise VenueNormalizationError("Kalshi payload is not an approved NFL series")
    home, away, kickoff = _kalshi_home_away_kickoff(payload)
    return CanonicalEvent(
        sport=NFL_SPORT,
        competition=NFL_COMPETITION,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=VenueName.KALSHI,
        source_event_id=source_id,
        confidence=1.0,
    )


def kalshi_nfl_markets(
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
    mapping = require_kalshi_nfl_mapping(
        event_payload or series or {},
        series_ticker=series_ticker,
    )
    family_head = approved_kalshi_nfl_series(series_ticker)
    if family_head is None or mapping.family is None:
        raise VenueNormalizationError(f"unsupported NFL Kalshi series: {series_ticker}")
    if family_head == "KXNFLGAME":
        return [_assemble_kalshi_game_winner(event, payloads)]
    markets: list[CanonicalMarket] = []
    for payload in payloads:
        markets.append(_kalshi_binary_market(event, payload, series_head=family_head))
    return markets


def polymarket_nfl_event(payload: dict[str, Any]) -> CanonicalEvent:
    source_id = str(_first(payload, "id", "event_id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Polymarket NFL event has no id")
    teams = payload.get("teams")
    if not isinstance(teams, list) or len(teams) < 2:
        raise VenueNormalizationError("Polymarket NFL event is missing teams[].ordering")
    by_order: dict[str, str] = {}
    for item in teams:
        if not isinstance(item, dict):
            continue
        ordering = normalize_text(str(item.get("ordering") or ""))
        name = str(item.get("name") or item.get("alias") or "").strip()
        if ordering in {"home", "away"} and name:
            by_order[ordering] = _required_nfl_team(name)
    if "home" not in by_order or "away" not in by_order:
        raise VenueNormalizationError("Polymarket NFL home/away ordering is incomplete")
    start = _first(payload, "startTime", "start_time")
    if not start:
        raise VenueNormalizationError("Polymarket NFL event has no startTime kickoff")
    return CanonicalEvent(
        sport=NFL_SPORT,
        competition=NFL_COMPETITION,
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
    """True for invented ``condition_id:0/1`` (or market-id) placeholders.

    Real CLOB token IDs are provider-native strings, typically long decimals.
    Missing or synthetic tokens must never become executable/durable PAPER identity.
    """

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
            "Polymarket NFL market is missing exact CLOB token IDs"
        )
    if not tokens:
        raise VenueNormalizationError(
            "Polymarket NFL market is missing exact CLOB token IDs"
        )
    for token in tokens:
        if is_fabricated_polymarket_clob_token(
            token, condition_id=condition_id, market_id=source_market_id
        ):
            raise VenueNormalizationError(
                "Polymarket NFL CLOB token IDs are not executable provider-native IDs"
            )
    return tokens


def polymarket_nfl_market(event: CanonicalEvent, payload: dict[str, Any]) -> CanonicalMarket:
    source_market_id = str(_first(payload, "id", "conditionId", "condition_id") or "").strip()
    if not source_market_id:
        raise VenueNormalizationError("Polymarket NFL market has no id")
    question = str(_first(payload, "question", "title", "groupItemTitle") or "")
    mapping = require_polymarket_nfl_mapping(payload)
    sports_type = normalize_text(str(_first(payload, "sportsMarketType", "sports_market_type") or ""))
    _reject_period_or_prop(f"{sports_type} {question} {payload.get('slug') or ''}")
    outcomes = [str(item) for item in _list_field(payload.get("outcomes"))]
    token_ids = exact_polymarket_clob_token_ids(
        payload, market_id=source_market_id, required=len(outcomes) if outcomes else 2
    )
    if mapping.family is MarketFamily.GAME_WINNER:
        family = MarketFamily.GAME_WINNER
        line = None
        runners = _team_runners(
            event,
            outcomes,
            token_ids,
            source_market_id,
            family=family,
        )
    elif mapping.family is MarketFamily.POINT_SPREAD:
        family = MarketFamily.POINT_SPREAD
        named_line = _polymarket_named_spread_line(event, payload, question)
        line = named_line
        runners = _spread_runners_from_named_outcomes(
            event, outcomes, token_ids, source_market_id, home_line=line
        )
    elif mapping.family is MarketFamily.TOTAL_POINTS:
        family = MarketFamily.TOTAL_POINTS
        raw_line = payload.get("line")
        try:
            line = _require_half_line(Decimal(str(raw_line)), what="total")
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise VenueNormalizationError("Polymarket NFL total has no exact half-point line") from exc
        runners = _over_under_runners(outcomes, token_ids, source_market_id)
    else:
        raise VenueNormalizationError(f"unsupported Polymarket NFL market type: {sports_type or question}")
    if len({runner.outcome for runner in runners}) != 2:
        raise VenueNormalizationError("Polymarket NFL market is not a two-team / two-side book")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.POLYMARKET,
        source_market_id=source_market_id,
        family=family,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=nfl_paper_settlement(
            family=family, line=line, mapping_version=mapping.mapping_version
        ),
        runners=runners,
        confidence=1.0,
    )


def matchbook_nfl_event(payload: dict[str, Any]) -> CanonicalEvent:
    source_id = str(payload.get("id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Matchbook NFL event has no id")
    title = str(payload.get("name") or "").strip()
    away, home = _split_away_at_home(title)
    start = _first(payload, "start", "start-time", "start_time")
    if not start:
        raise VenueNormalizationError("Matchbook NFL event has no start kickoff")
    return CanonicalEvent(
        sport=NFL_SPORT,
        competition=NFL_COMPETITION,
        home_team=_required_nfl_team(home),
        away_team=_required_nfl_team(away),
        kickoff_utc=_kickoff(start),
        source_venue=VenueName.MATCHBOOK,
        source_event_id=source_id,
        confidence=1.0,
    )


def matchbook_nfl_market(event: CanonicalEvent, payload: dict[str, Any]) -> CanonicalMarket:
    source_market_id = str(payload.get("id") or "").strip()
    if not source_market_id:
        raise VenueNormalizationError("Matchbook NFL market has no id")
    mapping = require_matchbook_nfl_mapping(payload)
    name = str(payload.get("name") or "")
    _reject_period_or_prop(f"{name} {mapping.native_archetype}")
    runners_payload = [item for item in (payload.get("runners") or []) if isinstance(item, dict)]
    if not runners_payload:
        raise VenueNormalizationError(f"Matchbook NFL market {source_market_id} has no runners")
    if mapping.family is MarketFamily.GAME_WINNER:
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
    elif mapping.family is MarketFamily.POINT_SPREAD:
        family = MarketFamily.POINT_SPREAD
        line, runners = _matchbook_spread(event, runners_payload)
    elif mapping.family is MarketFamily.TOTAL_POINTS:
        family = MarketFamily.TOTAL_POINTS
        line, runners = _matchbook_total(runners_payload)
    else:
        raise VenueNormalizationError(f"unsupported Matchbook NFL market: {name}")
    outcomes = {runner.outcome for runner in runners}
    if CanonicalOutcome.OTHER in outcomes:
        raise VenueNormalizationError("Matchbook NFL market has an unmapped runner")
    if family is MarketFamily.GAME_WINNER and outcomes != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
        raise VenueNormalizationError("Matchbook NFL moneyline is not a two-team winner")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.MATCHBOOK,
        source_market_id=source_market_id,
        family=family,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=nfl_paper_settlement(
            family=family, line=line, mapping_version=mapping.mapping_version
        ),
        runners=runners,
        confidence=1.0,
    )


def _resolved_detail_team(details: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        resolved = resolve_nfl_team(str(details.get(key) or ""))
        if resolved.ok and resolved.canonical:
            return resolved.canonical
    return None


def _kalshi_home_away_kickoff(payload: dict[str, Any]) -> tuple[str, str, datetime]:
    """Resolve home/away without lowering event-match confidence.

    Evidence order: milestone team ids joined to market strikes, structured
    milestone names, title labels (including ``KC Chiefs``), then the event
    ticker's away+home abbreviations. A ticker never overrides a resolved side.
    """

    milestone = payload.get("milestone")
    if not isinstance(milestone, dict):
        raise VenueNormalizationError(
            "Kalshi NFL event has no milestone; occurrence_datetime is not kickoff"
        )
    start = milestone.get("start_date")
    if not start:
        raise VenueNormalizationError("Kalshi NFL milestone has no start_date kickoff")
    details = milestone.get("details") if isinstance(milestone.get("details"), dict) else {}
    home_id = str(details.get("home_team_id") or "").strip()
    away_id = str(details.get("away_team_id") or "").strip()
    uuid_names = _kalshi_team_uuid_names(payload)
    home = uuid_names.get(home_id) if home_id else None
    away = uuid_names.get(away_id) if away_id else None
    if not home:
        home = _resolved_detail_team(details, "home_team_name", "home_team", "home_team_abbr")
    if not away:
        away = _resolved_detail_team(details, "away_team_name", "away_team", "away_team_abbr")
    if not home or not away:
        title = str(payload.get("title") or milestone.get("title") or "")
        try:
            away_label, home_label = _split_away_at_home(title)
        except VenueNormalizationError:
            away_label, home_label = "", ""
        if not away and away_label:
            resolved = resolve_nfl_team(away_label)
            if resolved.ok and resolved.canonical:
                away = resolved.canonical
        if not home and home_label:
            resolved = resolve_nfl_team(home_label)
            if resolved.ok and resolved.canonical:
                home = resolved.canonical
    ticker = str(_first(payload, "event_ticker", "ticker") or "")
    ticker_pair = nfl_away_home_from_event_ticker(ticker)
    if ticker_pair is not None:
        ticker_away, ticker_home = ticker_pair
        if (away and away != ticker_away) or (home and home != ticker_home):
            ticker_pair = None
        else:
            away = away or ticker_away
            home = home or ticker_home
    return _required_nfl_team(home), _required_nfl_team(away), _kickoff(start)


def _kalshi_team_uuid_names(payload: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for market in payload.get("markets") or []:
        if not isinstance(market, dict):
            continue
        strike = market.get("custom_strike")
        if not isinstance(strike, dict):
            continue
        team_id = str(strike.get("football_team") or "").strip()
        raw = str(_first(market, "yes_sub_title", "title") or "").strip()
        cleaned = re.split(r"\s+wins\b", raw, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        if team_id and cleaned:
            resolved = resolve_nfl_team(cleaned)
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
        raise VenueNormalizationError("Kalshi NFL GAME is not a two-team winner")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.KALSHI,
        source_market_id=f"{event.source_event_id}:game_winner",
        family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        line=None,
        settlement=nfl_paper_settlement(family=MarketFamily.GAME_WINNER),
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
    if series_head == "KXNFLSPREAD":
        covering_line = parse_wins_by_over_line(title)
        if covering_line is None:
            raise VenueNormalizationError("Kalshi NFL spread title is not '{team} wins by over N.5'")
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
            settlement=nfl_paper_settlement(family=MarketFamily.POINT_SPREAD, line=home_line),
            runners=runners,
            confidence=1.0,
        )
    if series_head == "KXNFLTOTAL":
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
            settlement=nfl_paper_settlement(family=MarketFamily.TOTAL_POINTS, line=line),
            runners=runners,
            confidence=1.0,
        )
    raise VenueNormalizationError(f"unsupported NFL Kalshi series: {series_head}")


def _polymarket_named_spread_line(
    event: CanonicalEvent, payload: dict[str, Any], question: str
) -> Decimal:
    raw = payload.get("line")
    try:
        named_line = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise VenueNormalizationError("Polymarket NFL spread has no signed line") from exc
    named_line = _require_half_line(named_line, what="spread")
    named_team = question.split(":")[-1]
    named_team = named_team.replace("(", " ").replace(")", " ")
    # Drop the numeric line tokens before resolving the team.
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
        raise VenueNormalizationError("NFL game winner must have exactly two outcomes")
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
        raise VenueNormalizationError("NFL spread must have exactly two outcomes")
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
        raise VenueNormalizationError("NFL spread outcomes are not both teams")
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
            raise VenueNormalizationError(f"NFL total outcome is not Over/Under: {label}")
        runners.append(
            CanonicalRunner(
                source_runner_id=_required_clob_token(token_ids, index, source_market_id),
                outcome=outcome,
                label=str(label),
            )
        )
    if {runner.outcome for runner in runners} != {CanonicalOutcome.OVER, CanonicalOutcome.UNDER}:
        raise VenueNormalizationError("NFL total is not a complete Over/Under book")
    return runners


def _required_clob_token(token_ids: list[str], index: int, source_market_id: str) -> str:
    if index >= len(token_ids):
        raise VenueNormalizationError("Polymarket NFL market is missing exact CLOB token IDs")
    token = str(token_ids[index]).strip()
    if is_fabricated_polymarket_clob_token(token, market_id=source_market_id):
        raise VenueNormalizationError(
            "Polymarket NFL CLOB token IDs are not executable provider-native IDs"
        )
    return token


def _matchbook_spread(
    event: CanonicalEvent, runners_payload: list[dict[str, Any]]
) -> tuple[Decimal, list[CanonicalRunner]]:
    by_outcome: dict[CanonicalOutcome, Decimal] = {}
    runners: list[CanonicalRunner] = []
    for item in runners_payload:
        label = str(item.get("name") or "")
        team_label = label
        for sep in ("+", "-"):
            if sep in label[1:]:
                team_label = label[: label.rfind(sep if label.count(sep) == 1 else sep)].strip()
                break
        # "Kansas City Chiefs -4.5"
        parts = label.rsplit(" ", 1)
        team_label = parts[0] if len(parts) == 2 else label
        outcome = _team_outcome(event, team_label)
        raw = item.get("handicap")
        try:
            signed = Decimal(str(raw))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise VenueNormalizationError("Matchbook NFL handicap runner has no signed line") from exc
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
        raise VenueNormalizationError("Matchbook NFL handicap is missing the home runner")
    home_line = by_outcome[CanonicalOutcome.HOME]
    if CanonicalOutcome.AWAY in by_outcome and by_outcome[CanonicalOutcome.AWAY] != -home_line:
        raise VenueNormalizationError("Matchbook NFL handicap sides are not exact opposites")
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
            raise VenueNormalizationError("Matchbook NFL total runner has no line") from exc
        line = _require_half_line(line, what="total")
        lines.add(line)
        text = normalize_text(label)
        if text.startswith("over"):
            outcome = CanonicalOutcome.OVER
        elif text.startswith("under"):
            outcome = CanonicalOutcome.UNDER
        else:
            raise VenueNormalizationError(f"Matchbook NFL total runner is not Over/Under: {label}")
        runners.append(
            CanonicalRunner(
                source_runner_id=str(item.get("id") or ""),
                outcome=outcome,
                label=label,
            )
        )
    if len(lines) != 1:
        raise VenueNormalizationError("Matchbook NFL total runners do not share one half-point line")
    return next(iter(lines)), runners


def _team_outcome(event: CanonicalEvent, label: str) -> CanonicalOutcome:
    resolved = resolve_nfl_team(label)
    if not resolved.ok or resolved.canonical is None:
        raise VenueNormalizationError(resolved.reason or f"unresolved NFL team: {label}")
    if resolved.canonical == event.home_team:
        return CanonicalOutcome.HOME
    if resolved.canonical == event.away_team:
        return CanonicalOutcome.AWAY
    raise VenueNormalizationError(f"NFL team {resolved.canonical} is not in this fixture")


def _split_away_at_home(title: str) -> tuple[str, str]:
    cleaned = _strip_nfl_family_suffix(title)
    lowered = f" {cleaned} "
    if " at " not in lowered.casefold():
        return _split_away_vs_home(cleaned)
    parts = re_split_at(cleaned)
    if len(parts) != 2:
        raise VenueNormalizationError(f"Cannot parse NFL Matchbook title: {title}")
    return parts[0], parts[1]


def re_split_at(title: str) -> list[str]:
    parts = [part.strip() for part in re.split(r"\s+at\s+", title, maxsplit=1, flags=re.IGNORECASE)]
    return [part for part in parts if part]


def _strip_nfl_family_suffix(title: str) -> str:
    return re.sub(
        r"\s*[:\-]\s*(spread|total points|totals?|moneyline|game winner).*$",
        "",
        title or "",
        flags=re.IGNORECASE,
    ).strip()


def _split_away_vs_home(title: str) -> tuple[str, str]:
    cleaned = _strip_nfl_family_suffix(title)
    parts = [
        part.strip(" -")
        for part in re.split(r"\s+(?:vs\.?|v)\s+", cleaned, flags=re.IGNORECASE)
        if part.strip(" -")
    ]
    if len(parts) != 2:
        raise VenueNormalizationError(f"Cannot parse NFL away vs home title: {title}")
    return parts[0], parts[1]


def operator_label_for(market: CanonicalMarket) -> str:
    return nfl_operator_market_label(market)

"""Kalshi / Polymarket / Matchbook MLB normalisation for Stage-1 families only."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from decimal import Decimal
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
from sports_hedge.mlb.constants import MLB_COMPETITION, MLB_SPORT
from sports_hedge.mlb.detect import (
    approved_kalshi_mlb_series,
    rejected_kalshi_mlb_series,
)
from sports_hedge.mlb.markets import (
    is_exact_half_line,
    mlb_text_is_rejected_family,
    parse_game_number,
    parse_over_runs_line,
)
from sports_hedge.mlb.settlement import mlb_structural_settlement
from sports_hedge.mlb.teams import (
    mlb_away_home_from_event_ticker,
    require_resolved_mlb_team,
    resolve_mlb_team,
)
from sports_hedge.normalization.text import is_money_line_label, normalize_text
from sports_hedge.normalization.venues import (
    VenueNormalizationError,
    _first,
    _list_field,
    _parse_datetime,
)

_AWAY_VS_HOME = re.compile(r"^(.+?)\s+vs\.?\s+(.+)$", re.IGNORECASE)
_AWAY_AT_HOME = re.compile(r"^(.+?)\s+at\s+(.+)$", re.IGNORECASE)


def _required_team(value: str | None) -> str:
    try:
        return require_resolved_mlb_team(value)
    except ValueError as exc:
        raise VenueNormalizationError(str(exc)) from exc


def _kickoff(value: Any) -> datetime:
    parsed = _parse_datetime(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def scheduled_game_key(kickoff: datetime, *labels: object) -> str:
    """Minute-precision start plus explicit Game 1/Game 2 when the provider says so.

    Same clubs and the same calendar date are not this key.
    """

    instant = kickoff.astimezone(UTC).replace(second=0, microsecond=0)
    number = parse_game_number(*labels)
    if number is None:
        return instant.isoformat()
    return f"{instant.isoformat()}|game-{number}"


def _reject_non_stage1(*values: object, series_head: str | None = None) -> None:
    if series_head and rejected_kalshi_mlb_series(series_head):
        raise VenueNormalizationError(f"unsupported MLB Kalshi series: {series_head}")
    text = " ".join(str(value or "") for value in values)
    if mlb_text_is_rejected_family(text):
        raise VenueNormalizationError("MLB market is outside Stage-1 game winner / total runs x.5")


def _event(
    *,
    home: str,
    away: str,
    kickoff: datetime,
    venue: VenueName,
    source_event_id: str,
    labels: tuple[object, ...] = (),
) -> CanonicalEvent:
    return CanonicalEvent(
        sport=MLB_SPORT,
        competition=MLB_COMPETITION,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_event_id,
        confidence=1.0,
        scheduled_game_key=scheduled_game_key(kickoff, *labels),
    )


def _resolved_detail_team(details: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        resolved = resolve_mlb_team(str(details.get(key) or ""))
        if resolved.ok and resolved.canonical:
            return resolved.canonical
    return None


def _split_away_home_title(title: str) -> tuple[str, str]:
    cleaned = re.sub(
        r"\s*[:\-]\s*(total runs|totals?|moneyline|game winner).*$",
        "",
        title or "",
        flags=re.IGNORECASE,
    ).strip()
    at_match = _AWAY_AT_HOME.match(cleaned)
    if at_match is not None:
        return at_match.group(1).strip(), at_match.group(2).strip()
    vs_match = _AWAY_VS_HOME.match(cleaned)
    if vs_match is None:
        raise VenueNormalizationError("MLB title is not 'Away vs Home' or 'Away at Home'")
    return vs_match.group(1).strip(), vs_match.group(2).strip()


def kalshi_mlb_event(
    payload: dict[str, Any],
    *,
    series: dict[str, Any] | None = None,
) -> CanonicalEvent:
    source_id = str(_first(payload, "event_ticker", "ticker", "id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Kalshi MLB event has no event_ticker")
    series_ticker = str(payload.get("series_ticker") or (series or {}).get("ticker") or source_id)
    if approved_kalshi_mlb_series(series_ticker) is None:
        raise VenueNormalizationError(f"unsupported MLB Kalshi series: {series_ticker}")
    milestone = payload.get("milestone")
    if not isinstance(milestone, dict):
        raise VenueNormalizationError("Kalshi MLB event has no milestone; occurrence_datetime is not first pitch")
    details = milestone.get("details") if isinstance(milestone.get("details"), dict) else {}
    league = normalize_text(str(details.get("league") or ""))
    if league != "mlb":
        raise VenueNormalizationError("Kalshi baseball milestone league is not MLB")
    start = milestone.get("start_date")
    if not start:
        raise VenueNormalizationError("Kalshi MLB milestone has no start_date")
    home_id = str(details.get("home_team_id") or "").strip()
    away_id = str(details.get("away_team_id") or "").strip()
    names = _kalshi_team_uuid_names(payload)
    home = names.get(home_id) if home_id else None
    away = names.get(away_id) if away_id else None
    if not home:
        home = _resolved_detail_team(details, "home_team_name", "home_team", "home_team_abbr")
    if not away:
        away = _resolved_detail_team(details, "away_team_name", "away_team", "away_team_abbr")
    title = str(payload.get("title") or milestone.get("title") or "")
    if not home or not away:
        try:
            away_label, home_label = _split_away_home_title(title)
        except VenueNormalizationError:
            away_label, home_label = "", ""
        if not away and away_label:
            resolved = resolve_mlb_team(away_label)
            if resolved.ok and resolved.canonical:
                away = resolved.canonical
        if not home and home_label:
            resolved = resolve_mlb_team(home_label)
            if resolved.ok and resolved.canonical:
                home = resolved.canonical
    ticker_pair = mlb_away_home_from_event_ticker(source_id)
    if ticker_pair is not None:
        ticker_away, ticker_home = ticker_pair
        if (away and away != ticker_away) or (home and home != ticker_home):
            ticker_pair = None
        else:
            away = away or ticker_away
            home = home or ticker_home
    if not home or not away:
        raise VenueNormalizationError("Kalshi MLB home/away teams are unresolved")
    _reject_non_stage1(title, series_head=series_ticker)
    kickoff = _kickoff(start)
    return _event(
        home=home,
        away=away,
        kickoff=kickoff,
        venue=VenueName.KALSHI,
        source_event_id=source_id,
        labels=(title, payload.get("sub_title"), source_id),
    )


def _kalshi_team_uuid_names(payload: dict[str, Any]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for market in payload.get("markets") or []:
        if not isinstance(market, dict):
            continue
        strike = market.get("custom_strike")
        if not isinstance(strike, dict):
            continue
        team_id = str(strike.get("baseball_team") or "").strip()
        raw = str(_first(market, "yes_sub_title", "title") or "").strip()
        cleaned = re.split(r"\s+wins\b", raw, maxsplit=1, flags=re.IGNORECASE)[0].strip()
        resolved = resolve_mlb_team(cleaned)
        if team_id and resolved.ok and resolved.canonical:
            mapping[team_id] = resolved.canonical
    return mapping


def kalshi_mlb_markets(
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
    head = approved_kalshi_mlb_series(series_ticker)
    if head is None:
        raise VenueNormalizationError(f"unsupported MLB Kalshi series: {series_ticker}")
    if head == "KXMLBGAME":
        return [_assemble_kalshi_game_winner(event, payloads)]
    markets: list[CanonicalMarket] = []
    for payload in payloads:
        markets.append(_kalshi_total(event, payload))
    return markets


def _team_outcome(event: CanonicalEvent, label: str) -> CanonicalOutcome:
    team = _required_team(label)
    if team == event.home_team:
        return CanonicalOutcome.HOME
    if team == event.away_team:
        return CanonicalOutcome.AWAY
    raise VenueNormalizationError("MLB runner is not one of the fixture clubs")


def _assemble_kalshi_game_winner(
    event: CanonicalEvent, payloads: list[dict[str, Any]]
) -> CanonicalMarket:
    runners: list[CanonicalRunner] = []
    for payload in payloads:
        ticker = str(_first(payload, "ticker", "market_ticker") or "").strip()
        title = str(_first(payload, "title", "yes_sub_title") or "")
        _reject_non_stage1(title, series_head=ticker)
        if "tie" in normalize_text(title):
            raise VenueNormalizationError("Kalshi MLB Tie strike is not a game-winner side")
        outcome = _team_outcome(event, title.replace(" wins", ""))
        runners.append(
            CanonicalRunner(
                source_runner_id=f"{ticker}:YES",
                outcome=outcome,
                label=str(_first(payload, "yes_sub_title", "title") or outcome.value),
            )
        )
    if {runner.outcome for runner in runners} != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
        raise VenueNormalizationError("Kalshi MLB GAME is not a two-club winner")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.KALSHI,
        source_market_id=f"{event.source_event_id}:game_winner",
        family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        line=None,
        settlement=mlb_structural_settlement(family=MarketFamily.GAME_WINNER),
        runners=runners,
        confidence=1.0,
    )


def _kalshi_total(event: CanonicalEvent, payload: dict[str, Any]) -> CanonicalMarket:
    ticker = str(_first(payload, "ticker", "market_ticker") or "").strip()
    title = str(_first(payload, "title", "yes_sub_title") or "")
    _reject_non_stage1(title, series_head=ticker)
    line = payload.get("floor_strike")
    parsed = Decimal(str(line)) if line is not None else parse_over_runs_line(title)
    if not is_exact_half_line(parsed):
        raise VenueNormalizationError("MLB total runs requires an exact x.5 line")
    assert parsed is not None
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.KALSHI,
        source_market_id=ticker,
        family=MarketFamily.TOTAL_RUNS,
        period=FootballPeriod.FULL_TIME,
        line=parsed,
        settlement=mlb_structural_settlement(family=MarketFamily.TOTAL_RUNS, line=parsed),
        runners=[
            CanonicalRunner(source_runner_id=f"{ticker}:YES", outcome=CanonicalOutcome.OVER, label=title),
            CanonicalRunner(
                source_runner_id=f"{ticker}:NO",
                outcome=CanonicalOutcome.UNDER,
                label=f"under {parsed}",
            ),
        ],
        confidence=1.0,
    )


def polymarket_mlb_event(payload: dict[str, Any]) -> CanonicalEvent:
    source_id = str(_first(payload, "id", "event_id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Polymarket MLB event has no id")
    slug = str(payload.get("slug") or "")
    title = str(payload.get("title") or "")
    _reject_non_stage1(slug, title)
    teams = payload.get("teams")
    if not isinstance(teams, list) or len(teams) < 2:
        raise VenueNormalizationError("Polymarket MLB event is missing teams[].ordering")
    by_order: dict[str, str] = {}
    for item in teams:
        if not isinstance(item, dict):
            continue
        ordering = normalize_text(str(item.get("ordering") or ""))
        league = normalize_text(str(item.get("league") or "mlb"))
        if league and league != "mlb":
            raise VenueNormalizationError("Polymarket team league is not MLB")
        name = str(item.get("name") or item.get("alias") or "").strip()
        if ordering in {"home", "away"} and name:
            by_order[ordering] = _required_team(name)
    if "home" not in by_order or "away" not in by_order:
        raise VenueNormalizationError("Polymarket MLB home/away ordering is incomplete")
    start = _first(payload, "startTime", "start_time")
    if not start:
        raise VenueNormalizationError("Polymarket MLB event has no startTime")
    kickoff = _kickoff(start)
    return _event(
        home=by_order["home"],
        away=by_order["away"],
        kickoff=kickoff,
        venue=VenueName.POLYMARKET,
        source_event_id=source_id,
        labels=(title, slug),
    )


def _polymarket_tokens(payload: dict[str, Any], *, market_id: str) -> list[str]:
    tokens = [
        str(item).strip()
        for item in _list_field(_first(payload, "clobTokenIds", "clob_token_ids"))
        if str(item).strip()
    ]
    condition = str(_first(payload, "conditionId", "condition_id") or "").strip()
    if len(tokens) < 2:
        raise VenueNormalizationError("Polymarket MLB market is missing exact CLOB token IDs")
    for token in tokens:
        if token in {f"{condition}:0", f"{condition}:1", f"{market_id}:0", f"{market_id}:1"}:
            raise VenueNormalizationError("Polymarket MLB CLOB token IDs are not provider-native")
        if ":" in token and token.rsplit(":", 1)[-1] in {"0", "1"}:
            raise VenueNormalizationError("Polymarket MLB CLOB token IDs are not provider-native")
    return tokens


def polymarket_mlb_market(event: CanonicalEvent, payload: dict[str, Any]) -> CanonicalMarket:
    source_market_id = str(_first(payload, "id", "conditionId") or "").strip()
    if not source_market_id:
        raise VenueNormalizationError("Polymarket MLB market has no id")
    question = str(_first(payload, "question", "title") or "")
    sports_type = normalize_text(str(_first(payload, "sportsMarketType", "sports_market_type") or ""))
    description = str(payload.get("description") or "")
    _reject_non_stage1(sports_type, question, payload.get("slug"), description)
    if sports_type not in {"moneyline", "totals", "total"}:
        raise VenueNormalizationError(f"unsupported Polymarket MLB market type: {sports_type or question}")
    outcomes = [str(item) for item in _list_field(payload.get("outcomes"))]
    tokens = _polymarket_tokens(payload, market_id=source_market_id)
    if len(tokens) != len(outcomes):
        raise VenueNormalizationError("Polymarket MLB outcomes and CLOB tokens disagree")
    if sports_type == "moneyline":
        runners = []
        for label, token in zip(outcomes, tokens, strict=True):
            runners.append(
                CanonicalRunner(
                    source_runner_id=token,
                    outcome=_team_outcome(event, label),
                    label=label,
                )
            )
        if {runner.outcome for runner in runners} != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
            raise VenueNormalizationError("Polymarket MLB moneyline is not a two-club winner")
        return CanonicalMarket(
            event=event,
            source_venue=VenueName.POLYMARKET,
            source_market_id=source_market_id,
            family=MarketFamily.GAME_WINNER,
            period=FootballPeriod.FULL_TIME,
            line=None,
            settlement=mlb_structural_settlement(family=MarketFamily.GAME_WINNER),
            runners=runners,
            confidence=1.0,
        )
    raw_line = payload.get("line")
    line = Decimal(str(raw_line)) if raw_line is not None else None
    if not is_exact_half_line(line):
        raise VenueNormalizationError("MLB total runs requires an exact x.5 line")
    assert line is not None
    by_name = {normalize_text(label): token for label, token in zip(outcomes, tokens, strict=True)}
    if "over" not in by_name or "under" not in by_name:
        raise VenueNormalizationError("Polymarket MLB total is missing Over/Under")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.POLYMARKET,
        source_market_id=source_market_id,
        family=MarketFamily.TOTAL_RUNS,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=mlb_structural_settlement(family=MarketFamily.TOTAL_RUNS, line=line),
        runners=[
            CanonicalRunner(source_runner_id=by_name["over"], outcome=CanonicalOutcome.OVER, label="Over"),
            CanonicalRunner(source_runner_id=by_name["under"], outcome=CanonicalOutcome.UNDER, label="Under"),
        ],
        confidence=1.0,
    )


def matchbook_mlb_event(payload: dict[str, Any]) -> CanonicalEvent:
    source_id = str(_first(payload, "id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Matchbook MLB event has no id")
    name = str(payload.get("name") or "").strip()
    _reject_non_stage1(name)
    match = _AWAY_AT_HOME.match(name)
    if match is None:
        raise VenueNormalizationError("Matchbook MLB event name is not 'Away at Home'")
    away = _required_team(match.group(1))
    home = _required_team(match.group(2))
    start = _first(payload, "start", "start-time", "start_time")
    if not start:
        raise VenueNormalizationError("Matchbook MLB event has no start")
    kickoff = _kickoff(start)
    return _event(
        home=home,
        away=away,
        kickoff=kickoff,
        venue=VenueName.MATCHBOOK,
        source_event_id=source_id,
        labels=(name,),
    )


def matchbook_mlb_market(event: CanonicalEvent, payload: dict[str, Any]) -> CanonicalMarket:
    source_market_id = str(_first(payload, "id") or "").strip()
    name = str(payload.get("name") or "")
    market_type = normalize_text(str(payload.get("market-type") or payload.get("market_type") or ""))
    _reject_non_stage1(name, market_type)
    runners_raw = [item for item in payload.get("runners") or [] if isinstance(item, dict)]
    if is_money_line_label(market_type) or is_money_line_label(name):
        runners = []
        for runner in runners_raw:
            label = str(runner.get("name") or "")
            runners.append(
                CanonicalRunner(
                    source_runner_id=str(runner.get("id") or ""),
                    outcome=_team_outcome(event, label),
                    label=label,
                )
            )
        if {runner.outcome for runner in runners} != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
            raise VenueNormalizationError("Matchbook MLB moneyline is not a two-club winner")
        return CanonicalMarket(
            event=event,
            source_venue=VenueName.MATCHBOOK,
            source_market_id=source_market_id,
            family=MarketFamily.GAME_WINNER,
            period=FootballPeriod.FULL_TIME,
            line=None,
            settlement=mlb_structural_settlement(family=MarketFamily.GAME_WINNER),
            runners=runners,
            confidence=1.0,
        )
    if market_type == "total" or normalize_text(name) == "total":
        raw = payload.get("handicap")
        line = Decimal(str(raw)) if raw is not None else None
        if not is_exact_half_line(line):
            raise VenueNormalizationError("MLB total runs requires an exact x.5 line")
        assert line is not None
        mapped: dict[CanonicalOutcome, CanonicalRunner] = {}
        for runner in runners_raw:
            label = str(runner.get("name") or "")
            text = normalize_text(label)
            if text.startswith("over"):
                outcome = CanonicalOutcome.OVER
            elif text.startswith("under"):
                outcome = CanonicalOutcome.UNDER
            else:
                raise VenueNormalizationError("Matchbook MLB total runner is not OVER/UNDER")
            mapped[outcome] = CanonicalRunner(
                source_runner_id=str(runner.get("id") or ""),
                outcome=outcome,
                label=label,
            )
        if set(mapped) != {CanonicalOutcome.OVER, CanonicalOutcome.UNDER}:
            raise VenueNormalizationError("Matchbook MLB total is missing OVER/UNDER")
        return CanonicalMarket(
            event=event,
            source_venue=VenueName.MATCHBOOK,
            source_market_id=source_market_id,
            family=MarketFamily.TOTAL_RUNS,
            period=FootballPeriod.FULL_TIME,
            line=line,
            settlement=mlb_structural_settlement(family=MarketFamily.TOTAL_RUNS, line=line),
            runners=[mapped[CanonicalOutcome.OVER], mapped[CanonicalOutcome.UNDER]],
            confidence=1.0,
        )
    raise VenueNormalizationError(f"unsupported Matchbook MLB market: {name}")


def split_away_vs_home(title: str) -> tuple[str, str]:
    match = _AWAY_VS_HOME.match(title.strip())
    if match is None:
        raise VenueNormalizationError("MLB title is not 'Away vs Home'")
    return match.group(1).strip(), match.group(2).strip()

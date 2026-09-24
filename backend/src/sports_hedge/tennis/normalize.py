"""ATP/WTA singles Match Winner normalisation. Other families fail closed."""

from __future__ import annotations

import re
from datetime import UTC, datetime
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
from sports_hedge.normalization.text import normalize_text
from sports_hedge.normalization.venues import (
    VenueNormalizationError,
    _first,
    _list_field,
    _parse_datetime,
)
from sports_hedge.tennis.constants import (
    POLYMARKET_ATP_SERIES_ID,
    POLYMARKET_ATP_SPORT,
    POLYMARKET_WTA_SERIES_ID,
    POLYMARKET_WTA_SPORT,
    TENNIS_EVENT_DOUBLES,
    TENNIS_EVENT_SINGLES,
    TENNIS_KALSHI_APPROVED_SERIES,
    TENNIS_SPORT,
)
from sports_hedge.tennis.detect import (
    approved_kalshi_tennis_series,
    is_tennis_kalshi_ticker,
    rejected_kalshi_tennis_series,
)
from sports_hedge.tennis.players import orient_players, require_tennis_player
from sports_hedge.tennis.rounds import canonical_round_label
from sports_hedge.tennis.settlement import tennis_match_winner_settlement
from sports_hedge.tennis.tournaments import admitted_tournament

_SINGLES_SERIES = {
    POLYMARKET_ATP_SPORT: "ATP",
    POLYMARKET_WTA_SPORT: "WTA",
}
_SINGLES_SERIES_IDS = {
    POLYMARKET_ATP_SERIES_ID: "ATP",
    POLYMARKET_WTA_SERIES_ID: "WTA",
}


def _kickoff(value: Any) -> datetime:
    parsed = _parse_datetime(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _split_players(title: str) -> tuple[str, str]:
    text = title.strip()
    if ":" in text:
        text = text.split(":", 1)[1].strip()
    match = re.search(r"\s+vs\.?\s+|\s+v\s+", text, flags=re.IGNORECASE)
    if match is None:
        raise VenueNormalizationError("tennis fixture title is not a two-player match")
    return text[: match.start()].strip(), text[match.end() :].strip()


def _event_type(text: str, *, series_head: str | None = None) -> str:
    blob = normalize_text(f"{series_head or ''} {text}")
    if "doubles" in blob or "mixed" in blob or "/" in text:
        return TENNIS_EVENT_DOUBLES
    return TENNIS_EVENT_SINGLES


def _build_event(
    *,
    tour: str,
    tournament: str,
    round_label: str,
    event_type: str,
    left_name: str,
    right_name: str,
    kickoff: datetime,
    venue: VenueName,
    source_event_id: str,
) -> CanonicalEvent:
    if event_type != TENNIS_EVENT_SINGLES:
        raise VenueNormalizationError("tennis doubles, mixed and team events are outside stage 1")
    if not tournament:
        raise VenueNormalizationError("tennis tournament is not an admitted ATP/WTA singles event")
    if not round_label:
        raise VenueNormalizationError("tennis round is unavailable")
    try:
        left = require_tennis_player(left_name)
        right = require_tennis_player(right_name)
        home, away = orient_players(left, right)
    except ValueError as exc:
        raise VenueNormalizationError(str(exc)) from exc
    return CanonicalEvent(
        sport=TENNIS_SPORT,
        competition=tour,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_event_id,
        tournament=tournament,
        round_label=round_label,
        event_type=TENNIS_EVENT_SINGLES,
        confidence=1.0,
    )


def _outcome_for_player(event: CanonicalEvent, label: str) -> CanonicalOutcome:
    try:
        canonical = require_tennis_player(label)
    except ValueError as exc:
        raise VenueNormalizationError(str(exc)) from exc
    if canonical == event.home_team:
        return CanonicalOutcome.HOME
    if canonical == event.away_team:
        return CanonicalOutcome.AWAY
    raise VenueNormalizationError("tennis runner is not one of the match players")


def _meta_value(payload: dict[str, Any], tag_type: str) -> list[str]:
    meta = payload.get("meta-tags") or payload.get("meta_tags") or []
    found: list[str] = []
    if not isinstance(meta, list):
        return found
    for tag in meta:
        if not isinstance(tag, dict):
            continue
        if normalize_text(str(tag.get("type") or "")) != tag_type:
            continue
        name = str(tag.get("name") or "").strip()
        if name:
            found.append(name)
    return found


def kalshi_tennis_event(
    payload: dict[str, Any],
    *,
    series: dict[str, Any] | None = None,
) -> CanonicalEvent:
    source_id = str(_first(payload, "event_ticker", "ticker", "id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Kalshi tennis event has no event_ticker")
    series_ticker = str(payload.get("series_ticker") or (series or {}).get("ticker") or source_id)
    if rejected_kalshi_tennis_series(series_ticker) or rejected_kalshi_tennis_series(source_id):
        raise VenueNormalizationError(f"unsupported tennis Kalshi series: {series_ticker}")
    head = approved_kalshi_tennis_series(series_ticker)
    if head not in TENNIS_KALSHI_APPROVED_SERIES:
        raise VenueNormalizationError("Kalshi payload is not an ATP/WTA singles match series")
    markets = payload.get("markets") if isinstance(payload.get("markets"), list) else []
    names: list[str] = []
    rule_parts: list[str] = []
    kickoff_raw = None
    for market in markets:
        if not isinstance(market, dict):
            continue
        label = str(market.get("yes_sub_title") or market.get("title") or "")
        label = label.replace(" wins", "").strip()
        if label:
            names.append(label)
        rule_parts.append(str(market.get("rules_primary") or ""))
        rule_parts.append(str(market.get("rules_secondary") or ""))
        if kickoff_raw is None:
            kickoff_raw = (
                market.get("occurrence_datetime")
                or market.get("expected_expiration_time")
                or market.get("open_time")
            )
    if len(names) < 2:
        raise VenueNormalizationError("Kalshi tennis match is missing two full player names")
    metadata = payload.get("product_metadata") if isinstance(payload.get("product_metadata"), dict) else {}
    competition = str(metadata.get("competition") or "")
    admitted = admitted_tournament(competition)
    if admitted is None:
        raise VenueNormalizationError("tennis tournament is not an admitted ATP/WTA singles event")
    tour, tournament = admitted
    expected = "ATP" if head == "KXATPMATCH" else "WTA"
    if tour != expected:
        raise VenueNormalizationError("tennis Kalshi series tour does not match the tournament")
    if kickoff_raw is None:
        raise VenueNormalizationError("Kalshi tennis match has no supporting start time")
    return _build_event(
        tour=tour,
        tournament=tournament,
        round_label=canonical_round_label(*rule_parts, payload.get("title"), payload.get("sub_title")),
        event_type=_event_type(" ".join(rule_parts), series_head=head),
        left_name=names[0],
        right_name=names[1],
        kickoff=_kickoff(kickoff_raw),
        venue=VenueName.KALSHI,
        source_event_id=source_id,
    )


def kalshi_tennis_markets(
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
    if approved_kalshi_tennis_series(series_ticker) is None:
        raise VenueNormalizationError(f"unsupported tennis Kalshi series: {series_ticker}")
    runners: list[CanonicalRunner] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        ticker = str(_first(payload, "ticker", "market_ticker") or "").strip()
        if rejected_kalshi_tennis_series(ticker):
            raise VenueNormalizationError(f"unsupported tennis Kalshi series: {ticker}")
        label = str(_first(payload, "yes_sub_title", "title") or "")
        player = label.replace(" wins", "").strip()
        runners.append(
            CanonicalRunner(
                source_runner_id=f"{ticker}:YES",
                outcome=_outcome_for_player(event, player),
                label=player,
            )
        )
    outcomes = {runner.outcome for runner in runners}
    if outcomes != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
        raise VenueNormalizationError("Kalshi tennis match is not a two-player match winner")
    return [
        CanonicalMarket(
            event=event,
            source_venue=VenueName.KALSHI,
            source_market_id=f"{event.source_event_id}:match_winner",
            family=MarketFamily.GAME_WINNER,
            period=FootballPeriod.FULL_TIME,
            line=None,
            settlement=tennis_match_winner_settlement(),
            runners=runners,
            confidence=1.0,
        )
    ]


def polymarket_tennis_event(payload: dict[str, Any]) -> CanonicalEvent:
    source_id = str(_first(payload, "id", "event_id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Polymarket tennis event has no id")
    tour = _polymarket_tour(payload)
    if tour is None:
        raise VenueNormalizationError("Polymarket tennis series is not ATP/WTA singles")
    title = str(_first(payload, "title", "question", "name") or "").strip()
    metadata = payload.get("eventMetadata") if isinstance(payload.get("eventMetadata"), dict) else {}
    league = str(metadata.get("league") or "")
    if not league and ":" in title:
        league = title.split(":", 1)[0]
    admitted = admitted_tournament(league)
    if admitted is None:
        raise VenueNormalizationError("tennis tournament is not an admitted ATP/WTA singles event")
    admitted_tour, tournament = admitted
    if admitted_tour != tour:
        raise VenueNormalizationError("Polymarket tennis tour does not match the tournament")
    if _event_type(title) != TENNIS_EVENT_SINGLES:
        raise VenueNormalizationError("tennis doubles, mixed and team events are outside stage 1")
    left_name, right_name = _split_players(title)
    start = _first(payload, "startTime", "start_time", "gameStartTime")
    if not start:
        raise VenueNormalizationError("Polymarket tennis event has no startTime")
    markets = payload.get("markets") if isinstance(payload.get("markets"), list) else []
    round_bits = [title, str(payload.get("description") or "")]
    for market in markets:
        if isinstance(market, dict):
            round_bits.append(str(market.get("question") or ""))
            round_bits.append(str(market.get("description") or ""))
            if normalize_text(str(market.get("sportsMarketType") or "")) == "moneyline":
                round_bits.append(str(market.get("description") or ""))
    return _build_event(
        tour=tour,
        tournament=tournament,
        round_label=canonical_round_label(*round_bits),
        event_type=TENNIS_EVENT_SINGLES,
        left_name=left_name,
        right_name=right_name,
        kickoff=_kickoff(start),
        venue=VenueName.POLYMARKET,
        source_event_id=source_id,
    )


def polymarket_tennis_market(event: CanonicalEvent, payload: dict[str, Any]) -> CanonicalMarket:
    sports_type = normalize_text(str(_first(payload, "sportsMarketType", "sports_market_type") or ""))
    if sports_type != "moneyline":
        raise VenueNormalizationError(f"unsupported tennis market family: {sports_type or 'unknown'}")
    source_market_id = str(_first(payload, "id", "conditionId", "condition_id") or "").strip()
    if not source_market_id:
        raise VenueNormalizationError("Polymarket tennis market has no id")
    outcomes = [str(item) for item in _list_field(payload.get("outcomes"))]
    token_ids = [
        str(item).strip()
        for item in _list_field(_first(payload, "clobTokenIds", "clob_token_ids"))
        if str(item).strip()
    ]
    if len(outcomes) != 2 or len(token_ids) != 2:
        raise VenueNormalizationError("Polymarket tennis match winner needs two named outcomes")
    runners = [
        CanonicalRunner(
            source_runner_id=token_ids[index],
            outcome=_outcome_for_player(event, outcomes[index]),
            label=outcomes[index],
        )
        for index in range(2)
    ]
    if {runner.outcome for runner in runners} != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
        raise VenueNormalizationError("Polymarket tennis match winner outcomes are incomplete")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.POLYMARKET,
        source_market_id=source_market_id,
        family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        line=None,
        settlement=tennis_match_winner_settlement(),
        runners=runners,
        confidence=1.0,
    )


def polymarket_tennis_markets(event: CanonicalEvent, payloads: list[dict[str, Any]]) -> list[CanonicalMarket]:
    markets: list[CanonicalMarket] = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        sports_type = normalize_text(str(payload.get("sportsMarketType") or ""))
        if sports_type != "moneyline":
            continue
        markets.append(polymarket_tennis_market(event, payload))
    if not markets:
        raise VenueNormalizationError("Polymarket tennis event has no match winner")
    return markets


def matchbook_tennis_event(payload: dict[str, Any]) -> CanonicalEvent:
    source_id = str(payload.get("id") or "").strip()
    if not source_id:
        raise VenueNormalizationError("Matchbook tennis event has no id")
    title = str(payload.get("name") or "").strip()
    competitions = _meta_value(payload, "competition")
    competition = competitions[0] if competitions else ""
    admitted = admitted_tournament(competition)
    if admitted is None:
        raise VenueNormalizationError("tennis tournament is not an admitted ATP/WTA singles event")
    tour, tournament = admitted
    if _event_type(f"{title} {competition}") != TENNIS_EVENT_SINGLES:
        raise VenueNormalizationError("tennis doubles, mixed and team events are outside stage 1")
    left_name, right_name = _split_players(title)
    start = _first(payload, "start", "start-time", "start_time")
    if not start:
        raise VenueNormalizationError("Matchbook tennis event has no start")
    round_labels = []
    for name in _meta_value(payload, "other"):
        parsed = canonical_round_label(name)
        if parsed:
            round_labels.append(parsed)
    round_label = round_labels[0] if round_labels else ""
    return _build_event(
        tour=tour,
        tournament=tournament,
        round_label=round_label,
        event_type=TENNIS_EVENT_SINGLES,
        left_name=left_name,
        right_name=right_name,
        kickoff=_kickoff(start),
        venue=VenueName.MATCHBOOK,
        source_event_id=source_id,
    )


def matchbook_tennis_market(event: CanonicalEvent, payload: dict[str, Any]) -> CanonicalMarket:
    name = normalize_text(str(payload.get("name") or ""))
    market_type = normalize_text(str(payload.get("market-type") or payload.get("market_type") or ""))
    if name != "moneyline" and market_type not in {"money line", "money_line"}:
        raise VenueNormalizationError(f"unsupported tennis market family: {name or market_type or 'unknown'}")
    source_market_id = str(payload.get("id") or "").strip()
    if not source_market_id:
        raise VenueNormalizationError("Matchbook tennis market has no id")
    runners: list[CanonicalRunner] = []
    for runner in payload.get("runners") or []:
        if not isinstance(runner, dict):
            continue
        label = str(runner.get("name") or "").strip()
        runner_id = str(runner.get("id") or "").strip()
        if not label or not runner_id:
            continue
        runners.append(
            CanonicalRunner(
                source_runner_id=runner_id,
                outcome=_outcome_for_player(event, label),
                label=label,
            )
        )
    if {runner.outcome for runner in runners} != {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}:
        raise VenueNormalizationError("Matchbook tennis moneyline is not a two-player match winner")
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.MATCHBOOK,
        source_market_id=source_market_id,
        family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        line=None,
        settlement=tennis_match_winner_settlement(),
        runners=runners,
        confidence=1.0,
    )


def _polymarket_tour(payload: dict[str, Any]) -> str | None:
    sport = payload.get("sport")
    if isinstance(sport, dict):
        code = normalize_text(str(sport.get("sport") or ""))
        if code in _SINGLES_SERIES:
            return _SINGLES_SERIES[code]
        series = str(sport.get("series") or "").strip()
        if series in _SINGLES_SERIES_IDS:
            return _SINGLES_SERIES_IDS[series]
    series_id = str(payload.get("series_id") or payload.get("seriesId") or "").strip()
    if series_id in _SINGLES_SERIES_IDS:
        return _SINGLES_SERIES_IDS[series_id]
    series_items = payload.get("series")
    if isinstance(series_items, dict):
        series_items = [series_items]
    if isinstance(series_items, list):
        for item in series_items:
            if not isinstance(item, dict):
                continue
            item_id = str(item.get("id") or "").strip()
            if item_id in _SINGLES_SERIES_IDS:
                return _SINGLES_SERIES_IDS[item_id]
            ticker = normalize_text(str(item.get("ticker") or item.get("slug") or ""))
            if ticker in _SINGLES_SERIES:
                return _SINGLES_SERIES[ticker]
    return None


def polymarket_stage1_rejection(payload: dict[str, Any]) -> str | None:
    """None when this Gamma payload is an admitted ATP/WTA singles tournament.

    Doubles and ITF series fail closed even when a league label is present.
    A missing tour field is not itself a rejection: the operator scope may
    already have resolved series 10365/10366.
    """

    from sports_hedge.tennis.constants import (
        POLYMARKET_ATP_DOUBLES_SERIES_ID,
        POLYMARKET_ITF_SERIES_ID,
        POLYMARKET_WTA_DOUBLES_SERIES_ID,
    )

    blocked = {
        POLYMARKET_ATP_DOUBLES_SERIES_ID,
        POLYMARKET_WTA_DOUBLES_SERIES_ID,
        POLYMARKET_ITF_SERIES_ID,
    }
    series_ids: list[str] = []
    direct = str(payload.get("series_id") or payload.get("seriesId") or "").strip()
    if direct:
        series_ids.append(direct)
    sport = payload.get("sport")
    if isinstance(sport, dict):
        sport_series = str(sport.get("series") or "").strip()
        if sport_series:
            series_ids.append(sport_series)
    series_items = payload.get("series")
    if isinstance(series_items, dict):
        series_items = [series_items]
    if isinstance(series_items, list):
        for item in series_items:
            if isinstance(item, dict) and str(item.get("id") or "").strip():
                series_ids.append(str(item.get("id")).strip())
    if any(series_id in blocked for series_id in series_ids):
        return "tennis_event_type_not_singles"
    title = str(_first(payload, "title", "name") or "")
    if _event_type(title) != TENNIS_EVENT_SINGLES:
        return "tennis_event_type_not_singles"
    metadata = payload.get("eventMetadata") if isinstance(payload.get("eventMetadata"), dict) else {}
    league = str(metadata.get("league") or "")
    if not league and ":" in title:
        league = title.split(":", 1)[0]
    if admitted_tournament(league) is None:
        return "tennis_tournament_not_admitted"
    return None


def is_tennis_kalshi_match_payload(payload: dict[str, Any]) -> bool:
    ticker = str(payload.get("series_ticker") or payload.get("event_ticker") or payload.get("ticker") or "")
    return approved_kalshi_tennis_series(ticker) is not None and is_tennis_kalshi_ticker(ticker)

from __future__ import annotations

import json
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
    SettlementFingerprint,
    SettlementScope,
    line_push_possible,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.normalization.text import normalize_text


class VenueNormalizationError(ValueError):
    """Raised when a venue payload cannot be normalized without guessing."""


_FIXTURE_SEPARATOR = re.compile(r"\s+(?:v|vs\.?|versus)\s+", re.IGNORECASE)
_NUMBER = re.compile(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)")


class MatchbookNormalizer:
    venue = VenueName.MATCHBOOK

    def normalize_event(self, payload: dict[str, Any]) -> CanonicalEvent:
        source_id = _required_string(payload, "id")
        title = _required_string(payload, "name")
        home_team, away_team = _split_fixture_title(title)
        kickoff = _parse_datetime(_first(payload, "start", "start-time", "start_time"))
        competition = _matchbook_competition(payload) or "Unknown competition"
        confidence = 1.0 if competition != "Unknown competition" else 0.9
        return CanonicalEvent(
            competition=competition,
            home_team=home_team,
            away_team=away_team,
            kickoff_utc=kickoff,
            source_venue=self.venue,
            source_event_id=source_id,
            confidence=confidence,
        )

    def normalize_market(
        self,
        event: CanonicalEvent,
        payload: dict[str, Any],
    ) -> CanonicalMarket:
        source_market_id = _required_string(payload, "id")
        name = _required_string(payload, "name")
        family, line = _matchbook_market_family(
            name,
            payload,
            home_team=event.home_team,
            away_team=event.away_team,
        )
        period = _period_from_text(name)
        settlement = _standard_football_settlement(family=family, period=period, line=line)
        runners = [
            CanonicalRunner(
                source_runner_id=_required_string(runner, "id"),
                outcome=_canonical_runner_outcome(
                    str(runner.get("name", "")),
                    family=family,
                    home_team=event.home_team,
                    away_team=event.away_team,
                ),
                label=_required_string(runner, "name"),
            )
            for runner in payload.get("runners", [])
            if isinstance(runner, dict)
        ]
        if not runners:
            raise VenueNormalizationError(f"Matchbook market {source_market_id} has no runners")
        return CanonicalMarket(
            event=event,
            source_venue=self.venue,
            source_market_id=source_market_id,
            family=family,
            period=period,
            line=line,
            settlement=settlement,
            runners=runners,
        )


class PolymarketNormalizer:
    venue = VenueName.POLYMARKET

    def normalize_event(self, payload: dict[str, Any]) -> CanonicalEvent:
        source_id = _required_string(payload, "id")
        title = str(_first(payload, "title", "question", "name") or "").strip()
        if not title:
            raise VenueNormalizationError("Polymarket event has no title")
        home_team, away_team = _split_fixture_title(title)
        kickoff = _parse_polymarket_fixture_datetime(_polymarket_fixture_start(payload))
        competition = _polymarket_competition(payload) or "Unknown competition"
        confidence = 1.0 if competition != "Unknown competition" else 0.85
        return CanonicalEvent(
            competition=competition,
            home_team=home_team,
            away_team=away_team,
            kickoff_utc=kickoff,
            source_venue=self.venue,
            source_event_id=source_id,
            confidence=confidence,
        )

    def normalize_market(
        self,
        event: CanonicalEvent,
        payload: dict[str, Any],
    ) -> CanonicalMarket:
        source_market_id = str(_first(payload, "id", "conditionId", "condition_id") or "").strip()
        if not source_market_id:
            raise VenueNormalizationError("Polymarket market has no id/condition id")
        question = str(_first(payload, "question", "title", "groupItemTitle") or "").strip()
        if not question:
            raise VenueNormalizationError(f"Polymarket market {source_market_id} has no question")

        family, line = _polymarket_market_family(
            question,
            payload,
            home_team=event.home_team,
            away_team=event.away_team,
        )
        period = _period_from_text(question)
        settlement = _polymarket_settlement(payload, family=family, period=period, line=line)
        outcomes = _list_field(payload.get("outcomes"))
        token_ids = _list_field(
            _first(payload, "clobTokenIds", "clob_token_ids", "tokenIds", "token_ids")
        )
        if not outcomes:
            raise VenueNormalizationError(f"Polymarket market {source_market_id} has no outcomes")
        if token_ids and len(token_ids) != len(outcomes):
            raise VenueNormalizationError(
                f"Polymarket market {source_market_id} outcome/token lengths differ"
            )

        runners: list[CanonicalRunner] = []
        for index, label in enumerate(outcomes):
            source_runner_id = str(token_ids[index]) if token_ids else f"{source_market_id}:{index}"
            runners.append(
                CanonicalRunner(
                    source_runner_id=source_runner_id,
                    outcome=_canonical_runner_outcome(
                        str(label),
                        family=family,
                        home_team=event.home_team,
                        away_team=event.away_team,
                    ),
                    label=str(label),
                )
            )

        return CanonicalMarket(
            event=event,
            source_venue=self.venue,
            source_market_id=source_market_id,
            family=family,
            period=period,
            line=line,
            settlement=settlement,
            runners=runners,
            confidence=1.0 if settlement.scope != SettlementScope.UNKNOWN else 0.75,
        )

    def assemble_canonical_markets(
        self,
        event: CanonicalEvent,
        payloads: list[dict[str, Any]],
    ) -> list[CanonicalMarket]:
        """Normalize Polymarket markets, assembling complementary moneyline binaries.

        A single Yes/No moneyline is preserved as YES/NO (not a 3-way). Only when
        HOME, DRAW and AWAY Yes contracts share a settlement fingerprint are they
        promoted to a complete match-result market, matching Kalshi GAME assembly.
        Incomplete groups stay fail-closed as binaries.
        """

        normalized: list[CanonicalMarket] = []
        for payload in payloads:
            normalized.append(self.normalize_market(event, payload))
        return promote_polymarket_complete_match_result(normalized, payloads)


class KalshiNormalizer:
    """Dedicated Kalshi football normalizer. Does not reuse Polymarket parsing."""

    venue = VenueName.KALSHI

    def normalize_event(
        self,
        payload: dict[str, Any],
        *,
        series: dict[str, Any] | None = None,
    ) -> CanonicalEvent:
        source_id = str(
            _first(payload, "event_ticker", "ticker", "id") or ""
        ).strip()
        if not source_id:
            raise VenueNormalizationError("Kalshi event has no event_ticker")
        title = str(_first(payload, "title", "name") or "").strip()
        if not title:
            raise VenueNormalizationError("Kalshi event has no title")
        home_team, away_team = _split_fixture_title(title)
        kickoff = _kalshi_fixture_kickoff(payload)
        competition = _kalshi_competition(payload, series) or "Unknown competition"
        confidence = 1.0 if competition != "Unknown competition" else 0.85
        return CanonicalEvent(
            competition=competition,
            home_team=home_team,
            away_team=away_team,
            kickoff_utc=kickoff,
            source_venue=self.venue,
            source_event_id=source_id,
            confidence=confidence,
        )

    def normalize_market(
        self,
        event: CanonicalEvent,
        payload: dict[str, Any],
    ) -> CanonicalMarket:
        markets = self.assemble_canonical_markets(event, [payload])
        if len(markets) != 1:
            raise VenueNormalizationError(
                "Kalshi single-market normalize requires exactly one assembled canonical market"
            )
        return markets[0]

    def assemble_canonical_markets(
        self,
        event: CanonicalEvent,
        payloads: list[dict[str, Any]],
        *,
        series: dict[str, Any] | None = None,
    ) -> list[CanonicalMarket]:
        classified: list[_KalshiContract] = []
        for payload in payloads:
            classified.append(self._classify_contract(event, payload, series=series))

        grouped: dict[tuple[str, str, str], list[_KalshiContract]] = {}
        for item in classified:
            key = (item.family.value, item.period.value, "" if item.line is None else format(item.line, "f"))
            grouped.setdefault(key, []).append(item)

        assembled: list[CanonicalMarket] = []
        for items in grouped.values():
            assembled.append(self._assemble_group(event, items))
        return assembled

    def _classify_contract(
        self,
        event: CanonicalEvent,
        payload: dict[str, Any],
        *,
        series: dict[str, Any] | None,
    ) -> _KalshiContract:
        ticker = str(_first(payload, "ticker", "market_ticker") or "").strip()
        if not ticker:
            raise VenueNormalizationError("Kalshi market has no ticker")
        title = str(_first(payload, "title", "yes_sub_title", "subtitle") or "").strip()
        if not title:
            raise VenueNormalizationError(f"Kalshi market {ticker} has no title")
        family, line, yes_outcome = _kalshi_market_family(
            payload,
            home_team=event.home_team,
            away_team=event.away_team,
        )
        period = _period_from_text(
            " ".join(
                str(value)
                for value in (
                    payload.get("title"),
                    payload.get("yes_sub_title"),
                    payload.get("subtitle"),
                )
                if value
            )
        )
        settlement = _kalshi_settlement(
            payload,
            series=series,
            family=family,
            period=period,
            line=line,
        )
        return _KalshiContract(
            ticker=ticker,
            payload=payload,
            family=family,
            period=period,
            line=line,
            yes_outcome=yes_outcome,
            settlement=settlement,
        )

    def _assemble_group(
        self,
        event: CanonicalEvent,
        items: list[_KalshiContract],
    ) -> CanonicalMarket:
        family = items[0].family
        period = items[0].period
        line = items[0].line
        settlements = {item.settlement.deterministic_key() for item in items}
        if len(settlements) != 1:
            raise VenueNormalizationError(
                f"Kalshi {family.value} contracts do not share a settlement fingerprint"
            )
        settlement = items[0].settlement
        if family is MarketFamily.MATCH_RESULT:
            return _assemble_match_result(event, items, settlement)
        if family is MarketFamily.BOTH_TEAMS_TO_SCORE:
            return _assemble_binary_yes_no(event, items, settlement, family=family, period=period, line=line)
        if family is MarketFamily.TOTAL_GOALS:
            return _assemble_total_goals(event, items, settlement)
        if family is MarketFamily.FIRST_TEAM_TO_SCORE:
            return _assemble_first_team_to_score(event, items, settlement)
        if family is MarketFamily.DRAW_NO_BET:
            raise VenueNormalizationError(
                "Kalshi Draw No Bet is deferred until draw-refund rules are proven"
            )
        raise VenueNormalizationError(f"Kalshi family {family.value} remains inventory-deferred")


class _KalshiContract:
    def __init__(
        self,
        *,
        ticker: str,
        payload: dict[str, Any],
        family: MarketFamily,
        period: FootballPeriod,
        line: Decimal | None,
        yes_outcome: CanonicalOutcome,
        settlement: SettlementFingerprint,
    ) -> None:
        self.ticker = ticker
        self.payload = payload
        self.family = family
        self.period = period
        self.line = line
        self.yes_outcome = yes_outcome
        self.settlement = settlement


# Kalshi Trade API Market.occurrence_datetime is "the recorded datetime when
# the underlying event occurred". For upcoming soccer it is currently a copy
# of expected_expiration_time (match-end / settlement window), not kickoff.
# Authoritative scheduled kickoff is the soccer milestone start_date.
_KALSHI_EXPIRATION_KEYS = (
    "expected_expiration_time",
    "expiration_time",
    "latest_expiration_time",
    "close_time",
    "end_date",
)
_KALSHI_KICKOFF_CANDIDATE_KEYS = (
    "start_date",
    "target_datetime",
    "game_start_time",
    "scheduled_start",
    "strike_date",
    "occurrence_datetime",
)


def _kalshi_nested_occurrence(payload: dict[str, Any]) -> Any:
    markets = payload.get("markets")
    if isinstance(markets, list):
        for market in markets:
            if isinstance(market, dict):
                nested = _first(market, "occurrence_datetime", "target_datetime")
                if nested:
                    return nested
    return None


def _kalshi_datetime_payloads(payload: dict[str, Any]) -> list[dict[str, Any]]:
    payloads = [payload]
    milestone = payload.get("milestone")
    if isinstance(milestone, dict):
        payloads.append(milestone)
    markets = payload.get("markets")
    if isinstance(markets, list):
        payloads.extend(item for item in markets if isinstance(item, dict))
    return payloads


def _kalshi_expiration_clocks(payload: dict[str, Any]) -> set[datetime]:
    clocks: set[datetime] = set()
    for item in _kalshi_datetime_payloads(payload):
        for key in _KALSHI_EXPIRATION_KEYS:
            raw = item.get(key)
            parsed = _try_parse_aware_datetime(raw)
            if parsed is not None:
                clocks.add(parsed)
    return clocks


def _kalshi_kickoff_candidates(payload: dict[str, Any]) -> list[Any]:
    candidates: list[Any] = []
    milestone = payload.get("milestone")
    if isinstance(milestone, dict):
        start = milestone.get("start_date")
        if start:
            candidates.append(start)
    for item in _kalshi_datetime_payloads(payload):
        for key in _KALSHI_KICKOFF_CANDIDATE_KEYS:
            raw = item.get(key)
            if raw:
                candidates.append(raw)
    nested = _kalshi_nested_occurrence(payload)
    if nested:
        candidates.append(nested)
    return candidates


def _kalshi_fixture_kickoff(payload: dict[str, Any]) -> datetime:
    """Scheduled kickoff from Kalshi milestone/start fields, never expiration.

    occurrence_datetime that equals expected_expiration_time is an expiration
    clock. Date-only values are refused rather than assumed as midnight UTC.
    """

    expiration = _kalshi_expiration_clocks(payload)
    seen: set[datetime] = set()
    for raw in _kalshi_kickoff_candidates(payload):
        if _is_date_only_clock(raw):
            continue
        parsed = _try_parse_aware_datetime(raw)
        if parsed is None or parsed in seen:
            continue
        seen.add(parsed)
        if parsed in expiration:
            continue
        return parsed
    raise VenueNormalizationError(
        "Kalshi event has no scheduled kickoff; occurrence_datetime/expected_expiration_time "
        "are expiration clocks, not kickoff. Use the soccer milestone start_date."
    )


def _is_date_only_clock(value: Any) -> bool:
    if isinstance(value, datetime):
        return False
    text = str(value or "").strip()
    return bool(_DATE_ONLY.fullmatch(text))


def _try_parse_aware_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return _parse_datetime(value)
    except VenueNormalizationError:
        return None


def _kalshi_competition(payload: dict[str, Any], series: dict[str, Any] | None) -> str | None:
    from sports_hedge.application.target_competitions import resolve_target_competition

    metadata = payload.get("product_metadata")
    if isinstance(metadata, dict):
        code = metadata.get("competition")
        resolved = resolve_target_competition(str(code) if code else None)
        if resolved is not None:
            return resolved.display_name
    series_ticker = str(payload.get("series_ticker") or (series or {}).get("ticker") or "")
    mapped = _kalshi_competition_from_series_ticker(series_ticker)
    if mapped:
        return mapped
    direct = _first(payload, "competition", "league", "series_title")
    if isinstance(direct, str) and direct.strip():
        resolved = resolve_target_competition(direct)
        return resolved.display_name if resolved is not None else direct.strip()
    if series:
        title = str(_first(series, "title", "name") or "").strip()
        if title:
            resolved = resolve_target_competition(title)
            return resolved.display_name if resolved is not None else title
    nested = payload.get("series")
    if isinstance(nested, dict):
        title = str(_first(nested, "title", "name") or "").strip()
        if title:
            resolved = resolve_target_competition(title)
            return resolved.display_name if resolved is not None else title
    return None


def _kalshi_competition_from_series_ticker(series_ticker: str) -> str | None:
    from sports_hedge.application.target_competitions import resolve_target_competition

    ticker = series_ticker.upper()
    if ticker.startswith("KXEPL"):
        resolved = resolve_target_competition("Premier League")
        return resolved.display_name if resolved else "English Premier League"
    if ticker.startswith("KXEFLCHAMPIONSHIP"):
        resolved = resolve_target_competition("Championship")
        return resolved.display_name if resolved else "EFL Championship"
    if ticker.startswith("KXLALIGA") and not ticker.startswith("KXLALIGA2"):
        resolved = resolve_target_competition("La Liga")
        return resolved.display_name if resolved else "Spain La Liga"
    return None


def _kalshi_market_family(
    payload: dict[str, Any],
    *,
    home_team: str,
    away_team: str,
) -> tuple[MarketFamily, Decimal | None, CanonicalOutcome]:
    title = str(_first(payload, "title", "yes_sub_title", "subtitle") or "")
    yes_label = str(_first(payload, "yes_sub_title", "yes_subtitle", "subtitle") or title)
    rules = " ".join(
        str(value)
        for value in (
            payload.get("rules_primary"),
            payload.get("rules_secondary"),
            payload.get("rules"),
            payload.get("description"),
        )
        if value
    )
    combined = normalize_text(f"{title} {yes_label} {rules}")
    line = _line_from_payload_or_text(payload, f"{title} {yes_label}")
    if any(token in combined for token in ("to qualify", "qualification", "advance")):
        raise VenueNormalizationError("Kalshi To Qualify is not inferred from titles")
    if "asian handicap" in combined or "handicap" in combined or "spread" in combined:
        raise VenueNormalizationError("Kalshi Asian Handicap is not inferred from titles")
    if "correct score" in combined:
        raise VenueNormalizationError("Kalshi Correct Score is not inferred from titles")
    if "next goal" in combined or "next team to score" in combined:
        raise VenueNormalizationError("Kalshi Next Goal is not inferred from titles")
    if _is_player_goal_market(combined):
        raise VenueNormalizationError("Kalshi player props are not inferred from titles")
    if "draw no bet" in combined:
        raise VenueNormalizationError(
            "Kalshi Draw No Bet remains deferred until draw-refund semantics are proven"
        )
    if "both teams to score" in combined or "btts" in combined:
        return MarketFamily.BOTH_TEAMS_TO_SCORE, None, CanonicalOutcome.YES
    if "total goal" in combined or ("over" in combined and "under" in combined) or (
        "over" in combined and "goal" in combined
    ):
        if line is None:
            raise VenueNormalizationError("Kalshi totals market has no line")
        if line_push_possible(line) is not False:
            raise VenueNormalizationError(
                "Kalshi integer/quarter Total Goals remain deferred until push/refund rules are proven"
            )
        yes_outcome = _canonical_runner_outcome(
            yes_label,
            family=MarketFamily.TOTAL_GOALS,
            home_team=home_team,
            away_team=away_team,
        )
        if yes_outcome not in {CanonicalOutcome.OVER, CanonicalOutcome.UNDER}:
            if "over" in normalize_text(yes_label) or "over" in combined:
                yes_outcome = CanonicalOutcome.OVER
            else:
                raise VenueNormalizationError("Kalshi totals YES side is not Over/Under")
        return MarketFamily.TOTAL_GOALS, line, yes_outcome
    if _explicit_first_team_to_score(combined) or _payload_is_team_level_first_score(
        payload, home_team=home_team, away_team=away_team
    ):
        yes_outcome = _canonical_runner_outcome(
            yes_label,
            family=MarketFamily.FIRST_TEAM_TO_SCORE,
            home_team=home_team,
            away_team=away_team,
        )
        return MarketFamily.FIRST_TEAM_TO_SCORE, None, yes_outcome
    yes_outcome = _canonical_runner_outcome(
        yes_label,
        family=MarketFamily.MATCH_RESULT,
        home_team=home_team,
        away_team=away_team,
    )
    if yes_outcome in {CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY}:
        return MarketFamily.MATCH_RESULT, None, yes_outcome
    if "match result" in combined or "moneyline" in combined or "to win" in combined:
        return MarketFamily.MATCH_RESULT, None, yes_outcome
    raise VenueNormalizationError(f"Unsupported Kalshi sports market: {title}")


def _kalshi_settlement(
    payload: dict[str, Any],
    *,
    series: dict[str, Any] | None,
    family: MarketFamily,
    period: FootballPeriod,
    line: Decimal | None,
) -> SettlementFingerprint:
    parts = [
        payload.get("rules_primary"),
        payload.get("rules_secondary"),
        payload.get("rules"),
        payload.get("description"),
        payload.get("settlement_source"),
    ]
    if series:
        parts.extend(
            [
                series.get("contract_terms_url"),
                json.dumps(series.get("settlement_sources") or []),
            ]
        )
    text = normalize_text(" ".join(str(value) for value in parts if value))
    if not text.strip() or text.strip() in {"[]", ""}:
        return SettlementFingerprint(
            scope=SettlementScope.UNKNOWN,
            period=period,
            line=line if family in {MarketFamily.TOTAL_GOALS, MarketFamily.ASIAN_HANDICAP} else None,
            push_possible=_family_push_possible(family, line),
            extra_time_included=None,
            penalties_included=None,
            source_rule_version=str(_first(payload, "ticker", "market_ticker") or "") or None,
        )
    if "including penalties" in text:
        scope = SettlementScope.INCLUDING_PENALTIES
        extra_time = True
        penalties = True
    elif "including extra time" in text:
        scope = SettlementScope.INCLUDING_EXTRA_TIME
        extra_time = True
        penalties = False
    elif "90 minutes" in text or "regulation time" in text or "regulation-time" in text:
        scope = SettlementScope.REGULATION_TIME
        extra_time = False
        penalties = False
    else:
        scope = SettlementScope.UNKNOWN
        extra_time = None
        penalties = None
    return SettlementFingerprint(
        scope=scope,
        period=period,
        line=line if family in {MarketFamily.TOTAL_GOALS, MarketFamily.ASIAN_HANDICAP} else None,
        push_possible=_family_push_possible(family, line),
        extra_time_included=extra_time,
        penalties_included=penalties,
        source_rule_version=str(_first(payload, "ticker", "market_ticker") or "") or None,
    )


def _assemble_match_result(
    event: CanonicalEvent,
    items: list[_KalshiContract],
    settlement: SettlementFingerprint,
) -> CanonicalMarket:
    by_outcome = {item.yes_outcome: item for item in items}
    required = {CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY}
    if set(by_outcome) != required:
        raise VenueNormalizationError(
            "Kalshi Match Result requires exhaustive HOME/DRAW/AWAY YES contracts"
        )
    runners = [
        CanonicalRunner(
            source_runner_id=f"{by_outcome[outcome].ticker}:YES",
            outcome=outcome,
            label=str(
                _first(by_outcome[outcome].payload, "yes_sub_title", "title") or outcome.value
            ),
        )
        for outcome in (CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY)
    ]
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.KALSHI,
        source_market_id=f"{event.source_event_id}:match_result",
        family=MarketFamily.MATCH_RESULT,
        period=items[0].period,
        line=None,
        settlement=settlement,
        runners=runners,
        confidence=1.0 if settlement.scope != SettlementScope.UNKNOWN else 0.75,
    )


def polymarket_moneyline_yes_outcome(
    question: str,
    *,
    home_team: str,
    away_team: str,
) -> CanonicalOutcome | None:
    """Map a Polymarket moneyline question to the YES-side 3-way outcome.

    Fail closed on ambiguous titles (both teams, home-or-draw, etc.).
    """

    text = normalize_text(question)
    home = normalize_text(home_team)
    away = normalize_text(away_team)
    if not text:
        return None
    if any(token in text for token in ("or draw", "double chance", "draw no bet", "to qualify")):
        return None
    draw_like = "draw" in text or "tie" in text
    home_present = bool(home) and home in text
    away_present = bool(away) and away in text
    if draw_like and not home_present and not away_present:
        return CanonicalOutcome.DRAW
    if home_present and away_present:
        return None
    if "win" not in text and "beat" not in text:
        if home_present:
            return CanonicalOutcome.HOME
        if away_present:
            return CanonicalOutcome.AWAY
        return None
    if home_present:
        return CanonicalOutcome.HOME
    if away_present:
        return CanonicalOutcome.AWAY
    return None


def promote_polymarket_complete_match_result(
    markets: list[CanonicalMarket],
    payloads: list[dict[str, Any]] | None = None,
) -> list[CanonicalMarket]:
    """Replace complementary Yes/No moneylines with a 3-way when exhaustive.

    Does not invent a comparison: one or two binaries stay YES/NO and will not
    match a Kalshi HOME/DRAW/AWAY GAME market.
    """

    payload_by_id: dict[str, dict[str, Any]] = {}
    for payload in payloads or []:
        source_id = str(_first(payload, "id", "conditionId", "condition_id") or "").strip()
        if source_id:
            payload_by_id[source_id] = payload

    binaries: list[tuple[CanonicalMarket, CanonicalOutcome]] = []
    passthrough: list[CanonicalMarket] = []
    for market in markets:
        if market.family is not MarketFamily.MATCH_RESULT:
            passthrough.append(market)
            continue
        present = {runner.outcome for runner in market.runners}
        if present == {CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY}:
            passthrough.append(market)
            continue
        if present != {CanonicalOutcome.YES, CanonicalOutcome.NO}:
            passthrough.append(market)
            continue
        payload = payload_by_id.get(market.source_market_id, {})
        question = str(_first(payload, "question", "title", "groupItemTitle") or "").strip()
        yes_outcome = polymarket_moneyline_yes_outcome(
            question,
            home_team=market.event.home_team,
            away_team=market.event.away_team,
        )
        if yes_outcome is None:
            passthrough.append(market)
            continue
        binaries.append((market, yes_outcome))

    grouped: dict[tuple[str, str], list[tuple[CanonicalMarket, CanonicalOutcome]]] = {}
    for market, yes_outcome in binaries:
        key = (market.period.value, market.settlement.deterministic_key())
        grouped.setdefault(key, []).append((market, yes_outcome))

    result = list(passthrough)
    for items in grouped.values():
        by_outcome: dict[CanonicalOutcome, CanonicalMarket] = {}
        unique = True
        for market, yes_outcome in items:
            if yes_outcome in by_outcome:
                unique = False
                break
            by_outcome[yes_outcome] = market
        required = {CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY}
        if not unique or set(by_outcome) != required:
            result.extend(market for market, _outcome in items)
            continue
        settlements = {market.settlement.deterministic_key() for market in by_outcome.values()}
        if len(settlements) != 1:
            result.extend(market for market, _outcome in items)
            continue
        first = by_outcome[CanonicalOutcome.HOME]
        runners: list[CanonicalRunner] = []
        for outcome in (CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY):
            binary = by_outcome[outcome]
            yes_runner = next(runner for runner in binary.runners if runner.outcome is CanonicalOutcome.YES)
            runners.append(
                CanonicalRunner(
                    source_runner_id=yes_runner.source_runner_id,
                    outcome=outcome,
                    label=yes_runner.label if outcome is CanonicalOutcome.DRAW else (
                        binary.event.home_team if outcome is CanonicalOutcome.HOME else binary.event.away_team
                    ),
                )
            )
        result.append(
            CanonicalMarket(
                event=first.event,
                source_venue=VenueName.POLYMARKET,
                source_market_id=f"{first.event.source_event_id}:match_result",
                family=MarketFamily.MATCH_RESULT,
                period=first.period,
                line=None,
                settlement=first.settlement,
                runners=runners,
                confidence=min(item.confidence for item in by_outcome.values()),
            )
        )
    return result


def _assemble_binary_yes_no(
    event: CanonicalEvent,
    items: list[_KalshiContract],
    settlement: SettlementFingerprint,
    *,
    family: MarketFamily,
    period: FootballPeriod,
    line: Decimal | None,
) -> CanonicalMarket:
    if len(items) != 1:
        raise VenueNormalizationError(
            f"Kalshi {family.value} must be a single binary YES/NO contract"
        )
    item = items[0]
    ticker = item.ticker
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.KALSHI,
        source_market_id=ticker,
        family=family,
        period=period,
        line=line,
        settlement=settlement,
        runners=[
            CanonicalRunner(source_runner_id=f"{ticker}:YES", outcome=CanonicalOutcome.YES, label="Yes"),
            CanonicalRunner(source_runner_id=f"{ticker}:NO", outcome=CanonicalOutcome.NO, label="No"),
        ],
        confidence=1.0 if settlement.scope != SettlementScope.UNKNOWN else 0.75,
    )


def _assemble_total_goals(
    event: CanonicalEvent,
    items: list[_KalshiContract],
    settlement: SettlementFingerprint,
) -> CanonicalMarket:
    if len(items) != 1:
        raise VenueNormalizationError("Kalshi totals must not collapse unrelated line contracts")
    item = items[0]
    yes = item.yes_outcome
    no = CanonicalOutcome.UNDER if yes is CanonicalOutcome.OVER else CanonicalOutcome.OVER
    if {yes, no} != {CanonicalOutcome.OVER, CanonicalOutcome.UNDER}:
        raise VenueNormalizationError("Kalshi totals YES/NO must map to Over/Under")
    ticker = item.ticker
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.KALSHI,
        source_market_id=ticker,
        family=MarketFamily.TOTAL_GOALS,
        period=item.period,
        line=item.line,
        settlement=settlement,
        runners=[
            CanonicalRunner(source_runner_id=f"{ticker}:YES", outcome=yes, label="Yes"),
            CanonicalRunner(source_runner_id=f"{ticker}:NO", outcome=no, label="No"),
        ],
        confidence=1.0 if settlement.scope != SettlementScope.UNKNOWN else 0.75,
    )


def _assemble_first_team_to_score(
    event: CanonicalEvent,
    items: list[_KalshiContract],
    settlement: SettlementFingerprint,
) -> CanonicalMarket:
    by_outcome = {item.yes_outcome: item for item in items}
    required = {CanonicalOutcome.HOME, CanonicalOutcome.AWAY, CanonicalOutcome.NO_GOAL}
    if set(by_outcome) != required:
        raise VenueNormalizationError(
            "Kalshi First Team To Score requires HOME/AWAY/NO_GOAL contracts"
        )
    if settlement.scope is not SettlementScope.REGULATION_TIME:
        raise VenueNormalizationError(
            "Kalshi First Team To Score requires proven regulation-time rules"
        )
    runners = [
        CanonicalRunner(
            source_runner_id=f"{by_outcome[outcome].ticker}:YES",
            outcome=outcome,
            label=str(_first(by_outcome[outcome].payload, "yes_sub_title", "title") or outcome.value),
        )
        for outcome in (CanonicalOutcome.HOME, CanonicalOutcome.AWAY, CanonicalOutcome.NO_GOAL)
    ]
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.KALSHI,
        source_market_id=f"{event.source_event_id}:first_team_to_score",
        family=MarketFamily.FIRST_TEAM_TO_SCORE,
        period=items[0].period,
        line=None,
        settlement=settlement,
        runners=runners,
        confidence=1.0,
    )


def _matchbook_competition(payload: dict[str, Any]) -> str | None:
    direct = _first(payload, "competition-name", "competition_name", "competition")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    for tag in payload.get("meta-tags", payload.get("meta_tags", [])) or []:
        if not isinstance(tag, dict):
            continue
        tag_type = normalize_text(str(tag.get("type", "")))
        name = str(tag.get("name", "")).strip()
        if name and tag_type in {"competition", "league", "tournament"}:
            return name
    return None


# Observed Gamma football fixture clocks. Listing/creation ``startDate``,
# date-only ``eventDate``, and settlement ``endDate`` are not kickoff.
_POLYMARKET_FIXTURE_START_KEYS = (
    "startTime",
    "gameStartTime",
    "start_time",
    "game_start_time",
)
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _polymarket_fixture_start(payload: dict[str, Any]) -> Any:
    raw = _first(payload, *_POLYMARKET_FIXTURE_START_KEYS)
    if raw:
        return raw
    markets = payload.get("markets")
    if isinstance(markets, list):
        for market in markets:
            if isinstance(market, dict):
                nested = _first(market, *_POLYMARKET_FIXTURE_START_KEYS)
                if nested:
                    return nested
    return None


def _parse_polymarket_fixture_datetime(value: Any) -> datetime:
    if not value:
        raise VenueNormalizationError(
            "Polymarket event has no supported fixture start time; leaving unmatched"
        )
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise VenueNormalizationError(
                "Polymarket fixture time is timezone-naive; leaving unmatched"
            )
        return value.astimezone(UTC)
    text = str(value).strip()
    if _DATE_ONLY.fullmatch(text):
        raise VenueNormalizationError(
            "Polymarket fixture time is date-only; refusing midnight guess"
        )
    normalized = text.replace("Z", "+00:00")
    if re.search(r"[+-]\d{2}$", normalized):
        normalized = f"{normalized}:00"
    if " " in normalized and "T" not in normalized:
        normalized = normalized.replace(" ", "T", 1)
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise VenueNormalizationError(f"Invalid Polymarket fixture datetime: {value}") from exc
    if parsed.tzinfo is None:
        raise VenueNormalizationError(
            "Polymarket fixture time is timezone-naive; leaving unmatched"
        )
    return parsed.astimezone(UTC)


def _polymarket_competition(payload: dict[str, Any]) -> str | None:
    direct = _first(payload, "competition", "league", "seriesTitle", "series_title")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    series = payload.get("series")
    if isinstance(series, list):
        for item in series:
            if isinstance(item, dict):
                title = str(_first(item, "title", "name") or "").strip()
                if title:
                    return title
    return None


def _matchbook_market_family(
    name: str,
    payload: dict[str, Any],
    *,
    home_team: str,
    away_team: str,
) -> tuple[MarketFamily, Decimal | None]:
    text = normalize_text(name)
    line = _line_from_payload_or_text(payload, name)
    if text in {"match odds", "match result", "moneyline", "full time result"}:
        return MarketFamily.MATCH_RESULT, None
    if "draw no bet" in text:
        return MarketFamily.DRAW_NO_BET, None
    if "double chance" in text:
        return MarketFamily.DOUBLE_CHANCE, None
    if "both teams to score" in text or text == "btts":
        return MarketFamily.BOTH_TEAMS_TO_SCORE, None
    if "corner" in text:
        return MarketFamily.CORNERS, line
    if "card" in text or "booking" in text:
        return MarketFamily.CARDS, line
    if "asian handicap" in text or text.startswith("handicap"):
        return MarketFamily.ASIAN_HANDICAP, line
    if any(
        token in text
        for token in ("team total", "home total", "away total", "participant total")
    ):
        return MarketFamily.TEAM_TOTAL, line
    if _matchbook_looks_like_match_total(text):
        if _matchbook_is_participant_or_team_total(
            name,
            payload,
            home_team=home_team,
            away_team=away_team,
        ):
            return MarketFamily.TEAM_TOTAL, line
        return MarketFamily.TOTAL_GOALS, line
    if "correct score" in text:
        return MarketFamily.CORRECT_SCORE, None
    if "half time full time" in text:
        return MarketFamily.HALF_TIME_FULL_TIME, None
    if "to qualify" in text or "qualification" in text:
        return MarketFamily.TO_QUALIFY, None
    if "next goal" in text or "next team to score" in text:
        return MarketFamily.NEXT_GOAL, None
    if _is_player_goal_market(text):
        return MarketFamily.PLAYER_PROPS, None
    if _is_first_team_to_score_market(
        text,
        payload,
        home_team=home_team,
        away_team=away_team,
    ):
        return MarketFamily.FIRST_TEAM_TO_SCORE, None
    raise VenueNormalizationError(f"Unsupported Matchbook market: {name}")


def _polymarket_market_family(
    question: str,
    payload: dict[str, Any],
    *,
    home_team: str,
    away_team: str,
) -> tuple[MarketFamily, Decimal | None]:
    text = normalize_text(question)
    sports_type = normalize_text(
        str(_first(payload, "sportsMarketType", "sports_market_type", "marketType") or "")
    )
    line = _line_from_payload_or_text(payload, question)
    combined = f"{sports_type} {text}".strip()
    if "corner" in combined:
        return MarketFamily.CORNERS, line
    if "card" in combined or "booking" in combined:
        return MarketFamily.CARDS, line
    if any(token in combined for token in ("player prop", "player shots", "player goal")):
        return MarketFamily.PLAYER_PROPS, line
    if _is_player_goal_market(combined):
        return MarketFamily.PLAYER_PROPS, line
    if "both teams to score" in combined or "btts" in combined:
        return MarketFamily.BOTH_TEAMS_TO_SCORE, None
    if "total goal" in combined or "over under" in combined and "goal" in combined:
        return MarketFamily.TOTAL_GOALS, line
    if "handicap" in combined or "spread" in sports_type:
        return MarketFamily.ASIAN_HANDICAP, line
    if "draw no bet" in combined:
        return MarketFamily.DRAW_NO_BET, None
    if "to qualify" in combined:
        return MarketFamily.TO_QUALIFY, None
    if "next goal" in combined or "next team to score" in combined:
        return MarketFamily.NEXT_GOAL, None
    if _is_first_team_to_score_market(
        combined,
        payload,
        home_team=home_team,
        away_team=away_team,
    ):
        return MarketFamily.FIRST_TEAM_TO_SCORE, None
    if "moneyline" in sports_type or "match result" in combined or "to win" in text:
        return MarketFamily.MATCH_RESULT, None
    if "correct score" in combined:
        return MarketFamily.CORRECT_SCORE, None
    raise VenueNormalizationError(f"Unsupported Polymarket sports market: {question}")


def _canonical_runner_outcome(
    label: str,
    *,
    family: MarketFamily,
    home_team: str,
    away_team: str,
) -> CanonicalOutcome:
    text = normalize_text(label)
    home = normalize_text(home_team)
    away = normalize_text(away_team)
    if family is MarketFamily.TO_QUALIFY:
        if text in {home, "home qualify", "home to qualify"} or (
            home and home in text and "qualify" in text
        ):
            return CanonicalOutcome.HOME_QUALIFY
        if text in {away, "away qualify", "away to qualify"} or (
            away and away in text and "qualify" in text
        ):
            return CanonicalOutcome.AWAY_QUALIFY
        return CanonicalOutcome.OTHER
    if family is MarketFamily.FIRST_TEAM_TO_SCORE and _is_no_goal_runner(text):
        return CanonicalOutcome.NO_GOAL
    if text == home:
        return CanonicalOutcome.HOME
    if text == away:
        return CanonicalOutcome.AWAY
    if family is MarketFamily.FIRST_TEAM_TO_SCORE and text in {"home", "home team"}:
        return CanonicalOutcome.HOME
    if family is MarketFamily.FIRST_TEAM_TO_SCORE and text in {"away", "away team"}:
        return CanonicalOutcome.AWAY
    if text in {"draw", "tie"}:
        return CanonicalOutcome.DRAW
    if text == "yes":
        return CanonicalOutcome.YES
    if text == "no":
        return CanonicalOutcome.NO
    if text.startswith("over"):
        return CanonicalOutcome.OVER
    if text.startswith("under"):
        return CanonicalOutcome.UNDER
    if family == MarketFamily.DOUBLE_CHANCE:
        if any(token in text for token in ("home or draw", "1x")):
            return CanonicalOutcome.HOME_OR_DRAW
        if any(token in text for token in ("home or away", "12")):
            return CanonicalOutcome.HOME_OR_AWAY
        if any(token in text for token in ("draw or away", "x2")):
            return CanonicalOutcome.DRAW_OR_AWAY
    return CanonicalOutcome.OTHER


_PLAYER_GOAL_TOKENS = (
    "first goalscorer",
    "first goal scorer",
    "anytime scorer",
    "anytime goalscorer",
    "player to score",
    "last goalscorer",
    "last goal scorer",
)

_NO_GOAL_RUNNER_LABELS = {
    "no goal",
    "no goals",
    "neither",
    "neither team",
    "neither scores",
    "neither team to score",
    "no score",
    "no scorer",
    "none",
    "no",
    "no team",
    "no team to score",
}


def _is_player_goal_market(text: str) -> bool:
    return any(token in text for token in _PLAYER_GOAL_TOKENS)


def _explicit_first_team_to_score(text: str) -> bool:
    return (
        "first team to score" in text
        or "team to score first" in text
        or "first team goal" in text
        or text in {"ftts", "first team to score"}
    )


def _ambiguous_first_goal_name(text: str) -> bool:
    if "next" in text or "scorer" in text or "player" in text or "anytime" in text:
        return False
    return text in {"first goal", "first to score", "to score first"} or (
        "first goal" in text and "team" not in text
    )


def _is_no_goal_runner(text: str) -> bool:
    return text in _NO_GOAL_RUNNER_LABELS or text.startswith("no goal")


_MATCHBOOK_PARTICIPANT_KEYS = (
    "event-participant-id",
    "event_participant_id",
    "eventParticipantId",
    "participant-id",
    "participant_id",
)


def matchbook_raw_market_type(payload: dict[str, Any]) -> str | None:
    """Return the provider market-type/grading-type without interpreting it."""

    value = _first(payload, "market-type", "market_type", "grading-type", "grading_type")
    if value is None or str(value).strip() == "":
        value = payload.get("type")
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def _matchbook_looks_like_match_total(text: str) -> bool:
    return "total goal" in text or ("over under" in text and "goal" in text)


def _scalar_id(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    return text or None


def _matchbook_market_participant_id(payload: dict[str, Any]) -> str | None:
    for key in _MATCHBOOK_PARTICIPANT_KEYS:
        found = _scalar_id(payload.get(key))
        if found:
            return found
    return None


def _matchbook_runner_participant_ids(payload: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    runners = payload.get("runners")
    if not isinstance(runners, list):
        return ids
    for runner in runners:
        if not isinstance(runner, dict):
            continue
        for key in _MATCHBOOK_PARTICIPANT_KEYS:
            found = _scalar_id(runner.get(key))
            if found:
                ids.add(found)
    return ids


def _text_mentions_exactly_one_team(text: str, *, home_team: str, away_team: str) -> bool:
    normalized = normalize_text(text)
    home = normalize_text(home_team)
    away = normalize_text(away_team)
    home_in = bool(home) and home in normalized
    away_in = bool(away) and away in normalized
    return home_in != away_in


def _matchbook_is_participant_or_team_total(
    name: str,
    payload: dict[str, Any],
    *,
    home_team: str,
    away_team: str,
) -> bool:
    """True when the book is a team/participant total, not the full-match total.

    A team total must never be canonicalized as full-match TOTAL_GOALS. Missing
    or conflicting scope evidence is treated as participant-scoped (fail closed).
    """

    text = normalize_text(name)
    if any(
        token in text
        for token in ("team total", "home total", "away total", "participant total")
    ):
        return True
    if _text_mentions_exactly_one_team(text, home_team=home_team, away_team=away_team):
        return True
    if _matchbook_market_participant_id(payload):
        return True
    runner_ids = _matchbook_runner_participant_ids(payload)
    if len(runner_ids) == 1:
        return True
    if _text_mentions_exactly_one_team(
        " ".join(_payload_runner_labels(payload)),
        home_team=home_team,
        away_team=away_team,
    ):
        return True
    if _matchbook_looks_like_match_total(text) and len(runner_ids) > 1:
        # Over/Under runners carrying distinct participant ids is unproven match
        # vs team scope. Fail closed: do not treat as full-match TOTAL_GOALS.
        return True
    return False


def _payload_runner_labels(payload: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    runners = payload.get("runners")
    if isinstance(runners, list):
        for runner in runners:
            if isinstance(runner, dict):
                name = str(runner.get("name") or runner.get("label") or "").strip()
                if name:
                    labels.append(name)
    if labels:
        return labels
    return [str(item) for item in _list_field(payload.get("outcomes")) if str(item).strip()]


def _payload_is_team_level_first_score(
    payload: dict[str, Any],
    *,
    home_team: str,
    away_team: str,
) -> bool:
    labels = _payload_runner_labels(payload)
    if len(labels) < 3:
        return False
    mapped = {
        _canonical_runner_outcome(
            label,
            family=MarketFamily.FIRST_TEAM_TO_SCORE,
            home_team=home_team,
            away_team=away_team,
        )
        for label in labels
    }
    return mapped == {
        CanonicalOutcome.HOME,
        CanonicalOutcome.AWAY,
        CanonicalOutcome.NO_GOAL,
    }


def _is_first_team_to_score_market(
    text: str,
    payload: dict[str, Any],
    *,
    home_team: str,
    away_team: str,
) -> bool:
    if _is_player_goal_market(text) or "next goal" in text or "next team to score" in text:
        return False
    if _explicit_first_team_to_score(text):
        return True
    if _ambiguous_first_goal_name(text):
        return _payload_is_team_level_first_score(
            payload,
            home_team=home_team,
            away_team=away_team,
        )
    return False


def _standard_football_settlement(
    *,
    family: MarketFamily,
    period: FootballPeriod,
    line: Decimal | None,
) -> SettlementFingerprint:
    # Matchbook market payloads do not carry resolution-rule text. Do not infer
    # extra-time/penalty semantics from the To Qualify family name alone.
    if family is MarketFamily.TO_QUALIFY:
        return SettlementFingerprint(
            scope=SettlementScope.UNKNOWN,
            period=period,
            line=None,
            push_possible=None,
            extra_time_included=None,
            penalties_included=None,
        )
    if period == FootballPeriod.FULL_TIME:
        scope = SettlementScope.REGULATION_TIME
        extra_time = False
        penalties = False
    else:
        scope = SettlementScope.PERIOD_ONLY
        extra_time = False
        penalties = False
    push = _family_push_possible(family, line)
    return SettlementFingerprint(
        scope=scope,
        period=period,
        line=line if family in {MarketFamily.TOTAL_GOALS, MarketFamily.ASIAN_HANDICAP} else None,
        push_possible=push,
        extra_time_included=extra_time,
        penalties_included=penalties,
    )


def _polymarket_settlement(
    payload: dict[str, Any],
    *,
    family: MarketFamily,
    period: FootballPeriod,
    line: Decimal | None,
) -> SettlementFingerprint:
    rules_text = " ".join(
        str(value)
        for value in (
            payload.get("description"),
            payload.get("rules"),
            payload.get("resolutionCriteria"),
            payload.get("resolution_criteria"),
        )
        if value
    )
    text = normalize_text(rules_text)
    if "including penalties" in text:
        scope = SettlementScope.INCLUDING_PENALTIES
        extra_time = True
        penalties = True
    elif "including extra time" in text:
        scope = SettlementScope.INCLUDING_EXTRA_TIME
        extra_time = True
        penalties = False
    elif "90 minutes" in text or "regulation time" in text:
        scope = SettlementScope.REGULATION_TIME
        extra_time = False
        penalties = False
    else:
        scope = SettlementScope.UNKNOWN
        extra_time = None
        penalties = None
    if family is MarketFamily.TO_QUALIFY:
        period = FootballPeriod.FULL_TIME
        line = None
    return SettlementFingerprint(
        scope=scope,
        period=period,
        line=line if family in {MarketFamily.TOTAL_GOALS, MarketFamily.ASIAN_HANDICAP} else None,
        push_possible=_family_push_possible(family, line),
        extra_time_included=extra_time,
        penalties_included=penalties,
        source_rule_version=str(payload.get("id", "")) or None,
    )


def _period_from_text(value: str) -> FootballPeriod:
    text = normalize_text(value)
    if any(token in text for token in ("first half", "1st half", "half time")):
        return FootballPeriod.FIRST_HALF
    if any(token in text for token in ("second half", "2nd half")):
        return FootballPeriod.SECOND_HALF
    if "extra time" in text:
        return FootballPeriod.EXTRA_TIME
    return FootballPeriod.FULL_TIME


def _line_from_payload_or_text(payload: dict[str, Any], text: str) -> Decimal | None:
    for key in ("line", "handicap", "points", "total", "strike"):
        value = payload.get(key)
        if value is not None:
            try:
                return Decimal(str(value))
            except InvalidOperation:
                pass
    match = _NUMBER.search(text)
    if match:
        try:
            return Decimal(match.group(1))
        except InvalidOperation:
            return None
    return None


def _family_push_possible(family: MarketFamily, line: Decimal | None) -> bool | None:
    if family is MarketFamily.DRAW_NO_BET:
        return True
    if family in {
        MarketFamily.MATCH_RESULT,
        MarketFamily.BOTH_TEAMS_TO_SCORE,
        MarketFamily.TO_QUALIFY,
        MarketFamily.FIRST_TEAM_TO_SCORE,
    }:
        return False
    if family in {MarketFamily.TOTAL_GOALS, MarketFamily.ASIAN_HANDICAP}:
        return line_push_possible(line)
    return line_push_possible(line)


_MARKET_DESCRIPTOR_SUFFIX = re.compile(
    r"(?:\s*[-:|]\s*|\s+)"
    r"(?:(?:1st|2nd|first|second)\s+half\s+)?"
    r"(?:more markets|player props|btts|both teams? to score|"
    r"total goals?|total corners?|totals?|first team to score|ftts|"
    r"exact score|correct score|halftime result|second half result|"
    r"half[- ]?time(?: result)?|moneyline|match result|match odds|"
    r"spread|asian handicap|draw no bet)\s*$",
    re.IGNORECASE,
)


def _strip_market_descriptor(part: str) -> str:
    """Remove trailing market-family labels from a fixture participant string.

    Providers often publish split events as ``Home vs Away: BTTS``. Those
    suffixes are not part of the team name and must not pollute identity.
    """

    current = part.strip(" -–—:|")
    previous = None
    while current != previous:
        previous = current
        current = _MARKET_DESCRIPTOR_SUFFIX.sub("", current).strip(" -–—:|")
    return current


def _split_fixture_title(title: str) -> tuple[str, str]:
    clean = title.strip()
    parts = [part.strip(" -") for part in _FIXTURE_SEPARATOR.split(clean) if part.strip(" -")]
    if len(parts) != 2:
        raise VenueNormalizationError(f"Cannot safely split football fixture title: {title}")
    home = _strip_market_descriptor(parts[0])
    away = _strip_market_descriptor(parts[1])
    if not home or not away:
        raise VenueNormalizationError(f"Cannot safely split football fixture title: {title}")
    return home, away


def _required_string(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if value is None or str(value).strip() == "":
        raise VenueNormalizationError(f"Required field missing: {key}")
    return str(value).strip()


def _first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _parse_datetime(value: Any) -> datetime:
    if not value:
        raise VenueNormalizationError("Football event has no start datetime")
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise VenueNormalizationError(f"Invalid event datetime: {value}") from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise VenueNormalizationError(
            "Football event start datetime is timezone-naive; refuse to assume UTC"
        )
    return parsed.astimezone(UTC)


def _list_field(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return []
        try:
            decoded = json.loads(stripped)
        except json.JSONDecodeError:
            return [item.strip() for item in stripped.split(",") if item.strip()]
        if isinstance(decoded, list):
            return decoded
    raise VenueNormalizationError("Expected list-like venue field")

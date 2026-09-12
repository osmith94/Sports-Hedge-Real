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
    if "team total" in text:
        return MarketFamily.TEAM_TOTAL, line
    if "total goal" in text or "over under" in text and "goal" in text:
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
    if text in {home, "home", "home team"}:
        return CanonicalOutcome.HOME
    if text in {away, "away", "away team"}:
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


def _split_fixture_title(title: str) -> tuple[str, str]:
    clean = title.strip()
    parts = [part.strip(" -") for part in _FIXTURE_SEPARATOR.split(clean) if part.strip(" -")]
    if len(parts) != 2:
        raise VenueNormalizationError(f"Cannot safely split football fixture title: {title}")
    return parts[0], parts[1]


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

from __future__ import annotations

import html
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
_NEGATION_PREFIXES = (
    "not",
    "no",
    "without",
    "excluding",
    "except",
    "never",
    "does not",
    "do not",
    "doesn t",
    "don t",
    "doesnt",
    "dont",
)
# Conjunction is required. Optional and/plus previously classified
# "including extra time penalties do not count" as ET+penalties.
_COMPOUND_INCLUDE_ET_PENALTIES = re.compile(
    r"including extra time(?: and| plus) penalties"
)
_COMPOUND_EXCLUDE_ET_PENALTIES = re.compile(
    r"(?:not including|excluding|without) extra time(?: and| plus) penalties"
    r"|extra time and penalties do not count"
    r"|extra time and penalties don t count"
    r"|extra time plus penalties do not count"
    r"|extra time plus penalties don t count"
)
_AMBIGUOUS_ET_PENALTIES = re.compile(r"including extra time penalties")
_RESULT_EXTENSION_RE = re.compile(
    r"\b(?:"
    r"shoot[- ]?outs?|"
    r"spot kicks?|"
    r"penalty shoot|"
    r"if (?:the )?(?:match|game) is tied|"
    r"if (?:the )?scores? (?:are )?level|"
    r"in the event of a (?:tie|draw)|"
    r"goes to extra time|"
    r"extra time applies|"
    r"penalties (?:to )?decide|"
    r"decided (?:on|by) penalties|"
    r"play(?:s|ed|ing)?(?: out)? to a (?:finish|result|conclusion|winner)|"
    r"play(?:s|ed|ing)? until (?:a |the |there is a )?(?:winner|result|finish)|"
    r"until (?:a |there is a )winner|"
    r"must (?:be|produce) a winner"
    r")\b"
)
# Token classes for result determination that can extend beyond ordinary
# regulation / normal time. These are semantic families, not a denylist of
# quoted O1 strings. Bare "so" is not a token: it is a common English word.
_RESULT_EXTENSION_TOKEN_SEQS = (
    ("golden", "goal"),
    ("golden", "goals"),
    ("silver", "goal"),
    ("silver", "goals"),
    ("sudden", "death"),
    ("sos",),
    ("s", "o"),
    ("s", "o", "s"),
    ("so", "count"),
    ("so", "counts"),
    ("so", "counted"),
    ("from", "the", "spot"),
    ("from", "the", "penalty", "spot"),
    ("spot", "kick"),
    ("spot", "kicks"),
    ("winner", "on", "the", "day"),
    ("winner", "on", "the", "night"),
    ("to", "a", "finish"),
    ("to", "a", "conclusion"),
    ("until", "a", "winner"),
    ("until", "there", "is", "a", "winner"),
)
_REMAIN_OPEN_POSTPONE_RE = re.compile(
    r"if the (?:game|match) is postponed.{0,160}remain open"
)
_STANDARD_CANCEL_NO_MAKEUP_RE = re.compile(
    r"if the (?:game|match) is cancel(?:l?ed|led) entirely.{0,200}"
    r"(?:no make[- ]?up|this market will resolve)"
)
_DRAW_VOID_RE = re.compile(r"\bdraw voids?\b")
_ET_INCLUSION_PHRASES = ("including extra time", "include extra time", "includes extra time")
_PENALTY_INCLUSION_PHRASES = ("including penalties", "include penalties", "includes penalties")
_TEAM_TOTAL_TOKENS = ("team total", "home total", "away total", "participant total")
_OVER_UNDER_ABBREV_RE = re.compile(r"\bo u\b")
_TEAM_GOALS_RE = re.compile(r"\bteam goals?\b")
_HOME_ROLE_RE = re.compile(r"\bhome(?:\s+team)?\b")
_AWAY_ROLE_RE = re.compile(r"\baway(?:\s+team)?\b")
_CALLED_OFF_RE = re.compile(r"\b(?:called off|call off)\b")
_REFUND_RE = re.compile(
    r"\b(?:refund(?:s|ed)?|stakes? returned|bets? (?:are )?(?:refunded|returned))\b"
)
_NEGATED_EXCLUSION_RE = re.compile(
    r"\b(?:not|never)\s+(?:excluding|exclude|without)\b"
    r"|\bdoes not exclude\b"
    r"|\bdo not exclude\b"
    r"|\bdoesn t exclude\b"
    r"|\bdon t exclude\b"
)
_EXTRA_TIME_TOKEN_SEQS = (
    ("extra", "time"),
    ("aet",),
    ("a", "e", "t"),
    ("et",),
    ("120", "minutes"),
    ("120", "minute"),
    ("120", "mins"),
    ("120", "min"),
)
_PENALTY_TOKEN_SEQS = (
    ("penalties",),
    ("penalty",),
    ("pks",),
    ("pk",),
    ("pens",),
    ("p", "k"),
    ("p", "k", "s"),
    ("penalty", "kicks"),
    ("penalty", "kick"),
)
_SINGLE_NEGATION_PREFIXES = frozenset(prefix for prefix in _NEGATION_PREFIXES if " " not in prefix)
_MULTI_NEGATION_PREFIXES = tuple(
    tuple(prefix.split()) for prefix in _NEGATION_PREFIXES if " " in prefix
)


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
        event_payload: dict[str, Any] | None = None,
    ) -> list[CanonicalMarket]:
        classified: list[_KalshiContract] = []
        for payload in payloads:
            classified.append(
                self._classify_contract(
                    event,
                    payload,
                    series=series,
                    event_payload=event_payload,
                )
            )

        grouped: dict[tuple[str, str, str], list[_KalshiContract]] = {}
        for item in classified:
            key = (item.family.value, item.period.value, "" if item.line is None else format(item.line, "f"))
            grouped.setdefault(key, []).append(item)

        assembled: list[CanonicalMarket] = []
        for items in grouped.values():
            assembled.append(self._assemble_group(event, items))
        return assembled

    def match_result_rule_enrichment_tickers(
        self,
        event: CanonicalEvent,
        payloads: list[dict[str, Any]],
        *,
        series: dict[str, Any] | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> tuple[list[str], list[str]]:
        """Ordinary Match Result tickers split by settlement completeness.

        Returns ``(incomplete, already_complete)``. Incomplete tickers are
        eligible for documented Get Market enrichment. Already-complete
        wording is skipped. GAME / Opta / series names are not used.
        """

        incomplete: list[str] = []
        complete: list[str] = []
        seen: set[str] = set()
        for payload in payloads:
            try:
                classified = self._classify_contract(
                    event,
                    payload,
                    series=series,
                    event_payload=event_payload,
                )
            except (VenueNormalizationError, ValueError):
                continue
            if classified.family is not MarketFamily.MATCH_RESULT:
                continue
            if classified.ticker in seen:
                continue
            seen.add(classified.ticker)
            if classified.settlement.is_economically_complete():
                complete.append(classified.ticker)
            else:
                incomplete.append(classified.ticker)
        return incomplete, complete

    def match_result_tickers_missing_contract_rules(
        self,
        event: CanonicalEvent,
        payloads: list[dict[str, Any]],
        *,
        series: dict[str, Any] | None = None,
        event_payload: dict[str, Any] | None = None,
    ) -> list[str]:
        """Ordinary Match Result tickers whose current wording is not complete.

        Fetches documented Get Market when nested/event rule text is missing or
        present-but-incomplete (e.g. ``Winner of the match.``). Does not fetch
        when current market/event wording already classifies to an economically
        complete settlement fingerprint. GAME / Opta / series names are not used.
        """

        incomplete, _complete = self.match_result_rule_enrichment_tickers(
            event,
            payloads,
            series=series,
            event_payload=event_payload,
        )
        return incomplete

    def _classify_contract(
        self,
        event: CanonicalEvent,
        payload: dict[str, Any],
        *,
        series: dict[str, Any] | None,
        event_payload: dict[str, Any] | None = None,
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
            event_payload=event_payload,
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
    from sports_hedge.application.target_competitions import resolve_target_competition_from_kalshi_ticker

    ticker = series_ticker.upper()
    # La Liga 2 is a distinct Kalshi family. The KXLALIGA prefix must not claim it.
    if ticker.startswith("KXLALIGA2"):
        return None
    resolved = resolve_target_competition_from_kalshi_ticker(series_ticker)
    return resolved.display_name if resolved is not None else None


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
        if _is_named_team_or_participant_total(
            f"{title} {yes_label}",
            home_team=home_team,
            away_team=away_team,
        ):
            raise VenueNormalizationError(
                "Kalshi team/participant totals are not inferred as match totals"
            )
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


def _prefix_negation_count(prefix: str) -> int:
    """Count a trailing chain of negation tokens. Stacked negations are not evaluated."""

    words = " ".join(prefix.split()).split()
    count = 0
    index = len(words)
    while index > 0:
        matched = False
        for seq in sorted(_MULTI_NEGATION_PREFIXES, key=len, reverse=True):
            width = len(seq)
            if index >= width and tuple(words[index - width : index]) == seq:
                count += 1
                index -= width
                matched = True
                break
        if matched:
            continue
        if words[index - 1] in _SINGLE_NEGATION_PREFIXES:
            count += 1
            index -= 1
            continue
        break
    return count


def _prefix_negates(prefix: str) -> bool:
    return _prefix_negation_count(prefix) >= 1


def _phrase_polarity(text: str, phrase: str) -> bool | None:
    """True if phrase is affirmed, False if negated, None if absent or conflicted."""

    affirmed = False
    negated = False
    ambiguous = False
    start = 0
    while True:
        pos = text.find(phrase, start)
        if pos < 0:
            break
        depth = _prefix_negation_count(text[:pos])
        if depth == 0:
            affirmed = True
        elif depth == 1:
            negated = True
        else:
            ambiguous = True
        start = pos + len(phrase)
    if ambiguous or (affirmed and negated):
        return None
    if affirmed:
        return True
    if negated:
        return False
    return None


def _exclusion_phrases(subject: str) -> tuple[str, ...]:
    return (
        f"{subject} do not count",
        f"{subject} does not count",
        f"{subject} don t count",
        f"{subject} doesnt count",
        f"{subject} is not included",
        f"{subject} are not included",
        f"not including {subject}",
        f"excluding {subject}",
        f"without {subject}",
        f"exclude {subject}",
        f"excludes {subject}",
    )


def _explicitly_excludes(text: str, subject: str) -> bool:
    return any(_has_unnegated_phrase(text, phrase) for phrase in _exclusion_phrases(subject))


def _has_unnegated_phrase(text: str, phrase: str) -> bool:
    start = 0
    while True:
        pos = text.find(phrase, start)
        if pos < 0:
            return False
        if _prefix_negation_count(text[:pos]) == 0:
            return True
        start = pos + len(phrase)


def _tokens_of(text: str) -> list[str]:
    return text.split() if text else []


def _has_token_seq(tokens: list[str], seq: tuple[str, ...]) -> bool:
    width = len(seq)
    return any(tuple(tokens[index : index + width]) == seq for index in range(len(tokens) - width + 1))


def _has_extra_time_token(text: str) -> bool:
    tokens = _tokens_of(text)
    return any(_has_token_seq(tokens, seq) for seq in _EXTRA_TIME_TOKEN_SEQS)


def _has_penalties_token(text: str) -> bool:
    tokens = _tokens_of(text)
    return any(_has_token_seq(tokens, seq) for seq in _PENALTY_TOKEN_SEQS)


def _negated_exclusion_ambiguous(text: str) -> bool:
    """True when an exclusion construction is itself negated (double-negation / polarity)."""

    if _NEGATED_EXCLUSION_RE.search(text) and (
        _has_extra_time_token(text) or _has_penalties_token(text)
    ):
        return True
    for subject in ("extra time", "penalties"):
        for phrase in _exclusion_phrases(subject):
            start = 0
            while True:
                pos = text.find(phrase, start)
                if pos < 0:
                    break
                if _prefix_negation_count(text[:pos]) >= 1:
                    return True
                start = pos + len(phrase)
    return False


def _inclusion_exclusion_conflict(text: str, extra_time: bool | None, penalties: bool | None) -> bool:
    """True when the same subject is both affirmatively included and explicitly excluded."""

    extra_excluded = _explicitly_excludes(text, "extra time")
    penalties_excluded = _explicitly_excludes(text, "penalties")
    if extra_excluded and (
        extra_time is True or any(_has_unnegated_phrase(text, phrase) for phrase in _ET_INCLUSION_PHRASES)
    ):
        return True
    if penalties_excluded and (
        penalties is True
        or any(_has_unnegated_phrase(text, phrase) for phrase in _PENALTY_INCLUSION_PHRASES)
    ):
        return True
    return False


def _has_regulation_marker(text: str) -> bool:
    return "90 minutes" in text or "regulation time" in text or "regulation-time" in text


def _unparsed_abandon_postpone_void(text: str) -> bool:
    """True when abandon/postpone/cancel/reschedule/void language is unproven.

    Understood clauses that do not fail closed:
    - standard Polymarket postponed / remain-open-until-completed
    - standard Polymarket canceled-entirely / no-makeup / resolve Yes or No
    - draw-void wording, which is family/push semantics rather than match void
    """

    remain_open = bool(_REMAIN_OPEN_POSTPONE_RE.search(text))
    standard_cancel = bool(_STANDARD_CANCEL_NO_MAKEUP_RE.search(text))
    has_abandon = "abandon" in text
    has_postpone = "postpon" in text
    has_reschedule = "reschedul" in text
    has_cancel = bool(re.search(r"\bcancel", text))
    remaining_void = _DRAW_VOID_RE.sub(" ", text)
    has_other_void = bool(re.search(r"\bvoid", remaining_void))
    if _CALLED_OFF_RE.search(text):
        return True
    if _REFUND_RE.search(text) and not (remain_open or standard_cancel):
        return True
    if has_abandon or has_reschedule:
        return True
    if has_postpone and not remain_open:
        return True
    if has_cancel and not standard_cancel:
        return True
    if has_other_void:
        return True
    return False


def _has_result_extension_language(text: str) -> bool:
    """True when wording indicates the result can be decided beyond regulation.

    Covers sudden-death / golden-goal / silver-goal, shootout abbreviations
    and synonyms, spot-kick deciders, and play-to-a-result / winner-on-the-day
    language. Presence is enough to refuse a regulation-time claim; it does
    not newly prove extra-time or penalties inclusion.
    """

    if _RESULT_EXTENSION_RE.search(text):
        return True
    tokens = _tokens_of(text)
    return any(_has_token_seq(tokens, seq) for seq in _RESULT_EXTENSION_TOKEN_SEQS)


def _unparsed_result_extension(text: str, extra_time: bool | None, penalties: bool | None) -> bool:
    if _has_result_extension_language(text):
        return True
    if extra_time is None and _has_extra_time_token(text):
        return True
    if penalties is None and _has_penalties_token(text):
        return True
    return False


def _claim_regulation(
    text: str, extra_time: bool | None, penalties: bool | None
) -> tuple[SettlementScope, bool | None, bool | None]:
    if extra_time is True or penalties is True:
        return SettlementScope.UNKNOWN, None, None
    if _unparsed_result_extension(text, extra_time, penalties):
        return SettlementScope.UNKNOWN, None, None
    return SettlementScope.REGULATION_TIME, False, False


def _first_defined_polarity(text: str, phrases: tuple[str, ...]) -> bool | None:
    for phrase in phrases:
        polarity = _phrase_polarity(text, phrase)
        if polarity is not None:
            return polarity
        if phrase in text:
            return None
    return None


def classify_settlement_wording(text: str) -> tuple[SettlementScope, bool | None, bool | None]:
    """Map rules text onto the fingerprint model, or UNKNOWN when incomplete.

    Fail closed: negation must not parse as inclusion, compound extra-time plus
    penalties must not collapse, and 90-minute markers must not hide unparsed
    extra-time/shootout/tie/void/abbreviation/refund/result-extension language.
    """

    normalized = normalize_text(text)
    if not normalized or normalized in {"[]"}:
        return SettlementScope.UNKNOWN, None, None
    if _unparsed_abandon_postpone_void(normalized):
        return SettlementScope.UNKNOWN, None, None
    if _negated_exclusion_ambiguous(normalized):
        return SettlementScope.UNKNOWN, None, None

    include_matches = list(_COMPOUND_INCLUDE_ET_PENALTIES.finditer(normalized))
    exclude_matches = list(_COMPOUND_EXCLUDE_ET_PENALTIES.finditer(normalized))
    include_depths = [_prefix_negation_count(normalized[:match.start()]) for match in include_matches]
    if any(depth >= 2 for depth in include_depths):
        return SettlementScope.UNKNOWN, None, None
    include_affirmed = any(depth == 0 for depth in include_depths)
    include_negated = bool(exclude_matches) or any(depth == 1 for depth in include_depths)
    if include_affirmed and include_negated:
        return SettlementScope.UNKNOWN, None, None
    if include_negated:
        return _claim_regulation(normalized, False, False)
    if include_affirmed:
        if _inclusion_exclusion_conflict(normalized, True, True):
            return SettlementScope.UNKNOWN, None, None
        return SettlementScope.INCLUDING_PENALTIES, True, True
    if _AMBIGUOUS_ET_PENALTIES.search(normalized):
        return SettlementScope.UNKNOWN, None, None

    extra_time = _first_defined_polarity(normalized, _ET_INCLUSION_PHRASES)
    penalties = _first_defined_polarity(normalized, _PENALTY_INCLUSION_PHRASES)
    if _inclusion_exclusion_conflict(normalized, extra_time, penalties):
        return SettlementScope.UNKNOWN, None, None
    if extra_time is None and _explicitly_excludes(normalized, "extra time"):
        extra_time = False
    if penalties is None and _explicitly_excludes(normalized, "penalties"):
        penalties = False

    if extra_time is True and penalties is True:
        return SettlementScope.INCLUDING_PENALTIES, True, True
    if extra_time is True and penalties is not True:
        if _has_regulation_marker(normalized):
            return SettlementScope.UNKNOWN, None, None
        return SettlementScope.INCLUDING_EXTRA_TIME, True, False
    if extra_time is False and penalties is True:
        return SettlementScope.UNKNOWN, None, None
    if extra_time is False:
        return _claim_regulation(normalized, extra_time, penalties)
    if penalties is True:
        # Positive penalties token cannot override contrary 90-minute / ET-exclusion.
        if extra_time is not True and _has_regulation_marker(normalized):
            return SettlementScope.UNKNOWN, None, None
        return SettlementScope.INCLUDING_PENALTIES, True, True
    if penalties is False and extra_time is None:
        if _has_regulation_marker(normalized):
            return _claim_regulation(normalized, extra_time, penalties)
        return SettlementScope.UNKNOWN, None, None
    if _has_regulation_marker(normalized):
        return _claim_regulation(normalized, extra_time, penalties)
    return SettlementScope.UNKNOWN, None, None


KALSHI_CONTRACT_RULE_KEYS = ("rules_primary", "rules_secondary", "rules")
KALSHI_GENERIC_RULE_PHRASES = (
    "winner of the match",
    "see contract url",
    "see contract terms",
    "see contract",
)
KALSHI_SCOPE_CATALOG_PHRASES = (
    "result scope",
    "possible result",
    "may be one of",
    "one of the following",
    "contract terms",
    "series contract",
    "for example",
    "example:",
    "examples:",
)
KALSHI_DOCUMENTED_SELECTOR_KEYS = (
    "strike_type",
    "custom_strike",
    "market_type",
    "settlement_source",
    "functional_strike",
    "primary_participant_key",
)
KALSHI_STRIKE_TYPE_VALUES = (
    "greater",
    "greater_or_equal",
    "less",
    "less_or_equal",
    "between",
    "functional",
    "custom",
    "structured",
)
KALSHI_MARKET_TYPE_VALUES = ("binary", "scalar")
KALSHI_DOCUMENTED_RESULT_SCOPES = (
    "first_half",
    "regulation_time",
    "second_half",
    "extra_time",
    "full_match",
)
KALSHI_RESULT_SCOPE_PLACEHOLDER_PHRASES = (
    "result scope",
    "<result scope>",
    "the applicable result scope",
    "may take one of",
    "one of the following",
)
KALSHI_PAYOUT_CRITERION_PHRASES = (
    "payout criterion",
    "this market resolves",
    "this contract shall resolve",
    "this contract pays",
    "resolves based on",
    "yes if and only if",
)
KALSHI_SCOPE_DEFINITION_LABELS = (
    "first half",
    "regulation time",
    "second half",
    "extra time",
    "full match",
)
KALSHI_ENTITY_STRIKE_KEY_HINTS = ("team", "player", "participant", "club", "athlete")
_NINETY_MINUTE_ABBREV_RE = re.compile(r"\b90\s*mins?\b")
_SAFE_ENUM_TOKEN_RE = re.compile(r"^[a-z0-9_]{1,40}$")
_SAFE_OBJECT_KEY_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
_UUID_VALUE_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
KALSHI_RULE_LAYER_NESTED = "nested_list"
KALSHI_RULE_LAYER_EVENT = "event"
KALSHI_RULE_LAYER_GET_MARKET = "get_market"
KALSHI_RULE_LAYER_SERIES = "series"
KALSHI_RULE_DIAGNOSTIC_CAP = 150


def _classified_fingerprint_complete(
    scope: SettlementScope, extra_time: bool | None, penalties: bool | None
) -> bool:
    return (
        scope is not SettlementScope.UNKNOWN
        and extra_time is not None
        and penalties is not None
    )


def kalshi_secondary_is_scope_catalog(text: str) -> bool:
    """True when secondary wording lists possible scopes/examples, not this market."""

    normalized = normalize_text(text)
    if not normalized:
        return False
    if any(phrase in normalized for phrase in KALSHI_SCOPE_CATALOG_PHRASES):
        return True
    scope, extra_time, penalties = classify_settlement_wording(normalized)
    if (
        _classified_fingerprint_complete(scope, extra_time, penalties)
        and scope in {SettlementScope.INCLUDING_EXTRA_TIME, SettlementScope.INCLUDING_PENALTIES}
        and _has_regulation_marker(normalized)
    ):
        # Template listing regulation and ET/penalties scopes together, not one market clause.
        return True
    return False


def resolve_kalshi_rule_field_precedence(
    primary: str,
    secondary: str,
    rules: str = "",
) -> tuple[SettlementScope, bool | None, bool | None]:
    """Diagnostic-only primary-vs-catalog comparison. Not a settlement fingerprint.

    Owner-live disproved using a complete primary over catalog secondary: live
    ``rules_primary`` is itself an unclassified multi-scope template. Settlement
    uses ``resolve_kalshi_settlement_from_rule_fields`` instead. This helper
    remains so forensics can show what the retired precedence path would have
    claimed. Unclassified primary is not guessed from secondary.
    """

    unknown = (SettlementScope.UNKNOWN, None, None)
    primary_text = str(primary or "").strip()
    secondary_text = str(secondary or "").strip()
    rules_text = str(rules or "").strip()
    primary_fp = classify_settlement_wording(primary_text) if primary_text else unknown
    secondary_fp = classify_settlement_wording(secondary_text) if secondary_text else unknown
    rules_fp = classify_settlement_wording(rules_text) if rules_text else unknown

    selected: tuple[SettlementScope, bool | None, bool | None] | None = None
    if _classified_fingerprint_complete(*primary_fp):
        selected = primary_fp
    elif not primary_text and _classified_fingerprint_complete(*rules_fp):
        selected = rules_fp
    if selected is None:
        if primary_text:
            return primary_fp
        if rules_text:
            return rules_fp
        return unknown

    def _conflicts(
        other_text: str,
        other_fp: tuple[SettlementScope, bool | None, bool | None],
    ) -> bool:
        if not other_text or not _classified_fingerprint_complete(*other_fp):
            return False
        if other_fp == selected:
            return False
        return not kalshi_secondary_is_scope_catalog(other_text)

    if _conflicts(secondary_text, secondary_fp):
        return unknown
    if selected == primary_fp and _conflicts(rules_text, rules_fp):
        return unknown
    return selected


def kalshi_rule_field_presence(payload: dict[str, Any] | None) -> dict[str, bool]:
    """SAFE booleans for documented rule fields. Never returns the wording."""

    if not isinstance(payload, dict):
        return {
            "rules_primary_nonempty": False,
            "rules_secondary_nonempty": False,
            "rules_nonempty": False,
            "any_rule_field_nonempty": False,
        }
    present = {
        key: bool(str(payload.get(key) or "").strip()) for key in KALSHI_CONTRACT_RULE_KEYS
    }
    return {
        "rules_primary_nonempty": present["rules_primary"],
        "rules_secondary_nonempty": present["rules_secondary"],
        "rules_nonempty": present["rules"],
        "any_rule_field_nonempty": any(present.values()),
    }


def _kalshi_rule_text(payload: dict[str, Any] | None) -> str:
    if not isinstance(payload, dict):
        return ""
    return " ".join(
        str(payload.get(key) or "").strip()
        for key in KALSHI_CONTRACT_RULE_KEYS
        if str(payload.get(key) or "").strip()
    )


def _wording_completeness(text: str) -> tuple[str, bool]:
    scope, extra_time, penalties = classify_settlement_wording(text)
    complete = _classified_fingerprint_complete(scope, extra_time, penalties)
    return scope.value, complete


def kalshi_documented_selector_presence(payload: dict[str, Any] | None) -> dict[str, bool]:
    """SAFE presence of documented non-name fields. Values are never returned."""

    if not isinstance(payload, dict):
        return {key: False for key in KALSHI_DOCUMENTED_SELECTOR_KEYS}
    present: dict[str, bool] = {}
    for key in KALSHI_DOCUMENTED_SELECTOR_KEYS:
        value = payload.get(key)
        present[key] = value is not None and str(value).strip() not in {"", "None"}
    return present


def _safe_short_enum(value: Any, allowed: tuple[str, ...]) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    lowered = text.casefold().replace(" ", "_").replace("-", "_")
    if lowered in allowed:
        return lowered
    compact = text.strip().casefold()
    if _SAFE_ENUM_TOKEN_RE.fullmatch(compact):
        return compact
    return "unrecognized"


def _safe_json_value_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int) and not isinstance(value, bool):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return "empty_string"
        if _UUID_VALUE_RE.fullmatch(stripped):
            return "uuid_string"
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list | tuple):
        return "array"
    return "other"


def _safe_object_keys(value: Any) -> list[str]:
    if not isinstance(value, dict):
        return []
    keys: list[str] = []
    for key in value:
        token = str(key).strip()
        if _SAFE_OBJECT_KEY_RE.fullmatch(token):
            keys.append(token)
    return sorted(keys)


def _custom_strike_is_entity_target(keys: list[str]) -> bool:
    for key in keys:
        lowered = key.casefold()
        if any(hint in lowered for hint in KALSHI_ENTITY_STRIKE_KEY_HINTS):
            return True
    return False


def kalshi_title_scope_selector_flags(text: str) -> dict[str, bool]:
    """SAFE semantic flags for subtitle/yes_sub_title. Never a fingerprint source."""

    normalized = normalize_text(text)
    return {
        "contains_explicit_regulation_selector": (
            "regulation time" in normalized or "90 minutes" in normalized
        ),
        "contains_explicit_extra_time_selector": "extra time" in normalized,
        "contains_explicit_full_match_selector": "full match" in normalized,
        "contains_explicit_first_half_selector": "first half" in normalized,
        "contains_explicit_second_half_selector": "second half" in normalized,
    }


def _product_metadata_value_class(key: str, value: Any) -> str:
    enum_value = _safe_short_enum(value, KALSHI_DOCUMENTED_RESULT_SCOPES) if isinstance(value, str) else None
    if enum_value in KALSHI_DOCUMENTED_RESULT_SCOPES:
        return f"documented_result_scope:{enum_value}"
    lowered_key = key.casefold()
    if lowered_key in {"competition", "league"}:
        return "competition_code"
    if "scope" in lowered_key or lowered_key in {"product", "family", "kind"}:
        token = _safe_short_enum(value, ("game", "soccer_game", "match"))
        if token in {"game", "soccer_game", "match"}:
            return "series_or_product_family_not_result_scope"
        return "non_result_scope_token"
    return _safe_json_value_type(value)


def classify_kalshi_rule_structure(text: str) -> dict[str, Any]:
    """SAFE structural shape of one rule field. Never returns wording."""

    stripped = str(text or "").strip()
    if not stripped:
        return {
            "contains_result_scope_placeholder": False,
            "contains_payout_criterion": False,
            "contains_multiple_scope_definitions": False,
            "looks_like_generic_contract_template": False,
            "clause_token_pattern": "empty",
        }
    normalized = normalize_text(stripped)
    placeholder = any(phrase in normalized for phrase in KALSHI_RESULT_SCOPE_PLACEHOLDER_PHRASES)
    catalog = kalshi_secondary_is_scope_catalog(stripped)
    payout = any(phrase in normalized for phrase in KALSHI_PAYOUT_CRITERION_PHRASES)
    scope_labels = [label for label in KALSHI_SCOPE_DEFINITION_LABELS if label in normalized]
    if placeholder or catalog:
        multiple_scopes = len(scope_labels) >= 2
    else:
        multiple_scopes = len(scope_labels) >= 3
    has_regulation = _has_regulation_marker(normalized)
    has_et = _has_extra_time_token(normalized)
    has_pen = _has_penalties_token(normalized)
    if has_regulation and has_et and has_pen:
        pattern = "mixed_regulation_et_penalties"
    elif has_regulation and not has_et and not has_pen:
        pattern = "regulation_only"
    elif has_et or has_pen:
        pattern = "extra_time_or_penalties"
    elif has_regulation:
        pattern = "regulation_with_contingency_tokens"
    else:
        pattern = "generic_no_scope_tokens"
    scope, extra_time, penalties = classify_settlement_wording(stripped)
    complete = _classified_fingerprint_complete(scope, extra_time, penalties)
    template = bool(
        catalog
        or placeholder
        or multiple_scopes
        or (not complete and has_regulation and has_et and has_pen)
    )
    return {
        "contains_result_scope_placeholder": placeholder,
        "contains_payout_criterion": payout,
        "contains_multiple_scope_definitions": multiple_scopes,
        "looks_like_generic_contract_template": template,
        "clause_token_pattern": pattern,
    }


def kalshi_rule_field_is_generic_scope_template(text: str) -> bool:
    """True when wording is a multi-scope catalog/template, not a selected clause."""

    return bool(
        str(text or "").strip()
        and classify_kalshi_rule_structure(text)["looks_like_generic_contract_template"]
    )


def kalshi_documented_structured_fields(payload: dict[str, Any] | None) -> dict[str, Any]:
    """SAFE Get Market / event structured shapes. No rule text, UUIDs, or titles."""

    empty = {
        "strike_type": None,
        "market_type": None,
        "custom_strike_present": False,
        "custom_strike_keys": [],
        "custom_strike_value_types": [],
        "custom_strike_entity_target": False,
        "custom_strike_selects_result_scope": False,
        "functional_strike_present": False,
        "functional_strike_value_type": "null",
        "functional_strike_shape": "absent",
        "primary_participant_key_present": False,
        "subtitle_scope_selectors": kalshi_title_scope_selector_flags(""),
        "yes_sub_title_scope_selectors": kalshi_title_scope_selector_flags(""),
        "product_metadata_keys": [],
        "product_metadata_value_classes": {},
        "milestone_detail_keys": [],
        "milestone_detail_value_types": {},
        "documented_result_scope_selector": "none",
    }
    if not isinstance(payload, dict):
        return empty
    custom = payload.get("custom_strike")
    custom_keys = _safe_object_keys(custom)
    custom_types = (
        sorted({_safe_json_value_type(custom[key]) for key in custom if str(key) in custom_keys})
        if isinstance(custom, dict)
        else []
    )
    functional = payload.get("functional_strike")
    functional_present = functional is not None and str(functional).strip() not in {"", "None"}
    functional_type = _safe_json_value_type(functional)
    functional_shape = "absent"
    if functional_present:
        if isinstance(functional, dict):
            functional_shape = "object"
        elif isinstance(functional, list | tuple):
            functional_shape = "array"
        elif isinstance(functional, str):
            stripped = functional.strip()
            if stripped[:1] in {"{", "["}:
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    functional_shape = "string"
                else:
                    functional_shape = "json_object" if isinstance(parsed, dict) else (
                        "json_array" if isinstance(parsed, list) else "json_other"
                    )
            else:
                functional_shape = "string"
        else:
            functional_shape = functional_type
    metadata = payload.get("product_metadata")
    metadata_keys = _safe_object_keys(metadata)
    metadata_classes = {
        key: _product_metadata_value_class(key, metadata[key])
        for key in metadata_keys
        if isinstance(metadata, dict)
    }
    milestone = payload.get("milestone") if isinstance(payload.get("milestone"), dict) else {}
    details = milestone.get("details") if isinstance(milestone, dict) else None
    detail_keys = _safe_object_keys(details)
    detail_types = {
        key: _safe_json_value_type(details[key])
        for key in detail_keys
        if isinstance(details, dict)
    }
    return {
        "strike_type": _safe_short_enum(payload.get("strike_type"), KALSHI_STRIKE_TYPE_VALUES),
        "market_type": _safe_short_enum(payload.get("market_type"), KALSHI_MARKET_TYPE_VALUES),
        "custom_strike_present": isinstance(custom, dict) and bool(custom),
        "custom_strike_keys": custom_keys,
        "custom_strike_value_types": custom_types,
        "custom_strike_entity_target": _custom_strike_is_entity_target(custom_keys),
        "custom_strike_selects_result_scope": False,
        "functional_strike_present": functional_present,
        "functional_strike_value_type": functional_type,
        "functional_strike_shape": functional_shape,
        "primary_participant_key_present": bool(str(payload.get("primary_participant_key") or "").strip()),
        "subtitle_scope_selectors": kalshi_title_scope_selector_flags(
            str(payload.get("subtitle") or "")
        ),
        "yes_sub_title_scope_selectors": kalshi_title_scope_selector_flags(
            str(payload.get("yes_sub_title") or payload.get("yes_subtitle") or "")
        ),
        "product_metadata_keys": metadata_keys,
        "product_metadata_value_classes": metadata_classes,
        "milestone_detail_keys": detail_keys,
        "milestone_detail_value_types": detail_types,
        "documented_result_scope_selector": "none",
    }


def kalshi_documented_result_scope_fingerprint(
    payload: dict[str, Any] | None,
) -> tuple[SettlementScope, bool | None, bool | None] | None:
    """Return a fingerprint only when a documented field selects SOCCERGAME scope.

    Official Get Market schema: ``strike_type`` is strike evaluation
    (greater/custom/structured/…), ``market_type`` is binary|scalar,
    ``custom_strike`` with ``strike_type=structured`` holds structured-target
    entity IDs, ``functional_strike`` maps expiration values to settlement
    values, and ``primary_participant_key`` has no settlement description.
    SOCCERGAMEWIN terms define ``<result scope>`` as first half / regulation
    time / second half / extra time / full match, specified by the Exchange on
    listed iterations — not as a Market API enum. Title/GAME/Opta/team
    identity are never used. No documented selector currently maps.
    """

    _ = payload
    return None


def resolve_kalshi_settlement_from_rule_fields(
    primary: str,
    secondary: str,
    rules: str = "",
) -> tuple[SettlementScope, bool | None, bool | None]:
    """Fail-closed fingerprint from market rule fields. No primary-over-catalog.

    A single economically complete non-template field still maps (historical
    nested ``rules_primary`` regulation). Generic multi-scope template text in
    any nonempty field keeps UNKNOWN. Distinct complete fingerprints fail
    closed. Combined mixed-token blobs stay UNKNOWN.
    """

    unknown = (SettlementScope.UNKNOWN, None, None)
    fields = (
        str(primary or "").strip(),
        str(secondary or "").strip(),
        str(rules or "").strip(),
    )
    nonempty = [text for text in fields if text]
    if not nonempty:
        return unknown
    if any(kalshi_rule_field_is_generic_scope_template(text) for text in nonempty):
        return unknown
    complete: list[tuple[SettlementScope, bool | None, bool | None]] = []
    for text in nonempty:
        fingerprint = classify_settlement_wording(text)
        if _classified_fingerprint_complete(*fingerprint):
            complete.append(fingerprint)
    unique = set(complete)
    if len(unique) > 1:
        return unknown
    if len(unique) == 1:
        return next(iter(unique))
    return classify_settlement_wording(" ".join(nonempty))


def classify_kalshi_rule_field(field: str, text: str) -> dict[str, Any]:
    """SAFE classification of one documented rule field. Never returns wording."""

    stripped = str(text or "").strip()
    structure = classify_kalshi_rule_structure(stripped)
    if not stripped:
        return {
            "field": field,
            "present_nonempty": False,
            "classified_scope": None,
            "economically_complete": False,
            "wording_kind": "empty",
            "has_regulation_tokens": False,
            "has_extra_time_tokens": False,
            "has_penalties_tokens": False,
            "has_ninety_minute_abbrev": False,
            **structure,
        }
    normalized = normalize_text(stripped)
    scope, extra_time, penalties = classify_settlement_wording(stripped)
    complete = _classified_fingerprint_complete(scope, extra_time, penalties)
    return {
        "field": field,
        "present_nonempty": True,
        "classified_scope": scope.value,
        "economically_complete": complete,
        "wording_kind": _kalshi_wording_kind(stripped, complete=complete),
        "has_regulation_tokens": _has_regulation_marker(normalized),
        "has_extra_time_tokens": _has_extra_time_token(normalized),
        "has_penalties_tokens": _has_penalties_token(normalized),
        "has_ninety_minute_abbrev": bool(_NINETY_MINUTE_ABBREV_RE.search(normalized)),
        **structure,
    }


def _kalshi_wording_kind(text: str, *, complete: bool) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return "empty"
    if complete:
        return "complete"
    normalized = normalize_text(stripped)
    has_settlement_tokens = (
        _has_regulation_marker(normalized)
        or _has_extra_time_token(normalized)
        or _has_penalties_token(normalized)
        or bool(_NINETY_MINUTE_ABBREV_RE.search(normalized))
    )
    if has_settlement_tokens:
        return "present_unclassified_with_settlement_tokens"
    if any(phrase in normalized for phrase in KALSHI_GENERIC_RULE_PHRASES):
        return "generic_ambiguous"
    return "present_unclassified"


def _kalshi_layer_field_diagnostics(payload: dict[str, Any] | None) -> dict[str, Any]:
    raw = payload if isinstance(payload, dict) else {}
    fields = [
        classify_kalshi_rule_field(key, str(raw.get(key) or ""))
        for key in KALSHI_CONTRACT_RULE_KEYS
    ]
    prec_scope, prec_et, prec_pen = resolve_kalshi_rule_field_precedence(
        str(raw.get("rules_primary") or ""),
        str(raw.get("rules_secondary") or ""),
        str(raw.get("rules") or ""),
    )
    prec_complete = _classified_fingerprint_complete(prec_scope, prec_et, prec_pen)
    fingerprint = resolve_kalshi_settlement_from_rule_fields(
        str(raw.get("rules_primary") or ""),
        str(raw.get("rules_secondary") or ""),
        str(raw.get("rules") or ""),
    )
    fingerprint_complete = _classified_fingerprint_complete(*fingerprint)
    nonempty = any(item["present_nonempty"] for item in fields)
    return {
        "fields": fields,
        "precedence_classified_scope": prec_scope.value if nonempty else None,
        "precedence_economically_complete": prec_complete if nonempty else False,
        "fingerprint_classified_scope": fingerprint[0].value if nonempty else None,
        "fingerprint_economically_complete": fingerprint_complete if nonempty else False,
        "fingerprint_uses_rule_precedence": False,
        "documented_selector_presence": kalshi_documented_selector_presence(
            payload if isinstance(payload, dict) else None
        ),
        "structured_fields": kalshi_documented_structured_fields(
            payload if isinstance(payload, dict) else None
        ),
    }


def classify_kalshi_contract_rule_layer(
    payload: dict[str, Any] | None,
    *,
    layer: str,
    fetch_status: str | None = None,
) -> dict[str, Any]:
    """Classify one documented rule layer without exposing contract text."""

    field_diag = _kalshi_layer_field_diagnostics(
        payload if isinstance(payload, dict) else None
    )
    if payload is None and layer != KALSHI_RULE_LAYER_GET_MARKET:
        return {
            "layer": layer,
            "rules_primary_nonempty": False,
            "rules_secondary_nonempty": False,
            "rules_nonempty": False,
            "any_rule_field_nonempty": False,
            "classified_scope": None,
            "economically_complete": None,
            "wording_kind": "absent",
            "has_regulation_tokens": False,
            "has_extra_time_tokens": False,
            "has_penalties_tokens": False,
            "has_ninety_minute_abbrev": False,
            "fetch_status": fetch_status,
            **field_diag,
        }
    presence = kalshi_rule_field_presence(payload if isinstance(payload, dict) else None)
    text = _kalshi_rule_text(payload if isinstance(payload, dict) else None)
    normalized = normalize_text(text) if text else ""
    if not text:
        scope = None
        complete = False
        kind = "empty" if isinstance(payload, dict) else "absent"
        if fetch_status and str(fetch_status).startswith("not_called"):
            kind = "not_called"
        elif fetch_status in {"transport_failed", "empty_payload"}:
            kind = "absent"
    else:
        scope, complete = _wording_completeness(text)
        kind = _kalshi_wording_kind(text, complete=complete)
    return {
        "layer": layer,
        **presence,
        "classified_scope": scope,
        "economically_complete": complete if text else False,
        "wording_kind": kind,
        "has_regulation_tokens": bool(normalized) and _has_regulation_marker(normalized),
        "has_extra_time_tokens": bool(normalized) and _has_extra_time_token(normalized),
        "has_penalties_tokens": bool(normalized) and _has_penalties_token(normalized),
        "has_ninety_minute_abbrev": bool(normalized)
        and bool(_NINETY_MINUTE_ABBREV_RE.search(normalized)),
        "fetch_status": fetch_status,
        **field_diag,
    }


def classify_kalshi_series_rule_layer(series: dict[str, Any] | None) -> dict[str, Any]:
    """SAFE series-layer metadata. Contract-terms PDF body is never returned."""

    empty_family = {
        "family_id": None,
        "official_product_name_kind": None,
        "defines_default_result_scope": False,
        "default_result_scope": None,
        "default_applies_to_match_result": False,
        "match_result_default_scope": "none",
        "placeholder_specified_by_exchange": False,
        "listed_result_scopes": [],
        "verified": None,
        "fetch_status": None,
        "catalog_version": None,
        "filename": None,
        "sha256_prefix": None,
        "rulebook": None,
        "url_allowlisted": False,
    }
    if not isinstance(series, dict):
        return {
            "layer": KALSHI_RULE_LAYER_SERIES,
            "rules_primary_nonempty": False,
            "rules_secondary_nonempty": False,
            "rules_nonempty": False,
            "any_rule_field_nonempty": False,
            "classified_scope": None,
            "economically_complete": False,
            "wording_kind": "absent",
            "has_regulation_tokens": False,
            "has_extra_time_tokens": False,
            "has_penalties_tokens": False,
            "has_ninety_minute_abbrev": False,
            "fetch_status": None,
            "contract_terms_url_present": False,
            "settlement_sources_present": False,
            "contract_family": empty_family,
        }
    url = str(series.get("contract_terms_url") or "").strip()
    sources = series.get("settlement_sources")
    sources_present = bool(sources)
    raw_family = series.get("contract_family")
    if not isinstance(raw_family, dict):
        from sports_hedge.normalization.kalshi_contract_terms import lookup_kalshi_contract_family

        raw_family = lookup_kalshi_contract_family(url=url)
    family = {**empty_family}
    for key in empty_family:
        if key in raw_family:
            family[key] = raw_family[key]
    return {
        "layer": KALSHI_RULE_LAYER_SERIES,
        "rules_primary_nonempty": False,
        "rules_secondary_nonempty": False,
        "rules_nonempty": False,
        "any_rule_field_nonempty": False,
        "classified_scope": None,
        "economically_complete": False,
        "wording_kind": "catalog" if url else "absent",
        "has_regulation_tokens": False,
        "has_extra_time_tokens": False,
        "has_penalties_tokens": False,
        "has_ninety_minute_abbrev": False,
        "fetch_status": family.get("fetch_status"),
        "contract_terms_url_present": bool(url),
        "settlement_sources_present": sources_present,
        "contract_family": family,
    }


def safe_kalshi_match_result_rule_layers(
    *,
    nested: dict[str, Any] | None,
    event_payload: dict[str, Any] | None,
    series: dict[str, Any] | None,
    get_market_payload: dict[str, Any] | None,
    get_market_status: str | None,
) -> list[dict[str, Any]]:
    """Per-source-layer SAFE classification for one Match Result ticker."""

    return [
        classify_kalshi_contract_rule_layer(nested, layer=KALSHI_RULE_LAYER_NESTED),
        classify_kalshi_contract_rule_layer(event_payload, layer=KALSHI_RULE_LAYER_EVENT),
        classify_kalshi_contract_rule_layer(
            get_market_payload,
            layer=KALSHI_RULE_LAYER_GET_MARKET,
            fetch_status=get_market_status,
        ),
        classify_kalshi_series_rule_layer(series),
    ]


def merge_kalshi_contract_rules(target: dict[str, Any], source: dict[str, Any] | None) -> bool:
    """Prefer non-empty documented Get Market rule fields for the same ticker.

    Enrichment is only invoked when current nested/event wording is missing or
    incomplete. Non-empty Get Market ``rules_primary`` / ``rules_secondary`` /
    ``rules`` replace generic list text such as ``Winner of the match.``.
    Empty Get Market fields are not written. Titles and prices are never copied.
    """

    if not isinstance(source, dict):
        return False
    applied = False
    for key in KALSHI_CONTRACT_RULE_KEYS:
        value = str(source.get(key) or "").strip()
        if not value:
            continue
        current = str(target.get(key) or "").strip()
        if current == value:
            continue
        target[key] = value
        applied = True
    return applied


def _kalshi_settlement(
    payload: dict[str, Any],
    *,
    series: dict[str, Any] | None,
    family: MarketFamily,
    period: FootballPeriod,
    line: Decimal | None,
    event_payload: dict[str, Any] | None = None,
) -> SettlementFingerprint:
    # Market-specific rule fields only. Do not infer regulation from GAME / Opta
    # / series names / custom_strike entity IDs / title. Do not concatenate
    # series contract-terms catalogs into the market wording blob. Event-level
    # rules are inherited only for ordinary Match Result when the nested market
    # itself has no rule or description text. Primary-over-catalog precedence is
    # not a fingerprint source. Official Get Market structured fields do not
    # select SOCCERGAME <result scope>. Series contract-family defaults apply
    # only when the catalog records an unambiguous default for Match Result;
    # SOCCERGAMEWIN does not.
    primary = str(payload.get("rules_primary") or "").strip()
    secondary = str(payload.get("rules_secondary") or "").strip()
    rules = str(payload.get("rules") or "").strip()
    descriptions = [
        str(payload.get(key) or "").strip()
        for key in ("description", "yes_description", "no_description", "settlement_source")
        if str(payload.get(key) or "").strip()
    ]
    market_has_rule_text = bool(primary or secondary or rules or descriptions)
    if (
        not market_has_rule_text
        and family is MarketFamily.MATCH_RESULT
        and isinstance(event_payload, dict)
    ):
        primary = str(event_payload.get("rules_primary") or "").strip()
        secondary = str(event_payload.get("rules_secondary") or "").strip()
        rules = str(event_payload.get("rules") or "").strip()
    selector = kalshi_documented_result_scope_fingerprint(payload)
    if selector is not None:
        scope, extra_time, penalties = selector
    elif primary or secondary or rules:
        scope, extra_time, penalties = resolve_kalshi_settlement_from_rule_fields(
            primary, secondary, rules
        )
    else:
        scope, extra_time, penalties = classify_settlement_wording(" ".join(descriptions))
    if family is MarketFamily.MATCH_RESULT and not _classified_fingerprint_complete(
        scope, extra_time, penalties
    ):
        from sports_hedge.normalization.kalshi_contract_terms import (
            kalshi_apply_match_result_family_default,
            lookup_kalshi_contract_family,
        )

        family_meta = series.get("contract_family") if isinstance(series, dict) else None
        if not isinstance(family_meta, dict) and isinstance(series, dict):
            family_meta = lookup_kalshi_contract_family(
                url=str(series.get("contract_terms_url") or "")
            )
        default_scope = kalshi_apply_match_result_family_default(
            family_meta if isinstance(family_meta, dict) else None
        )
        if default_scope == "regulation_time":
            scope, extra_time, penalties = SettlementScope.REGULATION_TIME, False, False
        elif default_scope == "including_extra_time":
            scope, extra_time, penalties = SettlementScope.INCLUDING_EXTRA_TIME, True, False
        elif default_scope == "including_penalties":
            scope, extra_time, penalties = SettlementScope.INCLUDING_PENALTIES, True, True
    unknown_reason = None
    if (
        family is MarketFamily.MATCH_RESULT
        and scope is SettlementScope.UNKNOWN
        and not _classified_fingerprint_complete(scope, extra_time, penalties)
    ):
        from sports_hedge.normalization.kalshi_contract_terms import (
            GAMEWIN_SCOPE_UNAVAILABLE_REASON,
            kalshi_gamewin_result_scope_unavailable,
            lookup_kalshi_contract_family,
        )

        family_meta = series.get("contract_family") if isinstance(series, dict) else None
        if not isinstance(family_meta, dict) and isinstance(series, dict):
            family_meta = lookup_kalshi_contract_family(
                url=str(series.get("contract_terms_url") or "")
            )
        if kalshi_gamewin_result_scope_unavailable(
            family_meta if isinstance(family_meta, dict) else None
        ):
            unknown_reason = GAMEWIN_SCOPE_UNAVAILABLE_REASON
    return SettlementFingerprint(
        scope=scope,
        period=period,
        line=line if family in {MarketFamily.TOTAL_GOALS, MarketFamily.ASIAN_HANDICAP} else None,
        push_possible=_family_push_possible(family, line),
        extra_time_included=extra_time,
        penalties_included=penalties,
        source_rule_version=str(_first(payload, "ticker", "market_ticker") or "") or None,
        unknown_reason=unknown_reason,
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
    if any(token in text for token in _TEAM_TOTAL_TOKENS):
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
    if _looks_like_totals_surface(text) and _is_named_team_or_participant_total(
        text,
        home_team=home_team,
        away_team=away_team,
    ):
        return MarketFamily.TEAM_TOTAL, line
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
    if "total goal" in combined or ("over under" in combined and "goal" in combined):
        extra = normalize_text(str(_first(payload, "groupItemTitle", "group_item_title") or ""))
        scoped = f"{combined} {extra}".strip()
        if _is_named_team_or_participant_total(
            scoped,
            home_team=home_team,
            away_team=away_team,
        ):
            return MarketFamily.TEAM_TOTAL, line
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


def _has_exactly_one_role_side(text: str) -> bool:
    home_role = bool(_HOME_ROLE_RE.search(text))
    away_role = bool(_AWAY_ROLE_RE.search(text))
    return home_role != away_role


def _looks_like_totals_surface(text: str) -> bool:
    """Totals-like wording used only to fail closed into TEAM_TOTAL, never to invent match totals."""

    if _matchbook_looks_like_match_total(text):
        return True
    if _TEAM_GOALS_RE.search(text):
        return True
    if _OVER_UNDER_ABBREV_RE.search(text) and (
        "goal" in text or "team" in text or _has_exactly_one_role_side(text)
    ):
        return True
    if ("over" in text or "under" in text) and (
        "goal" in text or "team" in text or _has_exactly_one_role_side(text)
    ):
        return True
    return False


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


def _is_named_team_or_participant_total(
    text: str,
    *,
    home_team: str,
    away_team: str,
) -> bool:
    """True when a totals label is team-scoped, not a proven full-match total."""

    normalized = normalize_text(text)
    if any(token in normalized for token in _TEAM_TOTAL_TOKENS):
        return True
    if _TEAM_GOALS_RE.search(normalized):
        return True
    if _has_exactly_one_role_side(normalized):
        return True
    return _text_mentions_exactly_one_team(
        normalized,
        home_team=home_team,
        away_team=away_team,
    )


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
    if _is_named_team_or_participant_total(
        text,
        home_team=home_team,
        away_team=away_team,
    ):
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
    scope, extra_time, penalties = classify_settlement_wording(rules_text)
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
# Proven Kalshi GAME event decoration only. Do not strip Extra Time, Penalties,
# Women, U21, or arbitrary colon suffixes — those remain fail-closed identity.
_EVENT_SETTLEMENT_DECORATION_SUFFIX = re.compile(
    r"(?:\s*[:|]\s*|\s+[–—-]\s+)regulation(?:[-\s]+time)?\s*$",
    re.IGNORECASE,
)


def _title_for_participant_parse(title: str) -> str:
    """Decode entities and drop proven terminal settlement decoration.

    Raw provider titles stay on the payload. Regulation-time meaning for the
    settlement/equivalence layer remains on market rules and labels.
    """

    clean = html.unescape(title).strip()
    return _EVENT_SETTLEMENT_DECORATION_SUFFIX.sub("", clean).strip()


def _strip_market_descriptor(part: str) -> str:
    """Remove trailing market-family labels from a fixture participant string.

    Providers often publish split events as ``Home vs Away: BTTS``. Those
    suffixes are not part of the team name and must not pollute identity.
    """

    current = _EVENT_SETTLEMENT_DECORATION_SUFFIX.sub("", part).strip(" -–—:|")
    previous = None
    while current != previous:
        previous = current
        current = _MARKET_DESCRIPTOR_SUFFIX.sub("", current).strip(" -–—:|")
        current = _EVENT_SETTLEMENT_DECORATION_SUFFIX.sub("", current).strip(" -–—:|")
    return current


def _split_fixture_title(title: str) -> tuple[str, str]:
    clean = _title_for_participant_parse(title)
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

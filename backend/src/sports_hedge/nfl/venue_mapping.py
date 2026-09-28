"""Deterministic NFL venue-native truth table (Issue #590).

UNIVERSE asks: does this exact provider-native market shape belong to one of
the three owner-approved NFL canonical families? Known convention maps;
unknown or generic shapes fail closed. Never infer TOTAL_POINTS, POINT_SPREAD,
or GAME_WINNER from a broad Matchbook ``total`` / ``handicap`` / ``money_line``
type alone.

Soccer catalogue rows are not consumed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn

from sports_hedge.domain.football import MarketFamily
from sports_hedge.nfl.constants import (
    NFL_KALSHI_GAME_SERIES,
    NFL_KALSHI_SPREAD_SERIES,
    NFL_KALSHI_TOTAL_SERIES,
)
from sports_hedge.nfl.detect import approved_kalshi_nfl_series, rejected_kalshi_nfl_series
from sports_hedge.normalization.text import normalize_text

NFL_VENUE_MAPPING_VERSION = "nfl-venue-truth-v1"
NFL_VENUE_MAPPING_ISSUE = 590

MATCHBOOK_GAME_WINNER_NAMES = frozenset({"moneyline", "money line", "game winner"})
MATCHBOOK_GAME_WINNER_TYPES = frozenset({"money line", "moneyline"})
MATCHBOOK_SPREAD_NAMES = frozenset({"handicap", "point spread", "spread"})
MATCHBOOK_SPREAD_TYPES = frozenset({"handicap"})
MATCHBOOK_TOTAL_POINTS_NAMES = frozenset({"total", "total points"})
MATCHBOOK_TOTAL_POINTS_TYPES = frozenset({"total"})

POLYMARKET_GAME_WINNER_TYPES = frozenset({"moneyline"})
POLYMARKET_SPREAD_TYPES = frozenset({"spreads", "spread"})
POLYMARKET_TOTAL_POINTS_TYPES = frozenset({"totals", "total"})


@dataclass(frozen=True)
class NflVenueMappingHit:
    """One positive provider-native convention mapped to an approved family."""

    venue: str
    family: MarketFamily
    native_archetype: str
    mapping_version: str = NFL_VENUE_MAPPING_VERSION


def classify_matchbook_nfl_market(payload: dict[str, Any] | None) -> NflVenueMappingHit | None:
    """Exact Matchbook name + type, not generic ``market-type`` alone."""

    if not isinstance(payload, dict):
        return None
    name = normalize_text(str(payload.get("name") or ""))
    market_type = normalize_text(str(payload.get("market-type") or payload.get("market_type") or ""))
    if name in MATCHBOOK_GAME_WINNER_NAMES and market_type in MATCHBOOK_GAME_WINNER_TYPES:
        return NflVenueMappingHit(
            venue="matchbook",
            family=MarketFamily.GAME_WINNER,
            native_archetype="matchbook_moneyline_full_game",
        )
    if name in MATCHBOOK_SPREAD_NAMES and market_type in MATCHBOOK_SPREAD_TYPES:
        return NflVenueMappingHit(
            venue="matchbook",
            family=MarketFamily.POINT_SPREAD,
            native_archetype="matchbook_handicap_full_game",
        )
    if name in MATCHBOOK_TOTAL_POINTS_NAMES and market_type in MATCHBOOK_TOTAL_POINTS_TYPES:
        return NflVenueMappingHit(
            venue="matchbook",
            family=MarketFamily.TOTAL_POINTS,
            native_archetype="matchbook_total_points_full_game",
        )
    return None


def classify_kalshi_nfl_market(
    payload: dict[str, Any] | None,
    *,
    series_ticker: str | None = None,
) -> NflVenueMappingHit | None:
    """Approved Kalshi series head only. Unknown KXNFL* series stay unsupported."""

    if not isinstance(payload, dict) and not series_ticker:
        return None
    ticker = str(
        series_ticker
        or (payload or {}).get("series_ticker")
        or (payload or {}).get("event_ticker")
        or (payload or {}).get("ticker")
        or ""
    )
    if rejected_kalshi_nfl_series(ticker):
        return None
    head = approved_kalshi_nfl_series(ticker)
    if head == NFL_KALSHI_GAME_SERIES:
        return NflVenueMappingHit(
            venue="kalshi",
            family=MarketFamily.GAME_WINNER,
            native_archetype="kalshi_kxnflgame",
        )
    if head == NFL_KALSHI_SPREAD_SERIES:
        return NflVenueMappingHit(
            venue="kalshi",
            family=MarketFamily.POINT_SPREAD,
            native_archetype="kalshi_kxnflspread",
        )
    if head == NFL_KALSHI_TOTAL_SERIES:
        return NflVenueMappingHit(
            venue="kalshi",
            family=MarketFamily.TOTAL_POINTS,
            native_archetype="kalshi_kxnfltotal",
        )
    return None


def _payload_first(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = payload.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def classify_polymarket_nfl_market(payload: dict[str, Any] | None) -> NflVenueMappingHit | None:
    """Structured Gamma ``sportsMarketType`` plus evidenced full-game shape."""

    if not isinstance(payload, dict):
        return None
    sports_type = normalize_text(
        str(_payload_first(payload, "sportsMarketType", "sports_market_type") or "")
    )
    question = normalize_text(
        str(_payload_first(payload, "question", "title", "groupItemTitle") or "")
    )
    slug = normalize_text(str(payload.get("slug") or ""))
    group_title = normalize_text(
        str(_payload_first(payload, "groupItemTitle", "group_item_title") or "")
    )
    if sports_type in POLYMARKET_GAME_WINNER_TYPES:
        return NflVenueMappingHit(
            venue="polymarket",
            family=MarketFamily.GAME_WINNER,
            native_archetype="polymarket_moneyline_full_game",
        )
    if sports_type in POLYMARKET_SPREAD_TYPES and (
        question.startswith("spread") or " spread " in f" {slug} " or "spread" in slug
    ):
        return NflVenueMappingHit(
            venue="polymarket",
            family=MarketFamily.POINT_SPREAD,
            native_archetype="polymarket_spreads_full_game",
        )
    if sports_type in POLYMARKET_TOTAL_POINTS_TYPES and _polymarket_game_total_shape(
        question=question, slug=slug, group_title=group_title
    ):
        return NflVenueMappingHit(
            venue="polymarket",
            family=MarketFamily.TOTAL_POINTS,
            native_archetype="polymarket_totals_full_game",
        )
    return None


def _polymarket_game_total_shape(*, question: str, slug: str, group_title: str) -> bool:
    """Game O/U / total-points slug. Player/team/period totals do not match."""

    combined = f" {question} {slug} {group_title} "
    unsupported = (
        "receiving",
        "rushing",
        "passing",
        "reception",
        "player",
        "team total",
        "quarter",
        "1st half",
        "2nd half",
        "first half",
        "second half",
    )
    if any(token in combined for token in unsupported):
        return False
    if " o u " in combined:
        return True
    if "total points" in combined:
        return True
    # Captured game-total slugs look like ``nfl-ind-kc-2026-09-21-total-47pt5``.
    if " total " in f" {slug} ":
        return True
    return False


def require_matchbook_nfl_mapping(payload: dict[str, Any]) -> NflVenueMappingHit:
    hit = classify_matchbook_nfl_market(payload)
    if hit is None:
        name = str(payload.get("name") or "").strip() or str(payload.get("id") or "unknown")
        raise_unsupported_matchbook(name)
    return hit


def raise_unsupported_matchbook(name: str) -> NoReturn:
    from sports_hedge.normalization.venues import VenueNormalizationError

    raise VenueNormalizationError(
        f"unsupported Matchbook NFL market: {name} "
        f"(not in {NFL_VENUE_MAPPING_VERSION} truth table)"
    )


def require_polymarket_nfl_mapping(payload: dict[str, Any]) -> NflVenueMappingHit:
    hit = classify_polymarket_nfl_market(payload)
    if hit is None:
        from sports_hedge.normalization.venues import VenueNormalizationError

        question = str(_payload_first(payload, "question", "title") or "")
        sports_type = str(_payload_first(payload, "sportsMarketType", "sports_market_type") or "")
        raise VenueNormalizationError(
            f"unsupported Polymarket NFL market type: {sports_type or question}"
        )
    return hit


def require_kalshi_nfl_mapping(
    payload: dict[str, Any] | None,
    *,
    series_ticker: str | None = None,
) -> NflVenueMappingHit:
    hit = classify_kalshi_nfl_market(payload, series_ticker=series_ticker)
    if hit is None:
        from sports_hedge.normalization.venues import VenueNormalizationError

        ticker = series_ticker or str((payload or {}).get("series_ticker") or "")
        raise VenueNormalizationError(f"unsupported NFL Kalshi series: {ticker}")
    return hit


def matchbook_payload_matches_nfl_family(
    payload: dict[str, Any],
    family: MarketFamily,
) -> bool:
    hit = classify_matchbook_nfl_market(payload)
    return hit is not None and hit.family is family

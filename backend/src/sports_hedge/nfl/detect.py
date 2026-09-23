"""NFL payload/sport detection. Soccer recognisers must not consume these."""

from __future__ import annotations

from typing import Any

from sports_hedge.domain.football import CanonicalEvent, MarketFamily
from sports_hedge.nfl.constants import (
    NFL_KALSHI_APPROVED_SERIES,
    NFL_SPORT,
    POLYMARKET_NFL_SERIES_ID,
    POLYMARKET_NFL_SPORT,
)
from sports_hedge.normalization.text import normalize_text

NFL_MARKET_FAMILIES: frozenset[MarketFamily] = frozenset(
    {
        MarketFamily.GAME_WINNER,
        MarketFamily.POINT_SPREAD,
        MarketFamily.TOTAL_POINTS,
    }
)

_NFL_KALSHI_PREFIX = "KXNFL"
_REJECTED_KALSHI_HEADS = (
    "KXNFLTEAMTOTAL",
    "KXNFL1H",
    "KXNFL2H",
    "KXNFL1Q",
    "KXNFL2Q",
    "KXNFL3Q",
    "KXNFL4Q",
    "KXNFLOT",
    "KXNFLGAMEFG",
    "KXNFLWINS",
)


def kalshi_series_head(ticker: str | None) -> str:
    return str(ticker or "").strip().upper().split("-", 1)[0]


def is_nfl_kalshi_ticker(ticker: str | None) -> bool:
    return kalshi_series_head(ticker).startswith(_NFL_KALSHI_PREFIX)


def approved_kalshi_nfl_series(ticker: str | None) -> str | None:
    head = kalshi_series_head(ticker)
    if head in NFL_KALSHI_APPROVED_SERIES:
        return head
    return None


def rejected_kalshi_nfl_series(ticker: str | None) -> bool:
    head = kalshi_series_head(ticker)
    if not head.startswith(_NFL_KALSHI_PREFIX):
        return False
    if head in NFL_KALSHI_APPROVED_SERIES:
        return False
    return True


def is_nfl_canonical_event(event: CanonicalEvent | None) -> bool:
    if event is None:
        return False
    return event.sport == NFL_SPORT


def is_nfl_market_family(family: MarketFamily | str | None) -> bool:
    if family is None:
        return False
    value = family if isinstance(family, MarketFamily) else MarketFamily(str(family))
    return value in NFL_MARKET_FAMILIES


def is_nfl_payload(payload: dict[str, Any] | None) -> bool:
    """True when a raw venue payload is NFL / American Football."""

    if not isinstance(payload, dict):
        return False
    if is_nfl_kalshi_ticker(
        str(payload.get("series_ticker") or payload.get("event_ticker") or payload.get("ticker") or "")
    ):
        return True
    sport = payload.get("sport")
    if isinstance(sport, dict):
        sport_code = normalize_text(str(sport.get("sport") or sport.get("name") or ""))
        series = str(sport.get("series") or "").strip()
        if sport_code in {POLYMARKET_NFL_SPORT, "american football", "nfl"}:
            return True
        if series == POLYMARKET_NFL_SERIES_ID:
            return True
    if str(payload.get("series_id") or payload.get("seriesId") or "").strip() == POLYMARKET_NFL_SERIES_ID:
        return True
    slug = normalize_text(str(payload.get("slug") or payload.get("ticker") or payload.get("seriesSlug") or ""))
    if slug.startswith("nfl "):
        return True
    sport_id = payload.get("sport-id") or payload.get("sport_id")
    if str(sport_id).strip() == "1":
        meta = payload.get("meta-tags") or payload.get("meta_tags") or []
        if isinstance(meta, list):
            for tag in meta:
                if not isinstance(tag, dict):
                    continue
                name = normalize_text(str(tag.get("name") or ""))
                tag_type = normalize_text(str(tag.get("type") or ""))
                if name == "american football" and tag_type == "sport":
                    return True
                if name == "nfl" and tag_type in {"competition", "league"}:
                    return True
        sport_name = normalize_text(str(payload.get("sport-name") or payload.get("sport_name") or payload.get("sport") or ""))
        if sport_name in {"american football", "nfl"}:
            return True
    return False

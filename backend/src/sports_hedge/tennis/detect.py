"""Tennis payload detection. Soccer recognisers must not consume these."""

from __future__ import annotations

from typing import Any

from sports_hedge.domain.football import CanonicalEvent, MarketFamily
from sports_hedge.normalization.text import normalize_text
from sports_hedge.tennis.constants import (
    POLYMARKET_ATP_DOUBLES_SERIES_ID,
    POLYMARKET_ATP_SERIES_ID,
    POLYMARKET_ATP_SPORT,
    POLYMARKET_ITF_SERIES_ID,
    POLYMARKET_WTA_DOUBLES_SERIES_ID,
    POLYMARKET_WTA_SERIES_ID,
    POLYMARKET_WTA_SPORT,
    TENNIS_KALSHI_APPROVED_SERIES,
    TENNIS_SPORT,
)

TENNIS_MARKET_FAMILIES: frozenset[MarketFamily] = frozenset({MarketFamily.GAME_WINNER})

_POLYMARKET_TENNIS_SPORTS = frozenset(
    {
        POLYMARKET_ATP_SPORT,
        POLYMARKET_WTA_SPORT,
        "atp-doubles",
        "wta-doubles",
        "itf",
    }
)
_POLYMARKET_TENNIS_SERIES = frozenset(
    {
        POLYMARKET_ATP_SERIES_ID,
        POLYMARKET_WTA_SERIES_ID,
        POLYMARKET_ATP_DOUBLES_SERIES_ID,
        POLYMARKET_WTA_DOUBLES_SERIES_ID,
        POLYMARKET_ITF_SERIES_ID,
    }
)


def kalshi_series_head(ticker: str | None) -> str:
    return str(ticker or "").strip().upper().split("-", 1)[0]


def is_tennis_kalshi_ticker(ticker: str | None) -> bool:
    head = kalshi_series_head(ticker)
    return head.startswith("KXATP") or head.startswith("KXWTA")


def approved_kalshi_tennis_series(ticker: str | None) -> str | None:
    head = kalshi_series_head(ticker)
    if head in TENNIS_KALSHI_APPROVED_SERIES:
        return head
    return None


def rejected_kalshi_tennis_series(ticker: str | None) -> bool:
    head = kalshi_series_head(ticker)
    if not is_tennis_kalshi_ticker(head):
        return False
    return head not in TENNIS_KALSHI_APPROVED_SERIES


def is_tennis_canonical_event(event: CanonicalEvent | None) -> bool:
    return event is not None and event.sport == TENNIS_SPORT


def is_tennis_market_family(family: MarketFamily | str | None) -> bool:
    if family is None:
        return False
    value = family if isinstance(family, MarketFamily) else MarketFamily(str(family))
    return value in TENNIS_MARKET_FAMILIES


def is_tennis_payload(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    ticker = str(
        payload.get("series_ticker") or payload.get("event_ticker") or payload.get("ticker") or ""
    )
    if is_tennis_kalshi_ticker(ticker):
        return True
    sport = payload.get("sport")
    if isinstance(sport, dict):
        sport_code = normalize_text(str(sport.get("sport") or ""))
        series = str(sport.get("series") or "").strip()
        if sport_code in _POLYMARKET_TENNIS_SPORTS or series in _POLYMARKET_TENNIS_SERIES:
            return True
    series_id = str(payload.get("series_id") or payload.get("seriesId") or "").strip()
    if series_id in _POLYMARKET_TENNIS_SERIES:
        return True
    series_items = payload.get("series")
    if isinstance(series_items, dict):
        series_items = [series_items]
    if isinstance(series_items, list):
        for item in series_items:
            if not isinstance(item, dict):
                continue
            if str(item.get("id") or "").strip() in _POLYMARKET_TENNIS_SERIES:
                return True
            if normalize_text(str(item.get("ticker") or item.get("slug") or "")) in _POLYMARKET_TENNIS_SPORTS:
                return True
    slug = normalize_text(str(payload.get("slug") or ""))
    if slug.startswith("atp ") or slug.startswith("wta "):
        return True
    sport_name = normalize_text(
        str(payload.get("sport-name") or payload.get("sport_name") or "")
    )
    if sport_name == "tennis":
        return True
    meta = payload.get("meta-tags") or payload.get("meta_tags") or []
    if isinstance(meta, list):
        for tag in meta:
            if not isinstance(tag, dict):
                continue
            if normalize_text(str(tag.get("name") or "")) == "tennis" and normalize_text(
                str(tag.get("type") or "")
            ) == "sport":
                return True
    if str(payload.get("sport-id") or payload.get("sport_id") or "").strip() == "9" and sport_name in {
        "",
        "tennis",
    }:
        if sport_name == "table tennis":
            return False
        if sport_name == "tennis" or _meta_sport_is_tennis(meta):
            return True
    return False


def _meta_sport_is_tennis(meta: object) -> bool:
    if not isinstance(meta, list):
        return False
    for tag in meta:
        if not isinstance(tag, dict):
            continue
        if normalize_text(str(tag.get("name") or "")) == "tennis" and normalize_text(
            str(tag.get("type") or "")
        ) == "sport":
            return True
    return False

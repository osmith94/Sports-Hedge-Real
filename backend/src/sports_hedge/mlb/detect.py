"""MLB payload detection. Other baseball competitions fail closed."""

from __future__ import annotations

from typing import Any

from sports_hedge.domain.football import CanonicalEvent, MarketFamily
from sports_hedge.mlb.constants import (
    MATCHBOOK_MLB_COMPETITION_TAG_ID,
    MLB_KALSHI_APPROVED_SERIES,
    MLB_SPORT,
    POLYMARKET_MLB_SERIES_ID,
    POLYMARKET_MLB_SPORT,
)
from sports_hedge.normalization.text import normalize_text

MLB_MARKET_FAMILIES: frozenset[MarketFamily] = frozenset(
    {
        MarketFamily.GAME_WINNER,
        MarketFamily.TOTAL_RUNS,
    }
)

_MLB_KALSHI_PREFIXES = ("KXMLB", "KXNCAABB", "KXKBO", "KXWBC", "KXLMB", "KXNPB")
_OTHER_BASEBALL_POLYMARKET = frozenset(
    {"kbo", "npb", "wbc", "ncaabaseball", "cuba", "mlbb"}
)


def kalshi_series_head(ticker: str | None) -> str:
    return str(ticker or "").strip().upper().split("-", 1)[0]


def is_mlb_kalshi_ticker(ticker: str | None) -> bool:
    head = kalshi_series_head(ticker)
    return any(head.startswith(prefix) for prefix in _MLB_KALSHI_PREFIXES)


def approved_kalshi_mlb_series(ticker: str | None) -> str | None:
    head = kalshi_series_head(ticker)
    if head in MLB_KALSHI_APPROVED_SERIES:
        return head
    return None


def rejected_kalshi_mlb_series(ticker: str | None) -> bool:
    head = kalshi_series_head(ticker)
    if not is_mlb_kalshi_ticker(head):
        return False
    return head not in MLB_KALSHI_APPROVED_SERIES


def is_mlb_canonical_event(event: CanonicalEvent | None) -> bool:
    if event is None:
        return False
    return event.sport == MLB_SPORT and normalize_text(event.competition) in {
        "mlb",
        "major league baseball",
    }


def is_mlb_market_family(family: MarketFamily | str | None) -> bool:
    if family is None:
        return False
    try:
        value = family if isinstance(family, MarketFamily) else MarketFamily(str(family))
    except ValueError:
        return False
    return value in MLB_MARKET_FAMILIES


def matchbook_competition_tag(payload: dict[str, Any]) -> tuple[str, str] | None:
    meta = payload.get("meta-tags") or payload.get("meta_tags") or []
    if not isinstance(meta, list):
        return None
    for tag in meta:
        if not isinstance(tag, dict):
            continue
        tag_type = normalize_text(str(tag.get("type") or ""))
        if tag_type not in {"competition", "league"}:
            continue
        return str(tag.get("id") or "").strip(), str(tag.get("name") or "").strip()
    return None


def matchbook_is_mlb_competition(payload: dict[str, Any]) -> bool:
    tagged = matchbook_competition_tag(payload)
    if tagged is None:
        return False
    tag_id, name = tagged
    if tag_id == MATCHBOOK_MLB_COMPETITION_TAG_ID:
        return True
    return normalize_text(name) in {"mlb", "major league baseball"}


def is_mlb_payload(payload: dict[str, Any] | None) -> bool:
    """True only for MLB. KBO, NPB, WBC, college, and Mexican league are false."""

    if not isinstance(payload, dict):
        return False
    ticker = str(payload.get("series_ticker") or payload.get("event_ticker") or payload.get("ticker") or "")
    if approved_kalshi_mlb_series(ticker):
        details = payload.get("milestone")
        if isinstance(details, dict):
            league = details.get("details") if isinstance(details.get("details"), dict) else details
            league_name = normalize_text(str((league or {}).get("league") or ""))
            if league_name and league_name != "mlb":
                return False
        return True
    if rejected_kalshi_mlb_series(ticker) or is_mlb_kalshi_ticker(ticker):
        return False
    sport = payload.get("sport")
    if isinstance(sport, dict):
        sport_code = normalize_text(str(sport.get("sport") or sport.get("name") or ""))
        series = str(sport.get("series") or "").strip()
        if sport_code in _OTHER_BASEBALL_POLYMARKET:
            return False
        if sport_code == POLYMARKET_MLB_SPORT or series == POLYMARKET_MLB_SERIES_ID:
            slug = normalize_text(str(payload.get("slug") or ""))
            if "player props" in slug or slug.startswith("kbo") or slug.startswith("npb"):
                return False
            return True
    series_id = str(payload.get("series_id") or payload.get("seriesId") or "").strip()
    if series_id == POLYMARKET_MLB_SERIES_ID:
        return True
    if matchbook_is_mlb_competition(payload):
        name = normalize_text(str(payload.get("name") or ""))
        if "world series" in name or "player prop" in name:
            return False
        return True
    return False

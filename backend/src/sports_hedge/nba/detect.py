"""NBA payload/sport detection. Soccer and NFL recognisers must not consume these."""

from __future__ import annotations

from typing import Any

from sports_hedge.domain.football import CanonicalEvent, MarketFamily
from sports_hedge.nba.constants import (
    MATCHBOOK_BASKETBALL_SPORT_ID,
    MATCHBOOK_NBA_COMPETITION_TAG_ID,
    MATCHBOOK_NBA_PRESEASON_COMPETITION_NAME,
    MATCHBOOK_NBA_PRESEASON_COMPETITION_TAG_ID,
    NBA_KALSHI_APPROVED_SERIES,
    NBA_SPORT,
    POLYMARKET_NBA_SERIES_ID,
    POLYMARKET_NBA_SPORT,
)
from sports_hedge.normalization.text import normalize_text

NBA_MARKET_FAMILIES: frozenset[MarketFamily] = frozenset(
    {
        MarketFamily.GAME_WINNER,
        MarketFamily.POINT_SPREAD,
        MarketFamily.TOTAL_POINTS,
    }
)

_NBA_KALSHI_PREFIX = "KXNBA"
_REJECTED_KALSHI_HEADS = (
    "KXNBATEAMTOTAL",
    "KXNBA1H",
    "KXNBA2H",
    "KXNBA1Q",
    "KXNBA2Q",
    "KXNBA3Q",
    "KXNBA4Q",
    "KXNBAOT",
    "KXNBASUMMER",
)
_NON_NBA_BASKETBALL = frozenset(
    {
        "wnba",
        "ncaab",
        "ncaa basketball",
        "college basketball",
        "summer league",
        "nba summer league",
        "g league",
        "nba g league",
    }
)


def kalshi_series_head(ticker: str | None) -> str:
    return str(ticker or "").strip().upper().split("-", 1)[0]


def is_nba_kalshi_ticker(ticker: str | None) -> bool:
    return kalshi_series_head(ticker).startswith(_NBA_KALSHI_PREFIX)


def approved_kalshi_nba_series(ticker: str | None) -> str | None:
    head = kalshi_series_head(ticker)
    if head in NBA_KALSHI_APPROVED_SERIES:
        return head
    return None


def rejected_kalshi_nba_series(ticker: str | None) -> bool:
    head = kalshi_series_head(ticker)
    if not head.startswith(_NBA_KALSHI_PREFIX):
        return False
    if head in NBA_KALSHI_APPROVED_SERIES:
        return False
    return True


def is_nba_canonical_event(event: CanonicalEvent | None) -> bool:
    if event is None:
        return False
    if event.sport != NBA_SPORT:
        return False
    from sports_hedge.ncaab.detect import is_ncaab_competition_label

    return not is_ncaab_competition_label(event.competition)


def is_nba_market_family(family: MarketFamily | str | None) -> bool:
    if family is None:
        return False
    value = family if isinstance(family, MarketFamily) else MarketFamily(str(family))
    return value in NBA_MARKET_FAMILIES


def _matchbook_tag_rows(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
    meta = payload.get("meta-tags") or payload.get("meta_tags") or []
    rows: list[tuple[str, str, str]] = []
    if not isinstance(meta, list):
        return rows
    for tag in meta:
        if not isinstance(tag, dict):
            continue
        name = normalize_text(str(tag.get("name") or ""))
        tag_type = normalize_text(str(tag.get("type") or ""))
        tag_id = str(tag.get("id") or "").strip()
        rows.append((name, tag_type, tag_id))
    return rows


def matchbook_nba_listing_phase(payload: dict[str, Any] | None) -> str | None:
    """Exact Matchbook competition phase. ``None`` is not NBA.

    ``preseason`` is the captured game-book tag. ``nba`` is the regular NBA
    competition tag, including the championship outright. Summer League, G
    League, WNBA and unlabeled exhibition basketball stay ``None``.
    """

    if not isinstance(payload, dict):
        return None
    phase: str | None = None
    for name, tag_type, tag_id in _matchbook_tag_rows(payload):
        if name in _NON_NBA_BASKETBALL:
            return None
        competition = tag_type in {"competition", "league"}
        if tag_id == MATCHBOOK_NBA_PRESEASON_COMPETITION_TAG_ID or (
            competition and name == MATCHBOOK_NBA_PRESEASON_COMPETITION_NAME
        ):
            phase = "preseason"
        elif tag_id == MATCHBOOK_NBA_COMPETITION_TAG_ID or (competition and name == "nba"):
            phase = phase or "nba"
    return phase


def is_nba_payload(payload: dict[str, Any] | None) -> bool:
    """True when a raw venue payload is NBA basketball, not WNBA/NCAAB/NFL."""

    if not isinstance(payload, dict):
        return False
    if is_nba_kalshi_ticker(
        str(payload.get("series_ticker") or payload.get("event_ticker") or payload.get("ticker") or "")
    ):
        return True
    sport = payload.get("sport")
    if isinstance(sport, dict):
        sport_code = normalize_text(str(sport.get("sport") or sport.get("name") or ""))
        series = str(sport.get("series") or "").strip()
        if sport_code in {POLYMARKET_NBA_SPORT, "nba basketball"}:
            return True
        if series == POLYMARKET_NBA_SERIES_ID:
            return True
        if sport_code in _NON_NBA_BASKETBALL:
            return False
    elif normalize_text(str(sport or "")) in {POLYMARKET_NBA_SPORT, "nba basketball"}:
        return True
    series_field = payload.get("series")
    if isinstance(series_field, (int, str)) and str(series_field).strip() == POLYMARKET_NBA_SERIES_ID:
        return True
    if str(payload.get("series_id") or payload.get("seriesId") or "").strip() == POLYMARKET_NBA_SERIES_ID:
        return True
    slug = normalize_text(str(payload.get("slug") or payload.get("ticker") or payload.get("seriesSlug") or ""))
    if slug.startswith("nba ") or slug.startswith("nba-"):
        if "summer" in slug:
            return False
        return True
    sport_id = str(payload.get("sport-id") or payload.get("sport_id") or "").strip()
    tag_names = _matchbook_tag_rows(payload)
    for name, _tag_type, _tag_id in tag_names:
        if name in _NON_NBA_BASKETBALL:
            return False
    if sport_id == MATCHBOOK_BASKETBALL_SPORT_ID or any(
        name == "basketball" and tag_type == "sport" for name, tag_type, _ in tag_names
    ):
        if matchbook_nba_listing_phase(payload) is not None:
            return True
        sport_name = normalize_text(
            str(payload.get("sport-name") or payload.get("sport_name") or payload.get("sport") or "")
        )
        if sport_name == "nba":
            return True
        return False
    return False

"""NCAAB payload/sport detection. Soccer, NFL, WNBA, and NBA must not consume these."""

from __future__ import annotations

from typing import Any

from sports_hedge.domain.football import CanonicalEvent, MarketFamily
from sports_hedge.ncaab.constants import (
    MATCHBOOK_BASKETBALL_SPORT_ID,
    MATCHBOOK_NBA_COMPETITION_TAG_ID,
    MATCHBOOK_WNBA_COMPETITION_TAG_ID,
    NCAAB_COMPETITION,
    NCAAB_KALSHI_APPROVED_SERIES,
    NCAAB_KALSHI_LEGACY_GAME_SERIES,
    NCAAB_KALSHI_MENS_PREFIX,
    NCAAB_KALSHI_WOMENS_PREFIX,
    NCAAB_SPORT,
    POLYMARKET_MARCH_MADNESS_SERIES_ID,
    POLYMARKET_MARCH_MADNESS_SPORT,
    POLYMARKET_NCAAB_SERIES_ID,
    POLYMARKET_NCAAB_SPORT,
    POLYMARKET_NCAAW_SERIES_ID,
    POLYMARKET_NCAAW_SPORT,
    REJECTED_NON_NCAAB_BASKETBALL,
)
from sports_hedge.normalization.text import normalize_text

NCAAB_MARKET_FAMILIES: frozenset[MarketFamily] = frozenset(
    {
        MarketFamily.GAME_WINNER,
        MarketFamily.POINT_SPREAD,
        MarketFamily.TOTAL_POINTS,
    }
)

_EXCLUDED_BASKETBALL = frozenset(
    {
        "wnba",
        "nba",
        "nba basketball",
        "g league",
        "nba g league",
        "summer league",
        "nba summer league",
        "nba preseason",
        "ncaaw",
        "ncaa women",
        "womens college basketball",
        "women s college basketball",
        "cwbb",
        "d2",
        "d3",
        "division ii",
        "division iii",
        "naia",
        "junior college",
        "juco",
        "high school",
    }
)
_NCAAB_COMPETITION_LABELS = frozenset(
    {
        "ncaab",
        "ncaa men",
        "ncaa mens basketball",
        "ncaa men s basketball",
        "ncaa division i mens basketball",
        "ncaa division i men s basketball",
        "college basketball",
        "cbb",
        "ncaa cbb",
        "ncaamb",
        "cbb tournament",
    }
)
_NCAAB_TAG_TOKENS = frozenset(
    {
        "ncaa",
        "ncaab",
        "ncaamb",
        "college basketball",
        "ncaa mens basketball",
        "ncaa men s basketball",
        "division i",
        "ncaa division i",
    }
)


def kalshi_series_head(ticker: str | None) -> str:
    return str(ticker or "").strip().upper().split("-", 1)[0]


def is_ncaab_kalshi_ticker(ticker: str | None) -> bool:
    """Men's NCAAB Kalshi series only. Women's and legacy empty GAME are excluded."""

    head = kalshi_series_head(ticker)
    if head.startswith(NCAAB_KALSHI_WOMENS_PREFIX):
        return False
    if head == NCAAB_KALSHI_LEGACY_GAME_SERIES:
        return False
    return head.startswith(NCAAB_KALSHI_MENS_PREFIX)


def approved_kalshi_ncaab_series(ticker: str | None) -> str | None:
    head = kalshi_series_head(ticker)
    if head in NCAAB_KALSHI_APPROVED_SERIES:
        return head
    return None


def rejected_kalshi_ncaab_series(ticker: str | None) -> bool:
    """True for men's NCAAB Kalshi series that must not enter FIXTURE_MATCH."""

    head = kalshi_series_head(ticker)
    if head.startswith(NCAAB_KALSHI_WOMENS_PREFIX):
        return True
    if head == NCAAB_KALSHI_LEGACY_GAME_SERIES:
        return True
    if not head.startswith(NCAAB_KALSHI_MENS_PREFIX):
        return False
    return head not in NCAAB_KALSHI_APPROVED_SERIES


def is_ncaab_competition_label(value: str | None) -> bool:
    text = normalize_text(str(value or ""))
    if not text:
        return False
    if any(token in text for token in ("women", "ncaaw", "cwbb")):
        return False
    if text in _NCAAB_COMPETITION_LABELS:
        return True
    return text.startswith("ncaa men")


def is_ncaab_canonical_event(event: CanonicalEvent | None) -> bool:
    if event is None:
        return False
    if event.sport != NCAAB_SPORT:
        return False
    return is_ncaab_competition_label(event.competition) or event.competition == NCAAB_COMPETITION


def is_ncaab_market_family(family: MarketFamily | str | None) -> bool:
    if family is None:
        return False
    value = family if isinstance(family, MarketFamily) else MarketFamily(str(family))
    return value in NCAAB_MARKET_FAMILIES


def _tag_rows(payload: dict[str, Any]) -> list[tuple[str, str, str]]:
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


def matchbook_excluded_basketball_reason(payload: dict[str, Any] | None) -> str | None:
    """WNBA / NBA / G League / women's college on basketball sport-id 4."""

    if not isinstance(payload, dict):
        return None
    from sports_hedge.nba.constants import MATCHBOOK_NBA_PRESEASON_COMPETITION_TAG_ID

    for name, _tag_type, tag_id in _tag_rows(payload):
        if tag_id in {
            MATCHBOOK_WNBA_COMPETITION_TAG_ID,
            MATCHBOOK_NBA_COMPETITION_TAG_ID,
            MATCHBOOK_NBA_PRESEASON_COMPETITION_TAG_ID,
        }:
            return REJECTED_NON_NCAAB_BASKETBALL
        if name in _EXCLUDED_BASKETBALL or name in {"wnba", "nba", "nba preseason"}:
            return REJECTED_NON_NCAAB_BASKETBALL
    title = normalize_text(str(payload.get("name") or payload.get("title") or ""))
    if any(token in title for token in ("wnba", "nba championship", "nba finals")):
        return REJECTED_NON_NCAAB_BASKETBALL
    return None


def matchbook_has_ncaab_signal(payload: dict[str, Any] | None) -> bool:
    if not isinstance(payload, dict):
        return False
    if matchbook_excluded_basketball_reason(payload) is not None:
        return False
    for name, tag_type, _tag_id in _tag_rows(payload):
        if name in _NCAAB_TAG_TOKENS:
            return True
        if tag_type in {"competition", "league"} and "ncaa" in name:
            return True
    title = normalize_text(str(payload.get("name") or payload.get("title") or ""))
    competition = normalize_text(str(payload.get("competition-name") or payload.get("competition") or ""))
    return any(token in f"{title} {competition}" for token in _NCAAB_TAG_TOKENS)


def is_ncaab_payload(payload: dict[str, Any] | None) -> bool:
    """True when a raw venue payload is NCAA D1 men's basketball, not WNBA/NBA/NCAAW."""

    if not isinstance(payload, dict):
        return False
    ticker = str(
        payload.get("series_ticker") or payload.get("event_ticker") or payload.get("ticker") or ""
    )
    if rejected_kalshi_ncaab_series(ticker) and not approved_kalshi_ncaab_series(ticker):
        return False
    if is_ncaab_kalshi_ticker(ticker):
        return approved_kalshi_ncaab_series(ticker) is not None

    sport = payload.get("sport")
    series_id = str(payload.get("series_id") or payload.get("seriesId") or "").strip()
    if isinstance(sport, dict):
        sport_code = normalize_text(str(sport.get("sport") or sport.get("name") or ""))
        series = str(sport.get("series") or "").strip()
        if sport_code in {POLYMARKET_NCAAW_SPORT} or series == POLYMARKET_NCAAW_SERIES_ID:
            return False
        if sport_code == POLYMARKET_MARCH_MADNESS_SPORT or series == POLYMARKET_MARCH_MADNESS_SERIES_ID:
            return False
        if sport_code == POLYMARKET_NCAAB_SPORT or series == POLYMARKET_NCAAB_SERIES_ID:
            return True
    elif normalize_text(str(sport or "")) == POLYMARKET_NCAAB_SPORT:
        return True
    if series_id == POLYMARKET_NCAAW_SERIES_ID or series_id == POLYMARKET_MARCH_MADNESS_SERIES_ID:
        return False
    if series_id == POLYMARKET_NCAAB_SERIES_ID:
        return True
    series_field = payload.get("series")
    if isinstance(series_field, (int, str)) and str(series_field).strip() == POLYMARKET_NCAAB_SERIES_ID:
        return True

    slug = normalize_text(str(payload.get("slug") or payload.get("ticker") or payload.get("seriesSlug") or ""))
    if slug.startswith("cbb ") or slug.startswith("cbb-") or slug.startswith("ncaa cbb"):
        return True

    sport_id = str(payload.get("sport-id") or payload.get("sport_id") or "").strip()
    tags = _tag_rows(payload)
    sport_name = normalize_text(
        str(payload.get("sport-name") or payload.get("sport_name") or payload.get("sport") or "")
    )
    if sport_id == MATCHBOOK_BASKETBALL_SPORT_ID or sport_name == "basketball" or any(
        name == "basketball" and tag_type == "sport" for name, tag_type, _ in tags
    ):
        if matchbook_excluded_basketball_reason(payload) is not None:
            return False
        return matchbook_has_ncaab_signal(payload)
    return False

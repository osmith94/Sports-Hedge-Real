"""Operator UNIVERSE COMPETITION_SEASON scopes (Phase 1A follow-up).

Sibling picker rows for the existing #381 Universe scope. These are not
FIXTURE_MATCH competition codes and must not copy NFL game-fixture logic.

Selecting a row makes it eligible for the existing UNIVERSE worker's
discovery filters. Deselecting stops new discovery without deleting
catalogue history. PAPER admission stays closed.

Captured public native IDs: 2026-09-20 Phase 0A. Not owner-live quotes.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.domain.market_scope import MarketScope
from sports_hedge.domain.models import VenueName
from sports_hedge.domain.outrights import (
    CanonicalCompetitionSeasonRef,
    CanonicalSeasonMarketIdentity,
    OutrightMarketFamily,
    ParticipantType,
)
from sports_hedge.outrights.catalogue import persist_season_observation
from sports_hedge.outrights.native_ids import extract_kalshi_listing
from sports_hedge.outrights.participants import (
    SeasonParticipantError,
    epl_team_participant_id,
    nfl_season_team_id,
    venue_native_player_id,
)
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

SEASON_SCOPE_GROUP_ID = "outrights"
SEASON_SCOPE_GROUP_LABEL = "Outrights / season markets"
TOP_SCORER_NOT_EXECUTABLE_REASON = (
    "Observation-only · not executable while cross-venue equivalence remains blocked"
)
SEASON_OBSERVATION_REASON = "Season observation · not PAPER-admitted"
SEASON_SCOPE_CAPTURED_AT = "2026-09-20"


class SeasonUniverseScopeCode(StrEnum):
    EPL_2026_27_CHAMPION = "epl_2026_27_champion"
    NFL_2026_SUPER_BOWL_CHAMPION = "nfl_2026_super_bowl_champion"
    EPL_2026_27_TOP_SCORER = "epl_2026_27_top_scorer"


class SeasonUniverseScope(BaseModel):
    """Backend-authoritative season picker row. Venue tickers stay here."""

    code: SeasonUniverseScopeCode
    display_name: str
    selector_label: str
    sport: str
    competition_code: str
    season_id: str
    market_family: OutrightMarketFamily
    participant_type: ParticipantType
    observation_only: bool = False
    paper_executable: bool = False
    default_selected: bool = False
    selectable: bool = True
    verification_status: str = "OBSERVATION_ONLY"
    unavailable_reason: str | None = None
    kalshi_series_tickers: tuple[str, ...] = Field(default_factory=tuple)
    polymarket_event_ids: tuple[str, ...] = Field(default_factory=tuple)
    settlement_fingerprint_version: str
    expected_settlement_horizon: str | None = None


SEASON_UNIVERSE_SCOPES: tuple[SeasonUniverseScope, ...] = (
    SeasonUniverseScope(
        code=SeasonUniverseScopeCode.EPL_2026_27_CHAMPION,
        display_name="Premier League 2026-27 Champion",
        selector_label="Premier League 2026-27 · Champion",
        sport="football",
        competition_code="premier_league",
        season_id="2026/27",
        market_family=OutrightMarketFamily.COMPETITION_WINNER,
        participant_type=ParticipantType.TEAM,
        observation_only=True,
        unavailable_reason=SEASON_OBSERVATION_REASON,
        kalshi_series_tickers=("KXPREMIERLEAGUE",),
        polymarket_event_ids=("659518",),
        settlement_fingerprint_version="kalshi-kxpremierleague-27-2026-09-20",
        expected_settlement_horizon="2027-06-13T21:00:00Z",
    ),
    SeasonUniverseScope(
        code=SeasonUniverseScopeCode.NFL_2026_SUPER_BOWL_CHAMPION,
        display_name="NFL 2026 Super Bowl Champion",
        selector_label="NFL 2026 · Super Bowl Champion",
        sport="american_football",
        competition_code="nfl",
        season_id="NFL-2026-SB-LXI",
        market_family=OutrightMarketFamily.COMPETITION_WINNER,
        participant_type=ParticipantType.TEAM,
        observation_only=True,
        unavailable_reason=SEASON_OBSERVATION_REASON,
        kalshi_series_tickers=("KXSB",),
        polymarket_event_ids=("202857",),
        settlement_fingerprint_version="kalshi-kxsb-27-2026-09-20",
        expected_settlement_horizon="super_bowl_official_result",
    ),
    SeasonUniverseScope(
        code=SeasonUniverseScopeCode.EPL_2026_27_TOP_SCORER,
        display_name="Premier League 2026-27 Top Scorer",
        selector_label="Premier League 2026-27 · Top Scorer",
        sport="football",
        competition_code="premier_league",
        season_id="2026/27",
        market_family=OutrightMarketFamily.TOP_SCORER,
        participant_type=ParticipantType.PLAYER,
        observation_only=True,
        unavailable_reason=TOP_SCORER_NOT_EXECUTABLE_REASON,
        kalshi_series_tickers=("KXEPLLEADER",),
        polymarket_event_ids=("871869",),
        settlement_fingerprint_version="kalshi-kxeplleader-27goal-2026-09-20",
        expected_settlement_horizon="official_golden_boot",
    ),
)

_SCOPE_BY_CODE: dict[str, SeasonUniverseScope] = {
    item.code.value: item for item in SEASON_UNIVERSE_SCOPES
}
_SEASON_SERIES: dict[str, SeasonUniverseScope] = {}
for _item in SEASON_UNIVERSE_SCOPES:
    for _ticker in _item.kalshi_series_tickers:
        _SEASON_SERIES[_ticker.upper()] = _item

# NFL game fixtures (KXNFLGAME and similar) are not season scopes.
FORBIDDEN_NFL_FIXTURE_SERIES = frozenset({"KXNFLGAME", "KXNFLSPREAD", "KXNFLTOTAL", "KXNFLBTTS"})


def season_universe_scope_by_code(code: str | SeasonUniverseScopeCode | None) -> SeasonUniverseScope | None:
    if code is None:
        return None
    token = code.value if isinstance(code, SeasonUniverseScopeCode) else str(code).strip()
    return _SCOPE_BY_CODE.get(token)


def operator_season_scope_catalog() -> list[dict[str, Any]]:
    """UI catalog rows. No raw venue tickers."""

    rows: list[dict[str, Any]] = []
    for item in SEASON_UNIVERSE_SCOPES:
        rows.append(
            {
                "code": item.code.value,
                "display_name": item.display_name,
                "selector_label": item.selector_label,
                "group_id": SEASON_SCOPE_GROUP_ID,
                "group_label": SEASON_SCOPE_GROUP_LABEL,
                "default_selected": item.default_selected,
                "selectable": item.selectable,
                "verification_status": item.verification_status,
                "unavailable_reason": item.unavailable_reason,
                "market_scope": MarketScope.COMPETITION_SEASON.value,
                "observation_only": item.observation_only,
                "paper_executable": False,
            }
        )
    return rows


def normalize_selected_season_scope_codes(
    codes: list[str] | tuple[str, ...] | None,
    *,
    allow_empty: bool = True,
) -> tuple[str, ...]:
    """Keep known selectable season scopes only. Unknown codes are dropped."""

    selected: list[str] = []
    seen: set[str] = set()
    for raw in codes or ():
        item = season_universe_scope_by_code(raw)
        if item is None or not item.selectable:
            continue
        if item.code.value in seen:
            continue
        seen.add(item.code.value)
        selected.append(item.code.value)
    if not selected and not allow_empty:
        raise ValueError("universe_scope_empty_selection")
    return tuple(selected)


def default_season_scope_code_values() -> tuple[str, ...]:
    return tuple(item.code.value for item in SEASON_UNIVERSE_SCOPES if item.default_selected)


def kalshi_series_tickers_for_season_scopes(
    codes: list[str] | tuple[str, ...] | None,
) -> list[str]:
    tickers: list[str] = []
    seen: set[str] = set()
    for raw in codes or ():
        item = season_universe_scope_by_code(raw)
        if item is None:
            continue
        for ticker in item.kalshi_series_tickers:
            if ticker in FORBIDDEN_NFL_FIXTURE_SERIES:
                continue
            if ticker not in seen:
                seen.add(ticker)
                tickers.append(ticker)
    return tickers


def kalshi_ticker_is_season_series(series_ticker: str | None) -> bool:
    ticker = str(series_ticker or "").strip().upper()
    if not ticker or ticker in FORBIDDEN_NFL_FIXTURE_SERIES:
        return False
    if ticker in _SEASON_SERIES:
        return True
    for prefix, _item in _SEASON_SERIES.items():
        if ticker.startswith(prefix):
            return True
    return False


def season_scope_for_kalshi_ticker(series_ticker: str | None) -> SeasonUniverseScope | None:
    ticker = str(series_ticker or "").strip().upper()
    if not ticker or ticker in FORBIDDEN_NFL_FIXTURE_SERIES:
        return None
    direct = _SEASON_SERIES.get(ticker)
    if direct is not None:
        return direct
    matches: list[tuple[int, SeasonUniverseScope]] = []
    for prefix, item in _SEASON_SERIES.items():
        if ticker.startswith(prefix):
            matches.append((len(prefix), item))
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[0], reverse=True)
    return matches[0][1]


def partition_kalshi_season_events(
    payloads: list[dict[str, Any]],
    *,
    selected_season_scope_codes: list[str] | tuple[str, ...] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split Kalshi events so season series never enter fixture matching.

    Selected season series become catalogue-observation candidates. Unselected
    season series are dropped from the fixture path and are not observed.
    """

    selected = frozenset(normalize_selected_season_scope_codes(selected_season_scope_codes))
    fixture_events: list[dict[str, Any]] = []
    season_events: list[dict[str, Any]] = []
    for payload in payloads:
        ticker = _kalshi_series_ticker(payload)
        scope = season_scope_for_kalshi_ticker(ticker)
        if scope is None:
            fixture_events.append(payload)
            continue
        if scope.code.value in selected:
            season_events.append(payload)
    return fixture_events, season_events


def observe_selected_season_kalshi_events(
    store: SqliteApprovedMarketCatalogueStore | None,
    payloads: list[dict[str, Any]],
    *,
    now: datetime,
    generation_id: str | None = None,
) -> list[Any]:
    """Persist exact native-ID observation rows. Never admits PAPER/equivalence."""

    if store is None:
        return []
    persisted = []
    for payload in payloads:
        scope = season_scope_for_kalshi_ticker(_kalshi_series_ticker(payload))
        if scope is None:
            continue
        for market in _kalshi_markets(payload):
            try:
                listing = extract_kalshi_listing({"event": payload, "market": market})
                identity = _season_identity_for_kalshi_market(scope, payload, market)
                row = persist_season_observation(
                    store,
                    identity=identity,
                    listing=listing,
                    now=now,
                    generation_id=generation_id,
                )
            except (ValueError, SeasonParticipantError, KeyError, TypeError):
                continue
            persisted.append(row)
    return persisted


def _season_identity_for_kalshi_market(
    scope: SeasonUniverseScope,
    event: dict[str, Any],
    market: dict[str, Any],
) -> CanonicalSeasonMarketIdentity:
    participant_id = _participant_id_for_kalshi_market(scope, event, market)
    return CanonicalSeasonMarketIdentity(
        subject=CanonicalCompetitionSeasonRef(
            sport=scope.sport,
            competition_code=scope.competition_code,
            season_id=scope.season_id,
            event_scope=MarketScope.COMPETITION_SEASON,
        ),
        market_family=scope.market_family,
        participant_type=scope.participant_type,
        participant_canonical_id=participant_id,
        settlement_fingerprint_version=scope.settlement_fingerprint_version,
        expected_settlement_horizon=scope.expected_settlement_horizon,
    )


def _participant_id_for_kalshi_market(
    scope: SeasonUniverseScope,
    event: dict[str, Any],
    market: dict[str, Any],
) -> str:
    if scope.participant_type is ParticipantType.PLAYER:
        native = str(
            market.get("soccer_player_uuid")
            or event.get("soccer_player_uuid")
            or market.get("ticker")
            or ""
        ).strip()
        return venue_native_player_id(venue=VenueName.KALSHI.value, native_id=native)
    label = str(
        market.get("yes_sub_title")
        or market.get("subtitle")
        or market.get("title")
        or market.get("custom_strike")
        or ""
    ).strip()
    if scope.sport == "american_football":
        if not label:
            ticker = str(market.get("ticker") or "").strip()
            suffix = ticker.rsplit("-", 1)[-1] if "-" in ticker else ""
            label = suffix
        return nfl_season_team_id(label)
    if not label:
        ticker = str(market.get("ticker") or "").strip()
        suffix = ticker.rsplit("-", 1)[-1] if "-" in ticker else ""
        label = suffix
    if label:
        return epl_team_participant_id(label)
    uuid = str(market.get("soccer_team_uuid") or event.get("soccer_team_uuid") or "").strip()
    if uuid:
        return f"venue_native:kalshi:{uuid}"
    raise SeasonParticipantError("missing_epl_team_name")


def _kalshi_series_ticker(payload: dict[str, Any]) -> str:
    nested = payload.get("series")
    nested_ticker = ""
    if isinstance(nested, dict):
        nested_ticker = str(nested.get("ticker") or nested.get("series_ticker") or "").strip()
    return str(
        payload.get("series_ticker") or nested_ticker or payload.get("ticker") or ""
    ).strip()


def _kalshi_markets(payload: dict[str, Any]) -> list[dict[str, Any]]:
    markets = payload.get("markets") or payload.get("nested_markets")
    if isinstance(markets, dict):
        markets = [markets]
    if isinstance(markets, list):
        return [item for item in markets if isinstance(item, dict)]
    if payload.get("ticker") and payload.get("event_ticker"):
        return [payload]
    return [payload]

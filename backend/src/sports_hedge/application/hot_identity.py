"""HOT scheduling identity: one canonical football fixture, one HOT unit.

Uses curated team aliases and the declared EventMatcher kickoff window. Sibling
Kalshi GAME/BTTS/TOTAL/FTTS source timestamps that differ by a few minutes must
not become separate HOT jobs. This does not change market-equivalence or
settlement matching. Source aliases remain provenance. Canonical/source IDs
are not absorbed by a scheduling collision.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Iterable

from sports_hedge.facts.aliases import (
    _CANONICAL_TEAM_NAMES,
    resolve_team_name_for_competition,
)
from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.matching.events import target_competition_code

# Inclusive window, matching EventMatcher.kickoff_tolerance: delta > 5 minutes
# is a hard veto; 20:00 vs 20:05 is still one unit, 20:00 vs 20:06 is not.
HOT_KICKOFF_TOLERANCE = timedelta(minutes=5)


def _fixture_is_nfl(fixture: Any) -> bool:
    from sports_hedge.nfl.constants import NFL_COMPETITION, NFL_SPORT

    sport = str(getattr(fixture, "sport", "") or "").strip()
    competition = str(getattr(fixture, "competition", "") or "").strip()
    code = _fixture_competition_code(fixture)
    return sport == NFL_SPORT or competition.upper() == NFL_COMPETITION or code == "nfl"


def _fixture_is_nba(fixture: Any) -> bool:
    from sports_hedge.nba.constants import NBA_COMPETITION, NBA_SPORT

    sport = str(getattr(fixture, "sport", "") or "").strip()
    competition = str(getattr(fixture, "competition", "") or "").strip()
    code = _fixture_competition_code(fixture)
    return sport == NBA_SPORT or competition.upper() == NBA_COMPETITION or code == "nba"


def _fixture_is_ncaab(fixture: Any) -> bool:
    from sports_hedge.ncaab.constants import NCAAB_COMPETITION, NCAAB_SPORT
    from sports_hedge.ncaab.detect import is_ncaab_competition_label

    sport = str(getattr(fixture, "sport", "") or "").strip()
    competition = str(getattr(fixture, "competition", "") or "").strip()
    code = _fixture_competition_code(fixture)
    return (
        sport == NCAAB_SPORT
        or is_ncaab_competition_label(competition)
        or competition == NCAAB_COMPETITION
        or code == "ncaab"
    )


def _fixture_is_mlb(fixture: Any) -> bool:
    from sports_hedge.mlb.constants import MLB_COMPETITION, MLB_SPORT

    sport = str(getattr(fixture, "sport", "") or "").strip()
    competition = str(getattr(fixture, "competition", "") or "").strip()
    code = _fixture_competition_code(fixture)
    return sport == MLB_SPORT or competition.casefold() == MLB_COMPETITION or code == "mlb"


def scheduling_team_key(
    name: str | None,
    competition: str | None = None,
    *,
    nfl: bool = False,
    nba: bool = False,
    ncaab: bool = False,
    mlb: bool = False,
) -> str:
    """Resolve a team label to the longest curated canonical prefix.

    NFL/NBA/NCAAB aliases apply only when the caller marks the fixture so soccer
    labels such as Saints/Chiefs/Kings cannot leak into those identities.
    """

    if mlb:
        from sports_hedge.mlb.teams import resolve_mlb_team

        resolved_mlb = resolve_mlb_team(str(name or ""))
        if resolved_mlb.ok and resolved_mlb.canonical:
            return resolved_mlb.canonical
        return str(name or "")
    if ncaab:
        from sports_hedge.ncaab.teams import resolve_ncaab_team

        resolved_ncaab = resolve_ncaab_team(str(name or ""))
        if resolved_ncaab.ok and resolved_ncaab.canonical:
            return resolved_ncaab.canonical
        return str(name or "")
    if nfl:
        from sports_hedge.nfl.teams import resolve_nfl_team

        resolved_nfl = resolve_nfl_team(str(name or ""))
        if resolved_nfl.ok and resolved_nfl.canonical:
            return resolved_nfl.canonical
    if nba:
        from sports_hedge.nba.teams import resolve_nba_team

        resolved_nba = resolve_nba_team(str(name or ""))
        if resolved_nba.ok and resolved_nba.canonical:
            return resolved_nba.canonical
    resolved = resolve_team_name_for_competition(str(name or ""), competition)
    tokens = resolved.split()
    for index in range(len(tokens), 0, -1):
        candidate = " ".join(tokens[:index])
        mapped = resolve_team_name_for_competition(candidate, competition)
        if mapped in _CANONICAL_TEAM_NAMES:
            return mapped
    return resolved


def _fixture_competition_code(fixture: Any) -> str | None:
    code = getattr(fixture, "target_competition_code", None)
    if isinstance(code, str) and code.strip():
        return code.strip()
    competition = getattr(fixture, "competition", None)
    if not competition:
        return None
    return target_competition_code(str(competition))


def hot_scheduling_team_pair(fixture: Any) -> tuple[str, str] | None:
    """Order-independent curated senior pair, or None if identity is uncurated."""

    code = _fixture_competition_code(fixture)
    nfl = _fixture_is_nfl(fixture)
    nba = _fixture_is_nba(fixture)
    ncaab = _fixture_is_ncaab(fixture)
    mlb = _fixture_is_mlb(fixture)
    home = scheduling_team_key(
        getattr(fixture, "home_team", None), code, nfl=nfl, nba=nba, ncaab=ncaab, mlb=mlb
    )
    away = scheduling_team_key(
        getattr(fixture, "away_team", None), code, nfl=nfl, nba=nba, ncaab=ncaab, mlb=mlb
    )
    if not home or not away:
        return None
    if home not in _CANONICAL_TEAM_NAMES or away not in _CANONICAL_TEAM_NAMES:
        from sports_hedge.nba.teams import is_canonical_nba_team
        from sports_hedge.ncaab.teams import is_canonical_ncaab_team
        from sports_hedge.nfl.teams import is_canonical_nfl_team

        if ncaab and is_canonical_ncaab_team(home) and is_canonical_ncaab_team(away):
            pair = tuple(sorted((home, away)))
            return pair[0], pair[1]
        from sports_hedge.mlb.teams import is_canonical_mlb_team

        if mlb and is_canonical_mlb_team(home) and is_canonical_mlb_team(away):
            pair = tuple(sorted((home, away)))
            return pair[0], pair[1]
        if not (nfl and is_canonical_nfl_team(home) and is_canonical_nfl_team(away)) and not (
            nba and is_canonical_nba_team(home) and is_canonical_nba_team(away)
        ):
            return None
    pair = tuple(sorted((home, away)))
    return pair[0], pair[1]


def hot_scheduling_kickoff(fixture: Any) -> datetime | None:
    kickoff = getattr(fixture, "kickoff_utc", None)
    if kickoff is None:
        return None
    return require_aware_instant(kickoff, "kickoff_utc")


def hot_scheduling_key(fixture: Any) -> str | None:
    """Diagnostic team+kickoff token. Not the HOT unique-unit discriminator.

    Unique HOT units use ``same_hot_scheduling_unit`` / the inclusive 5-minute
    relation. A fixed floor-bucket is not that relation: 20:04 and 20:05 must
    still be one unit. Generic/uncurated labels stay None so placeholders
    cannot collapse unrelated fixtures.
    """

    pair = hot_scheduling_team_pair(fixture)
    kickoff = hot_scheduling_kickoff(fixture)
    if pair is None or kickoff is None:
        return None
    minute = kickoff.replace(second=0, microsecond=0)
    return f"{pair[0]}|{pair[1]}|{minute.isoformat()}"


def same_hot_scheduling_unit(left: Any, right: Any) -> bool:
    """True when exact curated seniors share the inclusive 5-minute kickoff window.

    Does not fuzzy-merge unknown/youth/women/reserve labels. Does not compare
    settlement or absorb canonical/source identity.
    """

    left_pair = hot_scheduling_team_pair(left)
    right_pair = hot_scheduling_team_pair(right)
    if left_pair is None or right_pair is None or left_pair != right_pair:
        return False
    left_kickoff = hot_scheduling_kickoff(left)
    right_kickoff = hot_scheduling_kickoff(right)
    if left_kickoff is None or right_kickoff is None:
        return False
    if _fixture_is_mlb(left) or _fixture_is_mlb(right):
        if not (_fixture_is_mlb(left) and _fixture_is_mlb(right)):
            return False
        left_key = str(getattr(left, "scheduled_game_key", None) or "").strip()
        right_key = str(getattr(right, "scheduled_game_key", None) or "").strip()
        if not left_key or not right_key or left_key != right_key:
            return False
    return abs(left_kickoff - right_kickoff) <= HOT_KICKOFF_TOLERANCE


def unique_hot_scheduling_ids(fixtures: Iterable[Any]) -> list[str]:
    """First representative canonical id per inclusive 5-minute curated unit.

    Uncurated rows fall back to their own canonical id and never join a
    curated unit. Order of ``fixtures`` selects the representative.
    """

    representatives: list[Any] = []
    ids: list[str] = []
    seen_uncurated: set[str] = set()
    for fixture in fixtures:
        canonical_id = str(getattr(fixture, "canonical_event_id", "") or "")
        pair = hot_scheduling_team_pair(fixture)
        if pair is None:
            if not canonical_id or canonical_id in seen_uncurated:
                continue
            seen_uncurated.add(canonical_id)
            representatives.append(fixture)
            ids.append(canonical_id)
            continue
        if any(same_hot_scheduling_unit(fixture, existing) for existing in representatives):
            continue
        representatives.append(fixture)
        ids.append(canonical_id or f"{pair[0]}|{pair[1]}")
    return ids


def prefer_hot_unit(left: Any, right: Any) -> Any:
    """Keep the richer / already-canonical row when two aliases collide."""

    left_venues = _venue_count(left)
    right_venues = _venue_count(right)
    if left_venues != right_venues:
        return left if left_venues > right_venues else right
    left_id = str(getattr(left, "canonical_event_id", "") or "")
    right_id = str(getattr(right, "canonical_event_id", "") or "")
    return left if left_id <= right_id else right


def _venue_count(fixture: Any) -> int:
    return sum(
        1
        for name in ("matchbook_matched", "polymarket_matched", "kalshi_matched")
        if bool(getattr(fixture, name, False))
    )

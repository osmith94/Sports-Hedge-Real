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

from sports_hedge.facts.aliases import _CANONICAL_TEAM_NAMES, resolve_team_name
from sports_hedge.application.quote_freshness import require_aware_instant

# Inclusive window, matching EventMatcher.kickoff_tolerance: delta > 5 minutes
# is a hard veto; 20:00 vs 20:05 is still one unit, 20:00 vs 20:06 is not.
HOT_KICKOFF_TOLERANCE = timedelta(minutes=5)


def scheduling_team_key(name: str | None) -> str:
    """Resolve a team label to the longest curated canonical prefix."""

    resolved = resolve_team_name(str(name or ""))
    tokens = resolved.split()
    for index in range(len(tokens), 0, -1):
        candidate = " ".join(tokens[:index])
        mapped = resolve_team_name(candidate)
        if mapped in _CANONICAL_TEAM_NAMES:
            return mapped
    return resolved


def hot_scheduling_team_pair(fixture: Any) -> tuple[str, str] | None:
    """Order-independent curated senior pair, or None if identity is uncurated."""

    home = scheduling_team_key(getattr(fixture, "home_team", None))
    away = scheduling_team_key(getattr(fixture, "away_team", None))
    if not home or not away:
        return None
    if home not in _CANONICAL_TEAM_NAMES or away not in _CANONICAL_TEAM_NAMES:
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

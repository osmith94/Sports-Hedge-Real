"""HOT scheduling identity: one canonical football fixture, one HOT unit.

Uses curated team aliases and kickoff-minute identity only. This does not
change market-equivalence or settlement matching. Source aliases remain
provenance; they must not become separate HOT jobs.
"""

from __future__ import annotations

from typing import Any

from sports_hedge.facts.aliases import _CANONICAL_TEAM_NAMES, resolve_team_name
from sports_hedge.application.quote_freshness import require_aware_instant


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


def hot_scheduling_key(fixture: Any) -> str | None:
    """Stable HOT roster key: curated teams + kickoff minute, order-independent.

    Generic or uncurated labels (for example test placeholders Home/Away) must
    not collapse unrelated fixtures that happen to share a kickoff minute.
    """

    home = scheduling_team_key(getattr(fixture, "home_team", None))
    away = scheduling_team_key(getattr(fixture, "away_team", None))
    kickoff = getattr(fixture, "kickoff_utc", None)
    if not home or not away or kickoff is None:
        return None
    if home not in _CANONICAL_TEAM_NAMES or away not in _CANONICAL_TEAM_NAMES:
        return None
    aware = require_aware_instant(kickoff, "kickoff_utc")
    minute = aware.replace(second=0, microsecond=0)
    pair = tuple(sorted((home, away)))
    return f"{pair[0]}|{pair[1]}|{minute.isoformat()}"


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

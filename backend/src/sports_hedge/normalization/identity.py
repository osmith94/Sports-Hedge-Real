from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from typing import Sequence

from sports_hedge.domain.football import CanonicalEvent, CanonicalMarket
from sports_hedge.normalization.text import normalize_text


def canonical_source_event_id(event: CanonicalEvent) -> str:
    payload = "|".join(
        [
            event.sport,
            normalize_text(event.competition),
            normalize_text(event.home_team),
            normalize_text(event.away_team),
            kickoff_bucket(event.kickoff_utc).isoformat(),
            event.source_venue.value,
            event.source_event_id,
        ]
    )
    return f"evt:{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def canonical_source_market_id(canonical_event_id: str, market: CanonicalMarket) -> str:
    payload = "|".join(
        [
            canonical_event_id,
            market.family.value,
            market.period.value,
            "" if market.line is None else format(market.line, "f"),
            market.settlement.deterministic_key(),
            market.source_venue.value,
            market.source_market_id,
        ]
    )
    return f"mkt:{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def canonical_matched_event_id(events: Sequence[CanonicalEvent]) -> str:
    """Build a stable event id for a matched cross-venue event set.

    Source references make the key stable across repeated scans while normalized
    structural fields make accidental source-id reuse exceedingly unlikely. The
    kickoff timestamp is rounded to the nearest five minutes, matching the current
    event-matching tolerance and the architecture's kickoff-bucket design.
    """

    if not events:
        raise ValueError("events must not be empty")
    first = events[0]
    source_refs = sorted(f"{event.source_venue.value}:{event.source_event_id}" for event in events)
    payload = "|".join(
        [
            first.sport,
            normalize_text(first.home_team),
            normalize_text(first.away_team),
            kickoff_bucket(first.kickoff_utc).isoformat(),
            *source_refs,
        ]
    )
    return f"evt:{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def canonical_matched_market_id(
    canonical_event_id: str,
    markets: Sequence[CanonicalMarket],
) -> str:
    """Build a stable id for economically equivalent matched markets."""

    if not markets:
        raise ValueError("markets must not be empty")
    first = markets[0]
    source_refs = sorted(
        f"{market.source_venue.value}:{market.source_market_id}" for market in markets
    )
    payload = "|".join(
        [
            canonical_event_id,
            first.family.value,
            first.period.value,
            "" if first.line is None else format(first.line, "f"),
            first.settlement.deterministic_key(),
            *source_refs,
        ]
    )
    return f"mkt:{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def kickoff_bucket(value: datetime) -> datetime:
    """Round kickoff to the nearest five minutes (UTC)."""

    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    utc = aware.astimezone(UTC)
    bucket_seconds = 5 * 60
    rounded = ((int(utc.timestamp()) + bucket_seconds // 2) // bucket_seconds) * bucket_seconds
    return datetime.fromtimestamp(rounded, tz=UTC)


def _kickoff_bucket(value: datetime) -> datetime:
    return kickoff_bucket(value)

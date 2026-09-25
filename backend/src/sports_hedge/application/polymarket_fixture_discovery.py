"""Polymarket football fixture discovery horizon.

Gamma ``active=true`` / ``closed=false`` is not evidence that a fixture is
still useful. Ordinary match listings whose kickoff is older than the shared
post-kickoff current-radar ceiling are excluded from the retained discovery
set.

This is discovery hygiene:

- provider payloads stay counted on the series row
- nothing is written to the viability cache
- a past kickoff is not terminal settlement evidence
- outright / season markets are exempt
- a missing fixture start stays unknown and is retained
- postponed, delayed, and rescheduled listings are retained
- live and recently started fixtures inside the ceiling stay retained

The ceiling is ``DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING`` (4 hours), the
same hard current-radar bound used for HOT membership (Tenet 11 / Issue #275).
UNIVERSE still keeps future fixtures of any distance. Four hours is long
enough for a match that is in progress or only just finished, and short
enough that a provider-active FA Cup archive cannot crowd out current leagues.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sports_hedge.application.scan_lanes import DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING
from sports_hedge.application.target_competitions import resolve_target_competition_from_series_id
from sports_hedge.normalization.venues import (
    VenueNormalizationError,
    _parse_polymarket_fixture_datetime,
    _polymarket_fixture_start,
)

SERIES_NOT_STARTED = "not_started"
SERIES_DEFERRED_REASON = "discovery_deferred"
STALE_FIXTURE_OUTSIDE_DISCOVERY_HORIZON = "stale_fixture_outside_discovery_horizon"
STALE_FIXTURE_SAMPLE_LIMIT = 3

_OUTRIGHT_MARKERS = (
    "winner",
    "relegat",
    "golden boot",
    "golden-boot",
    "top scorer",
    "top-scorer",
    "to finish",
    "outright",
    "champion",
)
_SCHEDULE_EXCEPTION_STATUSES = frozenset({"postponed", "delayed", "rescheduled"})


def discovery_clock() -> datetime:
    """Wall clock for production discovery. Tests may patch this."""

    return datetime.now(UTC)


def series_competition_code(series_id: str) -> str | None:
    item = resolve_target_competition_from_series_id(series_id)
    if item is None:
        return None
    return item.code.value


def classify_polymarket_discovery_event(payload: dict[str, Any], *, now: datetime) -> str:
    """Return how one Gamma event should be treated during fixture discovery.

    ``stale_fixture`` is the only class excluded from the retained set.
    """

    if _schedule_exception(payload):
        return "schedule_exception"
    if _outright_market(payload):
        return "outright_exempt"
    raw_start = _polymarket_fixture_start(payload)
    if not raw_start:
        return "unknown_start"
    try:
        kickoff = _parse_polymarket_fixture_datetime(raw_start)
    except VenueNormalizationError:
        return "unknown_start"
    evaluated = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    if kickoff > evaluated:
        return "future"
    if evaluated - kickoff > DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING:
        return "stale_fixture"
    return "current"


def blank_series_row(
    series_id: str,
    *,
    status: str,
    reason: str | None,
    http_attempted: bool,
    retryable: bool,
    pages_attempted: int = 0,
) -> dict[str, Any]:
    return {
        "series": series_id,
        "competition": series_competition_code(series_id),
        "status": status,
        "retryable": retryable,
        "event_count": 0,
        "raw_event_count": 0,
        "retained_event_count": 0,
        "deduped_event_count": 0,
        "stale_fixture_rejections": 0,
        "unknown_start_count": 0,
        "future_fixture_count": 0,
        "current_fixture_count": 0,
        "outright_retained_count": 0,
        "schedule_exception_count": 0,
        "pages_attempted": pages_attempted,
        "http_attempted": http_attempted,
        "empty": status == "ok",
        "reason": reason,
        "stale_fixture_sample_ids": [],
    }


def retain_polymarket_page(
    page_events: list[dict[str, Any]],
    *,
    seen: set[str],
    now: datetime,
    event_id,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Split one series page into retained events and discovery counts.

    ``event_id`` is a callable ``(item) -> str`` so the collector keeps its
    venue id rule. Stale fixtures are counted and sampled, not retained.
    """

    retained: list[dict[str, Any]] = []
    stats = {
        "raw_event_count": 0,
        "retained_event_count": 0,
        "deduped_event_count": 0,
        "stale_fixture_rejections": 0,
        "unknown_start_count": 0,
        "future_fixture_count": 0,
        "current_fixture_count": 0,
        "outright_retained_count": 0,
        "schedule_exception_count": 0,
        "stale_fixture_sample_ids": [],
    }
    samples: list[str] = stats["stale_fixture_sample_ids"]
    for item in page_events:
        if not isinstance(item, dict):
            continue
        stats["raw_event_count"] += 1
        identity = str(event_id(item) or "").strip()
        if identity and identity in seen:
            stats["deduped_event_count"] += 1
            continue
        kind = classify_polymarket_discovery_event(item, now=now)
        if kind == "stale_fixture":
            stats["stale_fixture_rejections"] += 1
            if identity and len(samples) < STALE_FIXTURE_SAMPLE_LIMIT:
                samples.append(identity)
            if identity:
                seen.add(identity)
            continue
        if identity:
            seen.add(identity)
        if kind == "unknown_start":
            stats["unknown_start_count"] += 1
        elif kind == "future":
            stats["future_fixture_count"] += 1
        elif kind == "current":
            stats["current_fixture_count"] += 1
        elif kind == "outright_exempt":
            stats["outright_retained_count"] += 1
        elif kind == "schedule_exception":
            stats["schedule_exception_count"] += 1
        retained.append(item)
        stats["retained_event_count"] += 1
    return retained, stats


def _label(payload: dict[str, Any]) -> str:
    return f"{payload.get('title') or ''} {payload.get('slug') or ''}".casefold()


def _outright_market(payload: dict[str, Any]) -> bool:
    text = _label(payload)
    if " vs " in text or " v " in text:
        return False
    return any(marker in text for marker in _OUTRIGHT_MARKERS)


def _schedule_exception(payload: dict[str, Any]) -> bool:
    status = str(payload.get("gameStatus") or payload.get("status") or "").strip().casefold()
    if status in _SCHEDULE_EXCEPTION_STATUSES:
        return True
    text = _label(payload)
    return any(token in text for token in _SCHEDULE_EXCEPTION_STATUSES)

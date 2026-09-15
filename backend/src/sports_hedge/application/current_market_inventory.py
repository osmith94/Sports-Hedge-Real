"""Canonical current market inventory merge for one fixture.

Issue #165: HOT and UNIVERSE observations share one fixture identity. Current
market rows are keyed by canonical family/period/line/settlement — never by
fuzzy labels — and merged so a partial HOT refresh cannot collapse still-current
equivalents from the other lane.

Radar TTL governs whether a market row remains current. Paper eligibility and
auto-capture still require executable quote freshness independently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sports_hedge.application.executable_liquidity import (
    NO_EXECUTABLE_ARB,
    FixtureHeadlineCandidate,
    HeadlineBand,
    LiquidityRole,
    PASSIVE_REJECTION_REASONS,
    best_arb_market_label,
    headline_band_for,
    select_fixture_headline,
)
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
    InventoryPairResult,
    inventory_summary,
    sort_fixture_inventory_rows,
)
from sports_hedge.application.quote_freshness import effective_quote_age_ms, require_aware_instant
from sports_hedge.application.scan_lanes import (
    DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    FRESHNESS_EXECUTABLE,
    FRESHNESS_EXPIRED,
    FRESHNESS_RADAR_CURRENT,
    ScanLane,
    freshness_class,
)

_MAPPING_INCOMPATIBLE = frozenset(
    {
        "incomplete_outcome_set",
        "settlement_mismatch",
        "incomplete_settlement",
        "unknown_settlement_scope",
        "unsupported_outcome_model",
        "unsupported_family",
        "market_not_equivalent",
        "noncanonical_outcome_space",
        "push_state_not_modelled",
        "unproven_settlement_semantics",
        "unproven_handicap_semantics",
        "generalized_split_line_not_modelled",
        "unknown_draw_void_semantics",
    }
)


@dataclass
class CurrentMarketSlot:
    key: str
    row: FixtureMarketInventoryRow
    scan_lane: ScanLane
    last_scanned_at: datetime
    paper_market_ids: tuple[str, ...]


def canonical_current_market_key(row: FixtureMarketInventoryRow) -> str:
    """Stable current-state key. Family/period/line/settlement, never display names."""

    family = (row.family or "").strip()
    period = (row.period or "").strip()
    line = "" if row.line is None else format(row.line, "f")
    settlement = ""
    for facts in (row.matchbook, row.polymarket, row.kalshi):
        if facts is not None and facts.settlement_key:
            settlement = facts.settlement_key
            break
    if family:
        return f"canon:{family}|{period}|{line}|{settlement}"
    sources: list[str] = []
    for facts in (row.matchbook, row.polymarket, row.kalshi):
        if facts is None:
            continue
        sources.append(f"{facts.venue.value}:{facts.source_market_id}")
    if sources:
        return "source:" + "|".join(sources)
    return f"display:{row.display_name}"


def merge_current_market_slots(
    existing: dict[str, CurrentMarketSlot],
    incoming_rows: list[FixtureMarketInventoryRow],
    *,
    scan_lane: ScanLane,
    scanned_at: datetime,
    paper_market_ids: tuple[str, ...],
    evaluated: bool,
) -> dict[str, CurrentMarketSlot]:
    """Upsert evaluated canonical markets. Partial/not_evaluated work is a no-op."""

    if not evaluated:
        return dict(existing)
    merged = dict(existing)
    scanned = require_aware_instant(scanned_at, "last_scanned_at")
    lane = ScanLane(scan_lane) if not isinstance(scan_lane, ScanLane) else scan_lane
    for row in incoming_rows:
        key = canonical_current_market_key(row)
        previous = merged.get(key)
        if previous is not None and not _incoming_supersedes(previous, scanned_at=scanned, scan_lane=lane):
            continue
        merged[key] = CurrentMarketSlot(
            key=key,
            row=row,
            scan_lane=lane,
            last_scanned_at=scanned,
            paper_market_ids=paper_market_ids,
        )
    return merged


def prune_expired_market_slots(
    slots: dict[str, CurrentMarketSlot],
    now: datetime,
    *,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
) -> dict[str, CurrentMarketSlot]:
    evaluated = require_aware_instant(now, "now")
    live: dict[str, CurrentMarketSlot] = {}
    for key, slot in slots.items():
        freshness = slot_freshness(
            slot,
            now=evaluated,
            hot_ttl_seconds=hot_ttl_seconds,
            universe_ttl_seconds=universe_ttl_seconds,
            max_quote_age_ms=max_quote_age_ms,
        )
        if freshness == FRESHNESS_EXPIRED:
            continue
        live[key] = slot
    return live


def slot_freshness(
    slot: CurrentMarketSlot,
    *,
    now: datetime,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
) -> str:
    quote_age = effective_quote_age_ms(row_quote_age_ms(slot.row), slot.last_scanned_at, now)
    return freshness_class(
        lane=slot.scan_lane,
        last_scanned_at=slot.last_scanned_at,
        now=now,
        quote_age_ms=quote_age,
        max_quote_age_ms=max_quote_age_ms,
        hot_ttl_seconds=hot_ttl_seconds,
        universe_ttl_seconds=universe_ttl_seconds,
    )


def stamp_current_market_row(
    slot: CurrentMarketSlot,
    *,
    now: datetime | None,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
) -> FixtureMarketInventoryRow:
    if now is None:
        freshness = FRESHNESS_RADAR_CURRENT
    else:
        freshness = slot_freshness(
            slot,
            now=now,
            hot_ttl_seconds=hot_ttl_seconds,
            universe_ttl_seconds=universe_ttl_seconds,
            max_quote_age_ms=max_quote_age_ms,
        )
    return slot.row.model_copy(
        update={
            "scan_lane": slot.scan_lane.value,
            "last_scanned_at": slot.last_scanned_at,
            "radar_freshness": freshness,
        }
    )


def equivalent_comparison_count(rows: list[FixtureMarketInventoryRow]) -> int:
    """Count distinct currently valid canonical market comparisons.

    A comparison is a matched-equivalent family/line plus each distinct venue
    pair on that row. Rows without pair payloads still count as one comparison.
    """

    keys: set[tuple[str, str, str, str]] = set()
    for row in rows:
        if row.comparison_status is not InventoryComparisonStatus.MATCHED_EQUIVALENT:
            continue
        family = row.family or ""
        period = row.period or ""
        line = "" if row.line is None else format(row.line, "f")
        pairs = comparable_venue_pairs(row)
        if not pairs:
            keys.add((family, period, line, "row"))
            continue
        for left, right in pairs:
            keys.add((family, period, line, f"{left}|{right}"))
    return len(keys)


def comparable_venue_pairs(row: FixtureMarketInventoryRow) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for result in row.pair_results:
        if not _pair_is_comparable(result):
            continue
        left, right = sorted((result.left_venue.value, result.right_venue.value))
        pairs.add((left, right))
    if pairs:
        return pairs
    venues = [
        facts.venue.value
        for facts in (row.matchbook, row.polymarket, row.kalshi)
        if facts is not None
    ]
    if len(venues) < 2:
        return set()
    ordered = sorted(set(venues))
    for index, left in enumerate(ordered):
        for right in ordered[index + 1 :]:
            pairs.add((left, right))
    return pairs


def candidate_from_current_slot(
    slot: CurrentMarketSlot,
    *,
    now: datetime | None,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
) -> FixtureHeadlineCandidate:
    row = slot.row
    if now is None:
        freshness = FRESHNESS_RADAR_CURRENT
        quote_age = row_quote_age_ms(row)
    else:
        freshness = slot_freshness(
            slot,
            now=now,
            hot_ttl_seconds=hot_ttl_seconds,
            universe_ttl_seconds=universe_ttl_seconds,
            max_quote_age_ms=max_quote_age_ms,
        )
        quote_age = effective_quote_age_ms(row_quote_age_ms(row), slot.last_scanned_at, now)
    executable = freshness == FRESHNESS_EXECUTABLE
    reasons = list(row.rejection_reasons)
    role = LiquidityRole.MAKER if set(reasons) & PASSIVE_REJECTION_REASONS else LiquidityRole.TAKER
    return FixtureHeadlineCandidate(
        family=row.family,
        line=row.line,
        current_net_edge=row.current_net_edge,
        trigger_net_edge=row.trigger_net_edge,
        eligible_for_paper_simulation=bool(row.solver_is_arbitrage and executable),
        solver_is_arbitrage=row.solver_is_arbitrage,
        rejection_reasons=reasons,
        liquidity_role=role,
        quote_age_ms=quote_age,
    )


def apply_current_market_inventory(
    fixture,
    slots: list[CurrentMarketSlot],
    *,
    now: datetime | None,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
):
    """Project merged current markets onto fixture headline fields."""

    ttl = {
        "hot_ttl_seconds": hot_ttl_seconds,
        "universe_ttl_seconds": universe_ttl_seconds,
        "max_quote_age_ms": max_quote_age_ms,
    }
    stamped = [
        stamp_current_market_row(slot, now=now, **ttl)
        for slot in slots
        if now is None
        or slot_freshness(slot, now=now, **ttl) != FRESHNESS_EXPIRED
    ]
    stamped = sort_fixture_inventory_rows(stamped)
    if not stamped:
        return fixture, []
    discovered, matched_rows, _best = inventory_summary(stamped)
    equivalent = equivalent_comparison_count(stamped)
    live_slots = [
        slot
        for slot in slots
        if now is None or slot_freshness(slot, now=now, **ttl) != FRESHNESS_EXPIRED
    ]
    candidates = [candidate_from_current_slot(slot, now=now, **ttl) for slot in live_slots]
    qualifying = sum(1 for item in candidates if headline_band_for(item) is HeadlineBand.QUALIFYING)
    near = sum(1 for item in candidates if headline_band_for(item) is HeadlineBand.NEAR_EXECUTABLE)
    headline = select_fixture_headline(candidates)
    winner_row = _headline_row(stamped, headline.candidate)
    updates: dict[str, object] = {
        "discovered_market_count": discovered,
        "matched_market_count": matched_rows,
        "matched_equivalent_count": equivalent,
        "qualifying_market_count": qualifying,
        "near_executable_market_count": near,
        "headline_band": headline.band.value,
        "best_arb_market": headline.best_arb_market
        or (
            best_arb_market_label(headline.candidate.family, line=headline.candidate.line)
            if headline.candidate is not None
            else None
        ),
        "solver_is_arbitrage": headline.band is HeadlineBand.QUALIFYING,
        "market_evaluation_state": "evaluated",
    }
    if headline.candidate is None:
        updates.update(
            {
                "current_net_edge": None,
                "trigger_net_edge": None,
                "distance_to_trigger_pp": None,
                "best_matchbook_price": None,
                "best_polymarket_price": None,
                "best_kalshi_price": None,
            }
        )
        if equivalent and fixture.no_comparison_reason is None:
            updates["no_comparison_reason"] = headline.reason or NO_EXECUTABLE_ARB
        elif not equivalent:
            updates["no_comparison_reason"] = fixture.no_comparison_reason
    else:
        updates.update(
            {
                "market_family": headline.candidate.family,
                "current_net_edge": headline.candidate.current_net_edge,
                "trigger_net_edge": headline.candidate.trigger_net_edge,
                "distance_to_trigger_pp": (
                    winner_row.distance_to_trigger_pp if winner_row is not None else None
                ),
                "quote_age_ms": headline.candidate.quote_age_ms,
                "no_comparison_reason": None
                if headline.band in {HeadlineBand.QUALIFYING, HeadlineBand.NEAR_EXECUTABLE}
                else fixture.no_comparison_reason,
            }
        )
        if winner_row is not None:
            updates["best_matchbook_price"] = _best_back_price(winner_row.matchbook)
            updates["best_polymarket_price"] = _best_back_price(winner_row.polymarket)
            updates["best_kalshi_price"] = _best_back_price(winner_row.kalshi)
    projected = fixture.model_copy(update=updates)
    projected.opportunity_state = _opportunity_state(projected)
    return projected, stamped


def union_paper_market_ids(slots: list[CurrentMarketSlot]) -> tuple[str, ...]:
    ids: list[str] = []
    for slot in slots:
        for market_id in slot.paper_market_ids:
            if market_id and market_id not in ids:
                ids.append(market_id)
    return tuple(ids)


def combined_radar_freshness(rows: list[FixtureMarketInventoryRow]) -> str:
    classes = {row.radar_freshness for row in rows if row.radar_freshness}
    if FRESHNESS_EXECUTABLE in classes:
        return FRESHNESS_EXECUTABLE
    if FRESHNESS_RADAR_CURRENT in classes:
        return FRESHNESS_RADAR_CURRENT
    return FRESHNESS_EXPIRED


def row_quote_age_ms(row: FixtureMarketInventoryRow) -> int | None:
    ages = [
        facts.quote_age_ms
        for facts in (row.matchbook, row.polymarket, row.kalshi)
        if facts is not None and facts.quote_age_ms is not None
    ]
    if not ages:
        return None
    return max(ages)


def _incoming_supersedes(
    previous: CurrentMarketSlot,
    *,
    scanned_at: datetime,
    scan_lane: ScanLane,
) -> bool:
    if scanned_at > previous.last_scanned_at:
        return True
    if scanned_at < previous.last_scanned_at:
        return False
    if scan_lane is ScanLane.HOT and previous.scan_lane is not ScanLane.HOT:
        return True
    return False


def _pair_is_comparable(result: InventoryPairResult) -> bool:
    if result.entered_solver:
        return True
    if not result.rejection_reasons:
        return True
    return not any(reason in _MAPPING_INCOMPATIBLE for reason in result.rejection_reasons)


def _headline_row(
    rows: list[FixtureMarketInventoryRow],
    candidate: FixtureHeadlineCandidate | None,
) -> FixtureMarketInventoryRow | None:
    if candidate is None:
        return None
    for row in rows:
        if row.family != candidate.family:
            continue
        if row.line != candidate.line:
            continue
        return row
    return rows[0] if rows else None


def _best_back_price(facts) -> Decimal | None:
    if facts is None:
        return None
    prices = [quote.decimal_odds for quote in facts.best_backs if quote.decimal_odds is not None]
    return max(prices) if prices else None


def _opportunity_state(fixture) -> str:
    if getattr(fixture, "market_evaluation_state", None) != "evaluated":
        return "not_evaluated"
    if fixture.headline_band == HeadlineBand.QUALIFYING.value or fixture.solver_is_arbitrage:
        return "qualifying"
    if fixture.headline_band == HeadlineBand.NEAR_EXECUTABLE.value:
        return "near"
    if fixture.matched_equivalent_count:
        return "matched"
    return "unmatched"

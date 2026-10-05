"""Canonical current market inventory merge for one fixture.

Issue #165: HOT and UNIVERSE observations share one fixture identity. Current
market rows are keyed by canonical family/period/line/settlement — never by
fuzzy labels — and merged so a partial HOT refresh cannot collapse still-current
equivalents from the other lane.

Issue #308: ApprovedEquivalent / market-relationship presence is
generation-aware. A UNIVERSE evaluation remains discovery-current for the
generation that proved it. After that proving generation closes, expiry is
anchored to that generation's own close time plus radar TTL. A later open
generation does not revive or suspend that expiry. Quote freshness, paper
eligibility, auto-capture and HOT promotion stay on the short-lived
executable / radar clocks and are independent of relationship retention.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sports_hedge.application.executable_liquidity import (
    HARD_NON_EXECUTABLE_REASONS,
    NO_EXECUTABLE_ARB,
    PASSIVE_REJECTION_REASONS,
    FixtureHeadlineCandidate,
    HeadlineBand,
    LiquidityRole,
    best_arb_market_label,
    headline_band_for,
    select_fixture_headline,
)
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
    InventoryPairResult,
    inventory_is_comparable_opportunity,
    inventory_summary,
    sort_fixture_inventory_rows,
)
from sports_hedge.application.quote_freshness import effective_quote_age_ms, require_aware_instant
from sports_hedge.application.scan_lanes import (
    DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    FRESHNESS_EXECUTABLE,
    FRESHNESS_EXPIRED,
    FRESHNESS_RADAR_CURRENT,
    ScanLane,
    freshness_class,
)
from sports_hedge.arbitrage.watchlist.economics import (
    distance_to_trigger_pp,
    promotes_hot_net_proximity,
    qualifies_min_net_arb,
    resolve_hot_proximity_limits,
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
# Fail-closed reasons that must never fabricate HOT opportunity promotion.
PROMOTION_FAIL_CLOSED_REASONS = frozenset(
    {
        *HARD_NON_EXECUTABLE_REASONS,
        *_MAPPING_INCOMPATIBLE,
        "missing_costs",
        "missing_fx",
        "missing_fx_rate",
        "unknown_required_cost",
        "unknown_fees",
        "allocator_size_required",
        "allocator_rejected",
        "fill_plan_not_allocator_sized",
        "insufficient_venue_equivalence",
    }
)


@dataclass
class CurrentMarketSlot:
    key: str
    row: FixtureMarketInventoryRow
    scan_lane: ScanLane
    last_scanned_at: datetime
    paper_market_ids: tuple[str, ...]
    evaluated_absent: bool = False
    universe_generation_id: int | None = None
    universe_generation_closed_at: datetime | None = None
    # Set only by a BACKGROUND pricing refresh. Opportunity Monitor retention
    # uses this clock. HOT and UNIVERSE discovery do not.
    background_priced_at: datetime | None = None


def canonical_current_market_key(row: FixtureMarketInventoryRow) -> str:
    """Stable current-state key. Family/period/line/settlement + source ids, never names.

    Unproven / unsupported rows with no family and no source-market ids fail
    closed: they never key or merge by display text. Callers must disambiguate
    those rows so two partial observations that share a label stay distinct.
    """

    family_id = _family_identity(row)
    sources = _source_identity(row)
    if family_id[0]:
        family, period, line, settlement = family_id
        return f"canon:{family}|{period}|{line}|{settlement}|{sources}"
    if sources:
        return f"source:{sources}"
    return _unproven_identity_stem(row)


def _unproven_identity_stem(row: FixtureMarketInventoryRow) -> str:
    status = (
        row.comparison_status.value
        if hasattr(row.comparison_status, "value")
        else str(row.comparison_status)
    )
    rejections = ",".join(sorted(row.rejection_reasons))
    return f"unproven:{status}|{row.reason or ''}|{rejections}"


def _identity_is_proven(row: FixtureMarketInventoryRow) -> bool:
    family_id = _family_identity(row)
    return bool(family_id[0] or _source_identity(row))


def _family_identity(row: FixtureMarketInventoryRow) -> tuple[str, str, str, str]:
    family = (row.family or "").strip()
    period = (row.period or "").strip()
    line = "" if row.line is None else format(row.line, "f")
    settlement = ""
    for facts in (row.matchbook, row.polymarket, row.kalshi):
        if facts is not None and facts.settlement_key:
            settlement = facts.settlement_key
            break
    return family, period, line, settlement


def _source_identity(row: FixtureMarketInventoryRow) -> str:
    parts: list[str] = []
    for facts in (row.matchbook, row.polymarket, row.kalshi):
        if facts is None:
            continue
        parts.append(f"{facts.venue.value}:{facts.source_market_id}")
    return "|".join(parts)


def _source_set(row: FixtureMarketInventoryRow) -> frozenset[str]:
    identity = _source_identity(row)
    if not identity:
        return frozenset()
    return frozenset(identity.split("|"))


def merge_slot_key(existing: dict[str, CurrentMarketSlot], row: FixtureMarketInventoryRow) -> str:
    """Reuse one slot when source ids overlap the same canonical family identity."""

    exact = canonical_current_market_key(row)
    if not _identity_is_proven(row):
        return _disambiguate_unproven(existing, exact)
    if exact in existing:
        return exact
    family_id = _family_identity(row)
    incoming_sources = _source_set(row)
    if not family_id[0] or not incoming_sources:
        return exact
    matches = [
        key
        for key, slot in existing.items()
        if _family_identity(slot.row) == family_id and incoming_sources & _source_set(slot.row)
    ]
    if len(matches) == 1:
        return matches[0]
    return exact


def _disambiguate_unproven(existing: dict[str, CurrentMarketSlot], stem: str) -> str:
    if stem not in existing:
        return stem
    index = 2
    while f"{stem}#{index}" in existing:
        index += 1
    return f"{stem}#{index}"


def merge_current_market_slots(
    existing: dict[str, CurrentMarketSlot],
    incoming_rows: list[FixtureMarketInventoryRow],
    *,
    scan_lane: ScanLane,
    scanned_at: datetime,
    paper_market_ids: tuple[str, ...],
    evaluated: bool,
    universe_generation_id: int | None = None,
    pricing_refresh: bool = False,
) -> dict[str, CurrentMarketSlot]:
    """Upsert evaluated canonical markets. Partial/not_evaluated work is a no-op.

    An evaluated observation with no comparable rows is explicit absence for
    this identity's current slots: stale proving promotion must not survive
    merely because omission was represented as no row. Older snapshots cannot
    tombstone newer slots. A non-empty evaluated subset still upserts only the
    keys it carries and leaves other-lane families in place.

    HOT/BACKGROUND pricing refreshes may update economics on a still-current
    ApprovedEquivalent slot. They must not tombstone UNIVERSE-proven presence
    via empty rows, and they must not revoke comparison identity merely because
    FX, fees, or quote age failed closed.
    """

    if not evaluated:
        return dict(existing)
    merged = dict(existing)
    scanned = require_aware_instant(scanned_at, "last_scanned_at")
    lane = ScanLane(scan_lane) if not isinstance(scan_lane, ScanLane) else scan_lane
    discovery = _is_universe_discovery(
        lane,
        incoming_generation_id=universe_generation_id,
        pricing_refresh=pricing_refresh,
    )
    for row in incoming_rows:
        key = merge_slot_key(merged, row)
        previous = merged.get(key)
        if previous is not None and not _incoming_supersedes(previous, scanned_at=scanned, scan_lane=lane):
            continue
        merged[key] = CurrentMarketSlot(
            key=key,
            row=_row_preserving_relationship(
                previous,
                row,
                discovery=discovery,
                pricing_refresh=pricing_refresh,
            ),
            scan_lane=lane,
            last_scanned_at=scanned,
            paper_market_ids=paper_market_ids,
            evaluated_absent=False,
            universe_generation_id=_slot_generation_id(
                lane,
                incoming_generation_id=universe_generation_id,
                previous=previous,
                pricing_refresh=pricing_refresh,
            ),
            universe_generation_closed_at=_slot_generation_closed_at(
                lane,
                incoming_generation_id=universe_generation_id,
                previous=previous,
                pricing_refresh=pricing_refresh,
            ),
            background_priced_at=_background_priced_at(
                lane,
                scanned=scanned,
                pricing_refresh=pricing_refresh,
                previous=previous,
            ),
        )
    if not incoming_rows and not pricing_refresh:
        merged = mark_evaluated_absence(
            merged,
            scanned_at=scanned,
            scan_lane=lane,
        )
    return merged


def mark_evaluated_absence(
    existing: dict[str, CurrentMarketSlot],
    *,
    scanned_at: datetime,
    scan_lane: ScanLane,
) -> dict[str, CurrentMarketSlot]:
    """Record identity-level evaluated absence without inventing a market row."""

    updated: dict[str, CurrentMarketSlot] = {}
    for key, slot in existing.items():
        if not _incoming_supersedes(slot, scanned_at=scanned_at, scan_lane=scan_lane):
            updated[key] = slot
            continue
        updated[key] = CurrentMarketSlot(
            key=key,
            row=slot.row,
            scan_lane=scan_lane,
            last_scanned_at=scanned_at,
            paper_market_ids=(),
            evaluated_absent=True,
            universe_generation_id=_slot_generation_id(
                scan_lane,
                incoming_generation_id=None,
                previous=slot,
            ),
            universe_generation_closed_at=_slot_generation_closed_at(
                scan_lane,
                incoming_generation_id=None,
                previous=slot,
            ),
        )
    return updated


def prune_expired_market_slots(
    slots: dict[str, CurrentMarketSlot],
    now: datetime,
    *,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    open_universe_generation_id: int | None = None,
    universe_generation_closed_at_by_id: dict[int, datetime] | None = None,
) -> dict[str, CurrentMarketSlot]:
    evaluated = require_aware_instant(now, "now")
    live: dict[str, CurrentMarketSlot] = {}
    for key, slot in slots.items():
        if not slot_relationship_current(
            slot,
            now=evaluated,
            hot_ttl_seconds=hot_ttl_seconds,
            universe_ttl_seconds=universe_ttl_seconds,
            background_current_state_ttl_seconds=background_current_state_ttl_seconds,
            max_quote_age_ms=max_quote_age_ms,
            open_universe_generation_id=open_universe_generation_id,
            universe_generation_closed_at_by_id=universe_generation_closed_at_by_id,
        ):
            continue
        live[key] = slot
    return live


def slot_relationship_current(
    slot: CurrentMarketSlot,
    *,
    now: datetime,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    open_universe_generation_id: int | None = None,
    universe_generation_closed_at_by_id: dict[int, datetime] | None = None,
) -> bool:
    """True when ApprovedEquivalent / market presence is still discovery-current.

    Quote freshness is a separate clock. A UNIVERSE-stamped relationship stays
    visible while its proving generation is open. After that generation closes,
    expiry is anchored to that proving generation's own close time plus radar
    TTL. A later open generation does not revive or suspend that expiry.
    Unstamped / HOT-only slots keep the historical last_scanned_at lane TTL.
    """

    if slot.evaluated_absent:
        return False
    if _uses_background_current_state_retention(slot):
        priced = require_aware_instant(slot.background_priced_at, "background_priced_at")
        return now < priced + timedelta(seconds=background_current_state_ttl_seconds)
    if slot.universe_generation_id is not None:
        proving = slot.universe_generation_id
        if open_universe_generation_id == proving:
            return True
        closed_at = slot.universe_generation_closed_at
        if closed_at is None and universe_generation_closed_at_by_id:
            closed_at = universe_generation_closed_at_by_id.get(proving)
        if closed_at is None:
            return (
                slot_freshness(
                    slot,
                    now=now,
                    hot_ttl_seconds=hot_ttl_seconds,
                    universe_ttl_seconds=universe_ttl_seconds,
                    max_quote_age_ms=max_quote_age_ms,
                )
                != FRESHNESS_EXPIRED
            )
        expires = require_aware_instant(closed_at, "universe_generation_closed_at") + timedelta(
            seconds=universe_ttl_seconds
        )
        return now < expires
    return (
        slot_freshness(
            slot,
            now=now,
            hot_ttl_seconds=hot_ttl_seconds,
            universe_ttl_seconds=universe_ttl_seconds,
            max_quote_age_ms=max_quote_age_ms,
        )
        != FRESHNESS_EXPIRED
    )


def slot_freshness(
    slot: CurrentMarketSlot,
    *,
    now: datetime,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    **_: object,
) -> str:
    quote_age = effective_quote_age_ms(row_quote_age_ms(slot.row), slot.last_scanned_at, now)
    retention = None
    anchor = slot.last_scanned_at
    if _uses_background_current_state_retention(slot):
        retention = background_current_state_ttl_seconds
        anchor = slot.background_priced_at or slot.last_scanned_at
    return freshness_class(
        lane=slot.scan_lane,
        last_scanned_at=anchor,
        now=now,
        quote_age_ms=quote_age,
        max_quote_age_ms=max_quote_age_ms,
        hot_ttl_seconds=hot_ttl_seconds,
        universe_ttl_seconds=universe_ttl_seconds,
        retention_ttl_seconds=retention,
    )


def stamp_current_market_row(
    slot: CurrentMarketSlot,
    *,
    now: datetime | None,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    **_: object,
) -> FixtureMarketInventoryRow:
    if now is None:
        freshness = FRESHNESS_RADAR_CURRENT
    else:
        freshness = slot_freshness(
            slot,
            now=now,
            hot_ttl_seconds=hot_ttl_seconds,
            universe_ttl_seconds=universe_ttl_seconds,
            background_current_state_ttl_seconds=background_current_state_ttl_seconds,
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

    A comparison is a matched-equivalent canonical family identity (family,
    period, line, settlement) plus each distinct venue pair on that row. Rows
    without pair payloads still count as one comparison. Distinct proven
    settlement keys stay distinct, matching store identity.
    """

    keys: set[tuple[str, str, str, str, str]] = set()
    for row in rows:
        if not inventory_is_comparable_opportunity(row.comparison_status):
            continue
        family, period, line, settlement = _family_identity(row)
        pairs = comparable_venue_pairs(row)
        if not pairs:
            keys.add((family, period, line, settlement, "row"))
            continue
        for left, right in pairs:
            keys.add((family, period, line, settlement, f"{left}|{right}"))
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
    **_: object,
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
            background_current_state_ttl_seconds=int(
                _.get(
                    "background_current_state_ttl_seconds",
                    DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
                )
            ),
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
    background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    open_universe_generation_id: int | None = None,
    universe_generation_closed_at_by_id: dict[int, datetime] | None = None,
):
    """Project merged current markets onto fixture headline fields.

    Relationship counts (discovered / matched / equivalent) include
    generation-current ApprovedEquivalent rows even when quotes are stale.
    Headline edge, qualifying/near bands, solver and paper eligibility use
    only quote-fresh slots.
    """

    del open_universe_generation_id, universe_generation_closed_at_by_id
    ttl = {
        "hot_ttl_seconds": hot_ttl_seconds,
        "universe_ttl_seconds": universe_ttl_seconds,
        "background_current_state_ttl_seconds": background_current_state_ttl_seconds,
        "max_quote_age_ms": max_quote_age_ms,
    }
    relationship = [slot for slot in slots if not slot.evaluated_absent]
    if not relationship:
        return fixture, []
    relationship_rows = sort_fixture_inventory_rows(
        [stamp_current_market_row(slot, now=now, **ttl) for slot in relationship]
    )
    quote_slots = [
        slot
        for slot in relationship
        if now is None or slot_freshness(slot, now=now, **ttl) != FRESHNESS_EXPIRED
    ]
    discovered, matched_rows, _best = inventory_summary(relationship_rows)
    equivalent = equivalent_comparison_count(relationship_rows)
    candidates = [candidate_from_current_slot(slot, now=now, **ttl) for slot in quote_slots]
    qualifying = sum(1 for item in candidates if headline_band_for(item) is HeadlineBand.QUALIFYING)
    near = sum(1 for item in candidates if headline_band_for(item) is HeadlineBand.NEAR_EXECUTABLE)
    headline = select_fixture_headline(candidates)
    quote_rows = [
        row
        for row in relationship_rows
        if now is None or row.radar_freshness != FRESHNESS_EXPIRED
    ]
    winner_row = _headline_row(quote_rows, headline.candidate)
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
    return projected, relationship_rows


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


def stored_row_proves_qualifying_executable(
    row: FixtureMarketInventoryRow,
    *,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
) -> bool:
    """True when stored current-state economics proved a qualifying executable arb.

    Uses observation-time quote age and stored solver/equivalence truth. Does
    not re-apply wall-clock executable freshness: that remains a paper-entry
    gate. Radar TTL decides whether the slot is still current.
    """

    if row.comparison_status is not InventoryComparisonStatus.MATCHED_EQUIVALENT:
        return False
    if not row.entered_solver or not row.solver_is_arbitrage:
        return False
    if set(row.rejection_reasons) & PROMOTION_FAIL_CLOSED_REASONS:
        return False
    if row.current_net_edge is None:
        return False
    if row.trigger_net_edge is not None and not qualifies_min_net_arb(
        row.current_net_edge, row.trigger_net_edge
    ):
        return False
    quote_age = row_quote_age_ms(row)
    if quote_age is None or quote_age >= max_quote_age_ms:
        return False
    venues = [
        facts
        for facts in (row.matchbook, row.polymarket, row.kalshi)
        if facts is not None
    ]
    return len(venues) >= 2


def current_slots_prove_qualifying_opportunity(
    slots: list[CurrentMarketSlot],
    *,
    now: datetime,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    **_: object,
) -> bool:
    """True when a radar-current merged slot still proves a qualifying executable arb."""

    evaluated = require_aware_instant(now, "now")
    for slot in slots:
        if slot.evaluated_absent:
            continue
        # Pure row economics first; radar freshness is the costly clock check.
        if not stored_row_proves_qualifying_executable(slot.row, max_quote_age_ms=max_quote_age_ms):
            continue
        freshness = slot_freshness(
            slot,
            now=evaluated,
            hot_ttl_seconds=hot_ttl_seconds,
            universe_ttl_seconds=universe_ttl_seconds,
            background_current_state_ttl_seconds=background_current_state_ttl_seconds,
            max_quote_age_ms=max_quote_age_ms,
        )
        if freshness != FRESHNESS_EXPIRED:
            return True
    return False


def stored_row_proves_surveillance_opportunity(
    row: FixtureMarketInventoryRow,
    *,
    band_pp: Decimal | None = None,
    minimum_limiting_depth_gbp: Decimal | None = None,
) -> bool:
    """True when merged economics justify HOT without being a paper fill.

    Already-triggered net ROI (`current_net_edge >= trigger_net_edge`) stays
    HOT even when executable quote-age / allocator / risk gates fail. A
    below-trigger approved market is HOT only inside the configured proximity
    band AND when `limiting_depth_gbp` meets the configured minimum. Slightly
    negative edges can qualify. Arbitrary distance, missing depth, gross edge,
    or a hard-coded zero trigger do not promote. Settlement/mapping
    contradictions still fail closed. The depth gate is not a fill gate.
    """

    if not _row_has_comparable_cross_venue_economics(row):
        return False
    current = row.current_net_edge
    trigger = row.trigger_net_edge
    if current is None:
        return False
    if trigger is None:
        return False
    if qualifies_min_net_arb(current, trigger):
        return True
    band, minimum = resolve_hot_proximity_limits(
        band_pp=band_pp,
        minimum_limiting_depth_gbp=minimum_limiting_depth_gbp,
    )
    return promotes_hot_net_proximity(
        current,
        trigger,
        row.limiting_depth_gbp,
        band_pp=band,
        minimum_limiting_depth_gbp=minimum,
    )


def stored_row_proves_net_proximity(
    row: FixtureMarketInventoryRow,
    *,
    band_pp: Decimal | None = None,
    minimum_limiting_depth_gbp: Decimal | None = None,
) -> bool:
    """Below Min Net Arb, inside the HOT band, with enough limiting depth. Not a fill."""

    if not _row_has_comparable_cross_venue_economics(row):
        return False
    if row.current_net_edge is None or row.trigger_net_edge is None:
        return False
    band, minimum = resolve_hot_proximity_limits(
        band_pp=band_pp,
        minimum_limiting_depth_gbp=minimum_limiting_depth_gbp,
    )
    return promotes_hot_net_proximity(
        row.current_net_edge,
        row.trigger_net_edge,
        row.limiting_depth_gbp,
        band_pp=band,
        minimum_limiting_depth_gbp=minimum,
    )


def stored_row_net_proximity_distance_pp(
    row: FixtureMarketInventoryRow,
    *,
    band_pp: Decimal | None = None,
    minimum_limiting_depth_gbp: Decimal | None = None,
) -> Decimal | None:
    if not stored_row_proves_net_proximity(
        row,
        band_pp=band_pp,
        minimum_limiting_depth_gbp=minimum_limiting_depth_gbp,
    ):
        return None
    assert row.current_net_edge is not None
    assert row.trigger_net_edge is not None
    return distance_to_trigger_pp(row.current_net_edge, row.trigger_net_edge)


def _row_has_comparable_cross_venue_economics(row: FixtureMarketInventoryRow) -> bool:
    if not inventory_is_comparable_opportunity(row.comparison_status):
        return False
    if set(row.rejection_reasons) & _MAPPING_INCOMPATIBLE:
        return False
    venues = [
        facts
        for facts in (row.matchbook, row.polymarket, row.kalshi)
        if facts is not None
    ]
    return len(venues) >= 2


def current_slots_prove_surveillance_opportunity(
    slots: list[CurrentMarketSlot],
    *,
    now: datetime,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    hot_proximity_band_pp: Decimal | None = None,
    hot_minimum_limiting_depth_gbp: Decimal | None = None,
    **_: object,
) -> bool:
    """True when a radar-current slot is triggered or inside the HOT proximity gate."""

    evaluated = require_aware_instant(now, "now")
    band, minimum = resolve_hot_proximity_limits(
        band_pp=hot_proximity_band_pp,
        minimum_limiting_depth_gbp=hot_minimum_limiting_depth_gbp,
    )
    for slot in slots:
        if slot.evaluated_absent:
            continue
        # Pure row economics first; radar freshness is the costly clock check.
        if not stored_row_proves_surveillance_opportunity(
            slot.row,
            band_pp=band,
            minimum_limiting_depth_gbp=minimum,
        ):
            continue
        freshness = slot_freshness(
            slot,
            now=evaluated,
            hot_ttl_seconds=hot_ttl_seconds,
            universe_ttl_seconds=universe_ttl_seconds,
            background_current_state_ttl_seconds=background_current_state_ttl_seconds,
            max_quote_age_ms=max_quote_age_ms,
        )
        if freshness != FRESHNESS_EXPIRED:
            return True
    return False


def current_slots_prove_net_proximity(
    slots: list[CurrentMarketSlot],
    *,
    now: datetime,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    hot_proximity_band_pp: Decimal | None = None,
    hot_minimum_limiting_depth_gbp: Decimal | None = None,
    **_: object,
) -> bool:
    """True when any radar-current approved market passes the HOT proximity gate."""

    return current_slots_net_proximity_distance_pp(
        slots,
        now=now,
        hot_ttl_seconds=hot_ttl_seconds,
        universe_ttl_seconds=universe_ttl_seconds,
        background_current_state_ttl_seconds=background_current_state_ttl_seconds,
        max_quote_age_ms=max_quote_age_ms,
        hot_proximity_band_pp=hot_proximity_band_pp,
        hot_minimum_limiting_depth_gbp=hot_minimum_limiting_depth_gbp,
    ) is not None


def current_slots_net_proximity_distance_pp(
    slots: list[CurrentMarketSlot],
    *,
    now: datetime,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
    background_current_state_ttl_seconds: int = DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    hot_proximity_band_pp: Decimal | None = None,
    hot_minimum_limiting_depth_gbp: Decimal | None = None,
    **_: object,
) -> Decimal | None:
    """Closest in-band distance among radar-current approved markets, else None."""

    evaluated = require_aware_instant(now, "now")
    closest: Decimal | None = None
    band, minimum = resolve_hot_proximity_limits(
        band_pp=hot_proximity_band_pp,
        minimum_limiting_depth_gbp=hot_minimum_limiting_depth_gbp,
    )
    for slot in slots:
        if slot.evaluated_absent:
            continue
        freshness = slot_freshness(
            slot,
            now=evaluated,
            hot_ttl_seconds=hot_ttl_seconds,
            universe_ttl_seconds=universe_ttl_seconds,
            background_current_state_ttl_seconds=background_current_state_ttl_seconds,
            max_quote_age_ms=max_quote_age_ms,
        )
        if freshness == FRESHNESS_EXPIRED:
            continue
        distance = stored_row_net_proximity_distance_pp(
            slot.row,
            band_pp=band,
            minimum_limiting_depth_gbp=minimum,
        )
        if distance is None:
            continue
        if closest is None or distance < closest:
            closest = distance
    return closest


def row_quote_age_ms(row: FixtureMarketInventoryRow) -> int | None:
    ages = [
        facts.quote_age_ms
        for facts in (row.matchbook, row.polymarket, row.kalshi)
        if facts is not None and facts.quote_age_ms is not None
    ]
    if not ages:
        return None
    return max(ages)


def _uses_background_current_state_retention(slot: CurrentMarketSlot) -> bool:
    """BACKGROUND pricing keeps its own monitor clock. HOT does not borrow it."""

    return slot.scan_lane is not ScanLane.HOT and slot.background_priced_at is not None


def _background_priced_at(
    scan_lane: ScanLane,
    *,
    scanned: datetime,
    pricing_refresh: bool,
    previous: CurrentMarketSlot | None,
) -> datetime | None:
    """Stamp a BACKGROUND price. Discovery and HOT refresh clear that clock."""

    if pricing_refresh and scan_lane is ScanLane.UNIVERSE:
        return scanned
    if scan_lane is ScanLane.HOT:
        return None
    if scan_lane is ScanLane.UNIVERSE and not pricing_refresh:
        return None
    if previous is not None:
        return previous.background_priced_at
    return None


def _is_universe_discovery(
    scan_lane: ScanLane,
    *,
    incoming_generation_id: int | None,
    pricing_refresh: bool,
) -> bool:
    """True only for a real UNIVERSE catalogue evaluation, not HOT/BACKGROUND pricing."""

    del incoming_generation_id
    return scan_lane is ScanLane.UNIVERSE and not pricing_refresh


def _row_preserving_relationship(
    previous: CurrentMarketSlot | None,
    incoming: FixtureMarketInventoryRow,
    *,
    discovery: bool,
    pricing_refresh: bool,
) -> FixtureMarketInventoryRow:
    """Pricing may refresh economics; it is not relationship authority.

    Ordinary HOT/UNIVERSE evaluations may authoritatively replace an existing
    relationship. Only a price-engine refresh preserves the previously proved
    comparable status when its reconstructed row is non-comparable.
    """

    if discovery or previous is None or not pricing_refresh:
        return incoming
    if not inventory_is_comparable_opportunity(previous.row.comparison_status):
        return incoming
    if inventory_is_comparable_opportunity(incoming.comparison_status):
        return incoming
    return incoming.model_copy(update={"comparison_status": previous.row.comparison_status})


def _slot_generation_id(
    scan_lane: ScanLane,
    *,
    incoming_generation_id: int | None,
    previous: CurrentMarketSlot | None,
    pricing_refresh: bool = False,
) -> int | None:
    """UNIVERSE evaluations stamp the generation; pricing refreshes keep the last UNIVERSE id."""

    if (
        scan_lane is ScanLane.UNIVERSE
        and not pricing_refresh
        and incoming_generation_id is not None
    ):
        return incoming_generation_id
    if previous is not None:
        return previous.universe_generation_id
    return incoming_generation_id


def _slot_generation_closed_at(
    scan_lane: ScanLane,
    *,
    incoming_generation_id: int | None,
    previous: CurrentMarketSlot | None,
    pricing_refresh: bool = False,
) -> datetime | None:
    """A UNIVERSE re-evaluation starts a new proving lifecycle; pricing keeps the old close."""

    if _is_universe_discovery(
        scan_lane,
        incoming_generation_id=incoming_generation_id,
        pricing_refresh=pricing_refresh,
    ):
        return None
    if previous is not None:
        return previous.universe_generation_closed_at
    return None


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

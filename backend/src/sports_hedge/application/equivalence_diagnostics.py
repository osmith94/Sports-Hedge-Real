"""Inspectable reasons when a multi-venue fixture has zero equivalents.

Does not change matcher or catalogue admission. Inventory MATCHED_EQUIVALENT
remains the operational settlement-equivalent count.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
)
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.matching.ordinary_1x2 import GAMEWIN_ORDINARY_1X2_AUDIT_REASON
from sports_hedge.normalization.kalshi_contract_terms import GAMEWIN_SCOPE_UNAVAILABLE_REASON

NO_FAMILY_OVERLAP = "no_normalized_market_family_overlap"
MATCHER_MISMATCH = "market_matcher_mismatch"
SETTLEMENT_FINGERPRINT_MISMATCH = "settlement_fingerprint_mismatch"
CATALOGUE_REVIEW_REQUIRED = "catalogue_review_required"
CATALOGUE_UNSUPPORTED = "catalogue_unsupported"
CATALOGUE_PARAMETER_MISMATCH = "catalogue_parameter_mismatch"
CATALOGUE_CONTRADICTION = "catalogue_contradiction"
MARKET_SPECIFIC_RULES_MISSING = "market_specific_rules_missing"
ORDER_BOOK_UNAVAILABLE = "order_book_unavailable"
NO_SETTLEMENT_EQUIVALENT = "no_settlement_equivalent_market_pair"

PRESERVED_REASONS = frozenset(
    {
        ORDER_BOOK_UNAVAILABLE,
        "list_markets_unavailable",
        "insufficient_enabled_venues",
        "event_identity_mismatch",
        "not_evaluated_scan_deadline",
        "unmatched / no supported Polymarket coverage",
        "series_not_queried",
    }
)

_FAMILY_MISMATCH_TOKENS = frozenset(
    {"market_family_mismatch", "unsupported_family", "one_side_outside_census_v1_catalogue"}
)
_PARAM_TOKENS = frozenset(
    {"period_mismatch", "line_mismatch", "catalogue_approved_parameter_mismatch"}
)
_CONTRADICTION_TOKENS = frozenset(
    {
        "settlement_mismatch",
        "settlement_key_mismatch",
        "catalogue_known_contradiction",
        "ftts_no_goal_contract_mismatch",
        "outcome_space_mismatch",
    }
)
_REVIEW_TOKENS = frozenset(
    {
        "catalogue_review_required",
        "incomplete_settlement",
        "plausible_archetype_incomplete_evidence",
        "incomplete_outcome_set",
        "match_result_outcome_space_incomplete_or_mismatched",
    }
)


def zero_equivalent_reason_from_inventory(
    rows: list[FixtureMarketInventoryRow],
    *,
    existing_reason: str | None = None,
) -> str:
    """Return the dominant inspectable reason for a zero-equivalent fixture."""

    existing = str(existing_reason or "").strip()
    if existing in PRESERVED_REASONS:
        return existing

    families_by_venue: dict[str, set[str]] = {}
    tokens: list[str] = []
    paired = 0
    for row in rows:
        if row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT:
            continue
        venue_facts = (
            ("matchbook", row.matchbook),
            ("polymarket", row.polymarket),
            ("kalshi", row.kalshi),
        )
        present = [name for name, facts in venue_facts if facts is not None]
        if len(present) >= 2:
            paired += 1
        for name, facts in venue_facts:
            if facts is None:
                continue
            family = str(facts.family or "").strip()
            if family:
                families_by_venue.setdefault(name, set()).add(family)
        tokens.extend(_row_tokens(row))

    if MARKET_SPECIFIC_RULES_MISSING in tokens or GAMEWIN_SCOPE_UNAVAILABLE_REASON in tokens:
        return MARKET_SPECIFIC_RULES_MISSING
    if GAMEWIN_ORDINARY_1X2_AUDIT_REASON in tokens and any(
        token in tokens for token in _REVIEW_TOKENS
    ):
        return MARKET_SPECIFIC_RULES_MISSING
    if f"catalogue_{CatalogueApprovalState.KNOWN_CONTRADICTION.value}" in tokens:
        return CATALOGUE_CONTRADICTION
    if any(token in tokens for token in _CONTRADICTION_TOKENS) or (
        InventoryComparisonStatus.SETTLEMENT_MISMATCH.value in tokens
    ):
        return SETTLEMENT_FINGERPRINT_MISMATCH
    if any(token in tokens for token in _PARAM_TOKENS) or (
        f"catalogue_{CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH.value}" in tokens
    ):
        return CATALOGUE_PARAMETER_MISMATCH
    if f"catalogue_{CatalogueApprovalState.UNSUPPORTED.value}" in tokens:
        return CATALOGUE_UNSUPPORTED
    if any(token in tokens for token in _REVIEW_TOKENS) or (
        f"catalogue_{CatalogueApprovalState.REVIEW_REQUIRED.value}" in tokens
    ):
        return CATALOGUE_REVIEW_REQUIRED
    if _no_family_overlap(families_by_venue) or any(token in tokens for token in _FAMILY_MISMATCH_TOKENS):
        return NO_FAMILY_OVERLAP
    if paired and tokens:
        return MATCHER_MISMATCH
    if existing:
        return existing
    return NO_SETTLEMENT_EQUIVALENT


def zero_equivalent_reason_counts(
    fixtures: list[Any],
    fixture_markets: dict[str, list[FixtureMarketInventoryRow]] | None = None,
) -> dict[str, int]:
    """Histogram of inspectable zero-equivalent reasons for multi-venue fixtures."""

    markets = fixture_markets or {}
    counts: Counter[str] = Counter()
    for fixture in fixtures:
        venues = sum(
            bool(flag)
            for flag in (
                getattr(fixture, "matchbook_matched", False),
                getattr(fixture, "polymarket_matched", False),
                getattr(fixture, "kalshi_matched", False),
            )
        )
        if venues < 2:
            continue
        if (getattr(fixture, "matched_equivalent_count", None) or 0) != 0:
            continue
        canonical_id = str(getattr(fixture, "canonical_event_id", "") or "")
        reason = zero_equivalent_reason_from_inventory(
            list(markets.get(canonical_id) or []),
            existing_reason=getattr(fixture, "no_comparison_reason", None),
        )
        counts[reason] += 1
    return dict(sorted(counts.items()))


def _row_tokens(row: FixtureMarketInventoryRow) -> list[str]:
    tokens = [str(row.comparison_status.value)]
    if row.reason:
        tokens.append(str(row.reason))
    tokens.extend(str(item) for item in row.rejection_reasons)
    tokens.extend(str(item) for item in row.match_reasons)
    return tokens


def _no_family_overlap(families_by_venue: dict[str, set[str]]) -> bool:
    present = [families for families in families_by_venue.values() if families]
    if len(present) < 2:
        return bool(families_by_venue)
    overlap = set.intersection(*present)
    return not overlap

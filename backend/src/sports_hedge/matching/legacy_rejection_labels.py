"""Historical diagnostic strings that older scans stored as rejection reasons.

New evaluations must not emit these as rejections. The only runtime use is
``persisted_rejection_reasons``, applied when a stored opportunity is read.
Live scanner, watchlist, and Price-2 decisions do not consult this set.
"""

from __future__ import annotations

LEGACY_DIAGNOSTIC_REJECTION_LABELS = frozenset(
    {
        "paper_assumed_equivalent",
        "PAPER_ASSUMED_EQUIVALENT",
        "registered_equivalent_settlement_not_independently_proven",
        "settlement_assumption=normal_full_game_completion",
        "settlement_assumption=regulation_time",
        "mlb_settlement_equivalence_not_proven",
        "owner_approved_nfl_paper_normal_completion",
        "owner_approved_nba_paper_normal_completion",
        "exceptional_settlement_mismatch_possible",
        "paper_assumed_not_live_execution_eligible",
        "paper_mode_only_not_live_execution_eligible",
        "nfl_paper_not_live_execution_equivalent",
        "nba_paper_not_live_execution_equivalent",
        "ncaab_paper_not_live_execution_equivalent",
        "register_paper_admitted_not_live_execution",
        "paper_assumed_equivalent_not_settlement_proven",
    }
)

# Compatibility name. Not an active decision filter.
PAPER_NONBLOCKING_REJECTION_REASONS = LEGACY_DIAGNOSTIC_REJECTION_LABELS


def persisted_rejection_reasons(reasons: list[str]) -> list[str]:
    """Drop historical diagnostic labels when reading a stored rejection list."""

    return [reason for reason in reasons if reason not in LEGACY_DIAGNOSTIC_REJECTION_LABELS]

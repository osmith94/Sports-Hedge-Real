"""LEGACY stored PAPER admission labels.

Wave 1B made an Approved Match Register relationship catalogue-eligible for
Real execution. Older rows, match reasons, and inventory statuses may still
store the strings below. Readers translate them to the registered-equivalent
concept. They are not an independent execution veto.

The wire value ``paper_assumed_equivalent`` is still emitted. It is the
catalogue/inventory status used by the operator UI and by in-memory coverage
reports. Replacing that token would be a stored-contract change with no
database migration in this cleanup, so emission stays and this module is the
translation boundary.

New runtime audit text must not emit ``*_not_live_execution*`` reasons.
"""

from __future__ import annotations

LEGACY_STORED_REGISTERED_EQUIVALENT = "paper_assumed_equivalent"
SEMANTIC_REGISTERED_EQUIVALENT = "registered_equivalent"

LEGACY_NOT_LIVE_EXECUTION_LABELS = frozenset(
    {
        "paper_assumed_not_live_execution_eligible",
        "paper_mode_only_not_live_execution_eligible",
        "nfl_paper_not_live_execution_equivalent",
        "nba_paper_not_live_execution_equivalent",
        "ncaab_paper_not_live_execution_equivalent",
        "register_paper_admitted_not_live_execution",
        "paper_assumed_equivalent_not_settlement_proven",
    }
)


def is_legacy_paper_execution_label(value: str) -> bool:
    text = str(value or "").strip()
    return text in LEGACY_NOT_LIVE_EXECUTION_LABELS or text in {
        LEGACY_STORED_REGISTERED_EQUIVALENT,
        "PAPER_ASSUMED_EQUIVALENT",
    }


def semantic_admission_label(stored: str) -> str:
    """Map a stored catalogue/inventory token onto the Real admission concept."""

    text = str(stored or "").strip()
    if text in {LEGACY_STORED_REGISTERED_EQUIVALENT, "PAPER_ASSUMED_EQUIVALENT"}:
        return SEMANTIC_REGISTERED_EQUIVALENT
    return text


def legacy_label_blocks_real_execution(value: str) -> bool:
    """Historical paper wording never independently vetoes Real catalogue eligibility."""

    if not is_legacy_paper_execution_label(value):
        raise ValueError(f"not a legacy paper admission label: {value}")
    return False

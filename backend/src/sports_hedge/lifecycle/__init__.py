"""Explicit Sports Hedge lifecycle contracts.

Phase 1 paper-only. These modules validate allowed UNIVERSE generation/chunk
and PAPER trade/settlement transitions without replacing working coordinators.
"""

from sports_hedge.lifecycle.decisions import (
    IllegalLifecycleTransition,
    LifecycleAuditLog,
    LifecycleDecision,
    require_accepted,
)
from sports_hedge.lifecycle.paper import (
    ACTIVE_TRADE_STATES,
    DISCOVERY_EVICTION_MUST_NOT_MUTATE,
    SETTLEABLE_TRADE_STATES,
    decide_active_trade_membership,
    decide_paper_fill,
    decide_paper_settlement,
    decide_paper_trade_transition,
    decide_paper_unwind,
)
from sports_hedge.lifecycle.universe import (
    UniverseChunkPhase,
    UniverseGenerationPhase,
    decide_chunk_transition,
    decide_generation_transition,
    decide_stale_chunk_callback,
    decide_sweep_transition,
    generation_phase,
)

__all__ = [
    "ACTIVE_TRADE_STATES",
    "DISCOVERY_EVICTION_MUST_NOT_MUTATE",
    "IllegalLifecycleTransition",
    "LifecycleAuditLog",
    "LifecycleDecision",
    "SETTLEABLE_TRADE_STATES",
    "UniverseChunkPhase",
    "UniverseGenerationPhase",
    "decide_active_trade_membership",
    "decide_chunk_transition",
    "decide_generation_transition",
    "decide_paper_fill",
    "decide_paper_settlement",
    "decide_paper_trade_transition",
    "decide_paper_unwind",
    "decide_stale_chunk_callback",
    "decide_sweep_transition",
    "generation_phase",
    "require_accepted",
]

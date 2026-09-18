"""Pairwise Issue #268 approval matrix.

Cells are the operational catalogue verdict when required settlement evidence is
present. Sibling REVIEW_REQUIRED / UNSUPPORTED cases are listed per cell and
locked by the fixture corpus; they do not silently become APPROVED_EQUIVALENT.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from sports_hedge.catalogue.states import CatalogueApprovalState, CatalogueArchetype

CENSUS_MATRIX_VERSION = "v1"
CENSUS_PARENT_COMMIT = "620d1807473a5bfaa08a5023d2a28f4da756efe3"

# No versioned Matchbook↔Kalshi assumption currently proves that Kalshi
# cancellation/reschedule-to-fair-price is economically compatible with
# Matchbook regulation 1X2. Matchbook payloads have no resolution-rule text;
# `_standard_football_settlement` does not model this rule. Absence of
# Matchbook wording is not compatibility (Core Tenets 03 and 20).
MATCHBOOK_KALSHI_CANCEL_RESCHEDULE_FAIR_PRICE_ASSUMPTION_ID = None
MATCHBOOK_KALSHI_CANCEL_RESCHEDULE_FAIR_PRICE_COMPATIBLE = False
MATCHBOOK_KALSHI_CANCEL_RESCHEDULE_FAIR_PRICE_REASON = (
    "No versioned Matchbook↔Kalshi assumption proves cancellation/"
    "reschedule-to-fair-price economically compatible. Do not invent "
    "Matchbook void/postponement from absent rule text."
)


class PairwiseCell(BaseModel):
    archetype: CatalogueArchetype
    venue_pair: str
    state: CatalogueApprovalState
    solver_path: str
    reason: str
    sibling_states: list[str] = Field(default_factory=list)


PAIRWISE_MATRIX: tuple[PairwiseCell, ...] = (
    PairwiseCell(
        archetype=CatalogueArchetype.MATCH_RESULT_1X2,
        venue_pair="matchbook_polymarket",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="simple_complete_set",
        reason=(
            "Matchbook documented full-time Match Odds convention plus Polymarket "
            "parsed 90-minute regulation wording; exhaustive HOME/DRAW/AWAY."
        ),
        sibling_states=[
            "REVIEW_REQUIRED: Polymarket missing/unparsed settlement wording",
            "REVIEW_REQUIRED: lone Polymarket moneyline binary is not 3-way 1X2",
            "APPROVED_PARAMETER_MISMATCH: first-half vs full-time",
            "KNOWN_CONTRADICTION: extra-time/penalties vs regulation",
        ],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.MATCH_RESULT_1X2,
        venue_pair="matchbook_kalshi",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="simple_complete_set",
        reason=(
            "Approved only when Kalshi Get Market or nested rules prove regulation "
            "time. GAMEWIN-unknown 1X2 is REVIEW_REQUIRED in this catalogue even "
            "though MarketMatcher currently admits that pair into the solver."
        ),
        sibling_states=[
            "REVIEW_REQUIRED: Kalshi GAMEWIN result-scope placeholder unavailable",
            "REVIEW_REQUIRED: Kalshi cancellation/reschedule-to-fair-price unmodelled; no pairwise assumption",
            "KNOWN_CONTRADICTION: proven extra-time/penalties vs Matchbook regulation",
            "UNSUPPORTED: To Qualify / two-way books",
        ],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.MATCH_RESULT_1X2,
        venue_pair="kalshi_polymarket",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="simple_complete_set",
        reason=(
            "Approved only when both Kalshi and Polymarket fingerprints are "
            "economically complete regulation 1X2. The GAMEWIN unknown-settlement "
            "allowance is Matchbook↔Kalshi only."
        ),
        sibling_states=[
            "REVIEW_REQUIRED: either venue unknown/incomplete settlement",
            "REVIEW_REQUIRED: Polymarket binary moneyline vs Kalshi 3-way",
        ],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
        venue_pair="matchbook_polymarket",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="simple_complete_set",
        reason="Complete YES/NO regulation BTTS; simple complete-set solver.",
        sibling_states=["REVIEW_REQUIRED: missing Yes/No or unknown Polymarket wording"],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
        venue_pair="matchbook_kalshi",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="simple_complete_set",
        reason=(
            "Approved when Kalshi market rules prove regulation. Generic "
            "'See contract URL.' stays REVIEW_REQUIRED. Get Market enrichment is "
            "Match Result only; BTTS does not inherit SOCCERANYGOAL extra-time default."
        ),
        sibling_states=["REVIEW_REQUIRED: Kalshi ambiguous/missing BTTS rules"],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
        venue_pair="kalshi_polymarket",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="simple_complete_set",
        reason="Approved when both YES/NO fingerprints are complete regulation BTTS.",
        sibling_states=["REVIEW_REQUIRED: either venue incomplete settlement"],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
        venue_pair="matchbook_polymarket",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="simple_complete_set",
        reason="Exact half-line (e.g. 2.5) Over/Under; no push; simple complete-set.",
        sibling_states=[
            "APPROVED_PARAMETER_MISMATCH: 2.5 vs 3.5",
            "UNSUPPORTED: team/participant total vs match total",
        ],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
        venue_pair="matchbook_kalshi",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="simple_complete_set",
        reason=(
            "Approved when Kalshi half-line Over/Under rules prove regulation. "
            "Integer/quarter Kalshi totals are not this cell."
        ),
        sibling_states=[
            "REVIEW_REQUIRED: Kalshi totals wording incomplete",
            "UNSUPPORTED: Kalshi named-team totals are not inferred as match totals",
        ],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
        venue_pair="kalshi_polymarket",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="simple_complete_set",
        reason="Approved when both half-line Over/Under fingerprints are complete.",
        sibling_states=["APPROVED_PARAMETER_MISMATCH: different lines"],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TOTAL_GOALS_INTEGER,
        venue_pair="matchbook_polymarket",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="generalized_payoff",
        reason="Integer line Over/Under with modelled push/void; generalized payoff.",
        sibling_states=["REVIEW_REQUIRED: quarter/split lines (push unknown)"],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TOTAL_GOALS_INTEGER,
        venue_pair="matchbook_kalshi",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason=(
            "Kalshi integer/quarter Total Goals remain deferred until push/refund "
            "rules are proven. No CanonicalMarket is assembled."
        ),
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TOTAL_GOALS_INTEGER,
        venue_pair="kalshi_polymarket",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="Kalshi does not normalize integer totals; pair cannot be approved.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
        venue_pair="matchbook_polymarket",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="generalized_payoff",
        reason=(
            "HOME/AWAY/NO_GOAL regulation-time contract; generalized payoff. "
            "Missing NO_GOAL is not equivalent."
        ),
        sibling_states=[
            "REVIEW_REQUIRED: both sides missing NO_GOAL (void/0-0 unproven)",
            "KNOWN_CONTRADICTION: one side has NO_GOAL, the other does not",
            "KNOWN_CONTRADICTION: extra time vs regulation",
            "UNSUPPORTED: player first-goalscorer / next-goal",
        ],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
        venue_pair="matchbook_kalshi",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="generalized_payoff",
        reason=(
            "Kalshi assembles FTTS only with HOME/AWAY/NO_GOAL and proven "
            "regulation-time rules. Incomplete Kalshi FTTS is REVIEW_REQUIRED, "
            "not a 2-way CanonicalMarket."
        ),
        sibling_states=[
            "REVIEW_REQUIRED: Kalshi FTTS without proven regulation wording",
            "REVIEW_REQUIRED: Kalshi missing a NO_GOAL contract",
        ],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
        venue_pair="kalshi_polymarket",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="generalized_payoff",
        reason="Approved when both sides are complete 3-state regulation FTTS.",
        sibling_states=["REVIEW_REQUIRED: Polymarket unknown settlement or missing NO_GOAL"],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TEAM_TOTAL_GOALS,
        venue_pair="matchbook_polymarket",
        state=CatalogueApprovalState.REVIEW_REQUIRED,
        solver_path="none",
        reason=(
            "Matchbook and Polymarket can recognize TEAM_TOTAL, but CanonicalMarket "
            "does not extract the named-team parameter and the solver cannot model "
            "team totals. Do not invent operational approval."
        ),
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TEAM_TOTAL_GOALS,
        venue_pair="matchbook_kalshi",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="Kalshi team/participant totals are not inferred as match or team totals.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TEAM_TOTAL_GOALS,
        venue_pair="kalshi_polymarket",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="Kalshi does not normalize team totals.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.HANDICAP,
        venue_pair="matchbook_polymarket",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason=(
            "Asian Handicap is recognized on Matchbook/Polymarket but handicap "
            "semantics remain unproven and solver-ineligible."
        ),
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.HANDICAP,
        venue_pair="matchbook_kalshi",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="Kalshi Asian Handicap is not inferred from titles.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.HANDICAP,
        venue_pair="kalshi_polymarket",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="Kalshi does not normalize handicap contracts.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.DRAW_NO_BET,
        venue_pair="matchbook_polymarket",
        state=CatalogueApprovalState.APPROVED_EQUIVALENT,
        solver_path="generalized_payoff",
        reason=(
            "HOME/AWAY with proven draw-void (push_possible True) and complete "
            "regulation fingerprints; generalized payoff already models draw."
        ),
        sibling_states=["REVIEW_REQUIRED: unknown draw-void wording"],
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.DRAW_NO_BET,
        venue_pair="matchbook_kalshi",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="Kalshi Draw No Bet remains deferred until draw-refund rules are proven.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.DRAW_NO_BET,
        venue_pair="kalshi_polymarket",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="Kalshi Draw No Bet is not assembled.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.DOUBLE_CHANCE,
        venue_pair="matchbook_polymarket",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason=(
            "Matchbook recognizes Double Chance. Polymarket has no Double Chance "
            "recogniser. No solver model. Do not invent support."
        ),
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.DOUBLE_CHANCE,
        venue_pair="matchbook_kalshi",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="Kalshi Double Chance is not recognized.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.DOUBLE_CHANCE,
        venue_pair="kalshi_polymarket",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="Kalshi Double Chance is not recognized.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TEAM_TO_SCORE,
        venue_pair="matchbook_kalshi",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="No distinct Team To Score recogniser (not First Team To Score).",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TEAM_TO_SCORE,
        venue_pair="matchbook_polymarket",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="No distinct Team To Score recogniser or solver model.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TEAM_TO_SCORE,
        venue_pair="kalshi_polymarket",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="No distinct Team To Score recogniser or solver model.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TEAM_CLEAN_SHEET,
        venue_pair="matchbook_kalshi",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="No Team Clean Sheet recogniser.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TEAM_CLEAN_SHEET,
        venue_pair="matchbook_polymarket",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="No Team Clean Sheet recogniser.",
    ),
    PairwiseCell(
        archetype=CatalogueArchetype.TEAM_CLEAN_SHEET,
        venue_pair="kalshi_polymarket",
        state=CatalogueApprovalState.UNSUPPORTED,
        solver_path="none",
        reason="No Team Clean Sheet recogniser.",
    ),
)


def matrix_cell(archetype: CatalogueArchetype, venue_pair: str) -> PairwiseCell:
    for cell in PAIRWISE_MATRIX:
        if cell.archetype is archetype and cell.venue_pair == venue_pair:
            return cell
    raise KeyError(f"No census matrix cell for {archetype} {venue_pair}")


def render_matrix_markdown() -> str:
    lines = [
        "| Archetype | Matchbook↔Kalshi | Matchbook↔Polymarket | Kalshi↔Polymarket |",
        "|---|---|---|---|",
    ]
    order = (
        CatalogueArchetype.MATCH_RESULT_1X2,
        CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
        CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
        CatalogueArchetype.TOTAL_GOALS_INTEGER,
        CatalogueArchetype.FIRST_TEAM_TO_SCORE,
        CatalogueArchetype.TEAM_TOTAL_GOALS,
        CatalogueArchetype.HANDICAP,
        CatalogueArchetype.DRAW_NO_BET,
        CatalogueArchetype.DOUBLE_CHANCE,
        CatalogueArchetype.TEAM_TO_SCORE,
        CatalogueArchetype.TEAM_CLEAN_SHEET,
    )
    labels = {
        CatalogueArchetype.MATCH_RESULT_1X2: "Match Result / 1X2",
        CatalogueArchetype.BOTH_TEAMS_TO_SCORE: "Both Teams To Score",
        CatalogueArchetype.TOTAL_GOALS_HALF_LINE: "Total Goals O/U (half-line)",
        CatalogueArchetype.TOTAL_GOALS_INTEGER: "Total Goals O/U (integer)",
        CatalogueArchetype.FIRST_TEAM_TO_SCORE: "First Team To Score",
        CatalogueArchetype.TEAM_TOTAL_GOALS: "Team Total Goals O/U",
        CatalogueArchetype.HANDICAP: "Handicap",
        CatalogueArchetype.DRAW_NO_BET: "Draw No Bet",
        CatalogueArchetype.DOUBLE_CHANCE: "Double Chance",
        CatalogueArchetype.TEAM_TO_SCORE: "Team To Score",
        CatalogueArchetype.TEAM_CLEAN_SHEET: "Team Clean Sheet",
    }
    for archetype in order:
        cells = {
            cell.venue_pair: cell.state.value.upper()
            for cell in PAIRWISE_MATRIX
            if cell.archetype is archetype
        }
        lines.append(
            "| "
            + " | ".join(
                [
                    labels[archetype],
                    cells["matchbook_kalshi"],
                    cells["matchbook_polymarket"],
                    cells["kalshi_polymarket"],
                ]
            )
            + " |"
        )
    return "\n".join(lines)

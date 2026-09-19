"""Issue #316 versioned catalogue truth table.

This is the single source of truth consumed by UNIVERSE recognition/routing,
HOT, catalogue classification, diagnostics/UI coverage, and capture/replay.

Layers are separate on purpose. No archetype becomes live-execution eligible
just because it is in the Tenet-20 product catalogue.

    TARGET_ARCHETYPE           in the Tenet-20 football catalogue
    PHASE1_FOUR_FAMILY         owner-live Matchbook↔Kalshi acceptance target
    VENUE_AVAILABLE            this venue actually offers / we can recognize it
    SETTLEMENT_PROVEN          settlement can be proven when instance wording is complete
    SOLVER_SUPPORTED           Sports Hedge can model the settlement states
    OPERATIONAL_APPROVED       pairwise APPROVED_EQUIVALENT is possible when instance
                               evidence is independently complete
    PAPER_ASSUMED_OPERATIONAL  Matchbook↔Kalshi locked four-family paper-mode assumption

Phase-1 owner-live operationalises four families only:

    MATCH_RESULT / 1X2   PAPER_ASSUMED_EQUIVALENT when GAME HOME/DRAW/AWAY is
                         complete; Kalshi fair-price wording does not block PAPER
    BTTS                 PAPER_ASSUMED_EQUIVALENT when YES/NO identity matches;
                         APPROVED_EQUIVALENT when independently proven
    TOTAL_GOALS          exact safe half-line; PAPER_ASSUMED when identity matches;
                         APPROVED_EQUIVALENT when independently proven
    FTTS                 PAPER_ASSUMED_EQUIVALENT when HOME/AWAY/NO_GOAL identity
                         matches; APPROVED_EQUIVALENT when independently proven

DNB / handicap / double chance / team total / team-to-score / clean sheet stay
explicit in diagnostics as deferred / unsupported / venue-unavailable. They must
not trigger settlement enrichment, depth, solver, or HOT promotion.

PAPER_ASSUMED_EQUIVALENT is paper-mode only. It is never live-execution eligible
and is never represented as independently proven settlement. Cross-venue
equivalence for the four locked families is an owner-approved PAPER product
assumption (Issue #326). The scanner must not re-litigate settlement text on
every scan.

Data class: deterministic registry + cited captured/public read-only metadata.
Not owner-live quotes. Not modelled probabilities. PAPER / execution disabled.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

from sports_hedge.catalogue.states import CatalogueApprovalState, CatalogueArchetype
from sports_hedge.domain.football import MarketFamily

REGISTRY_VERSION = "v4"
REGISTRY_ISSUE = 326
REGISTRY_PARENT_COMMIT = "cf541aa857b19efb2d96cce54489c916bc1a38ba"
REGISTRY_SHARED_BY = ("hot", "universe")
DATA_CLASS = "deterministic_registry"
SETTLEMENT_ASSUMPTION_REGULATION_TIME = "regulation_time"

VENUE_MATCHBOOK = "matchbook"
VENUE_KALSHI = "kalshi"
VENUE_POLYMARKET = "polymarket"

VENUE_PAIR_MATCHBOOK_KALSHI = "matchbook_kalshi"
VENUE_PAIR_MATCHBOOK_POLYMARKET = "matchbook_polymarket"
VENUE_PAIR_KALSHI_POLYMARKET = "kalshi_polymarket"


class VenueAvailability(StrEnum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    RECOGNIZER_MISSING = "recognizer_missing"


class SettlementProofStatus(StrEnum):
    PROVEN_WHEN_INSTANCE_COMPLETE = "proven_when_instance_complete"
    UNPROVEN = "unproven"
    NOT_APPLICABLE = "not_applicable"


class SolverSupportStatus(StrEnum):
    SIMPLE_COMPLETE_SET = "simple_complete_set"
    GENERALIZED_PAYOFF = "generalized_payoff"
    NONE = "none"


class CatalogueCoverageState(StrEnum):
    """Fixture/UNIVERSE diagnostic state. Live execution stays Tenet 20."""

    APPROVED_EQUIVALENT = "approved_equivalent"
    PAPER_ASSUMED_EQUIVALENT = "paper_assumed_equivalent"
    APPROVED_PARAMETER_MISMATCH = "approved_parameter_mismatch"
    KNOWN_CONTRADICTION = "known_contradiction"
    REVIEW_REQUIRED = "review_required"
    UNSUPPORTED = "unsupported"
    VENUE_UNAVAILABLE = "venue_unavailable"
    NOT_LISTED = "not_listed"


class VenueArchetypeStatus(BaseModel):
    venue: str
    availability: VenueAvailability
    settlement_proof: SettlementProofStatus
    solver: SolverSupportStatus
    recognizer: str
    kalshi_series_suffix: str | None = None
    kalshi_series_missing_competitions: tuple[str, ...] = ()
    reason: str
    evidence: tuple[str, ...] = ()


class PairwiseRegistryCell(BaseModel):
    archetype: CatalogueArchetype
    venue_pair: str
    target_archetype: bool = True
    phase1_four_family: bool = False
    venue_available: bool
    settlement_proven: bool
    solver_supported: bool
    operational_approved: bool
    paper_assumed_operational: bool = False
    diagnostic_state: CatalogueCoverageState
    operational_state: CatalogueApprovalState
    reason: str
    sibling_states: tuple[str, ...] = ()


class CatalogueArchetypeSpec(BaseModel):
    archetype: CatalogueArchetype
    display_label: str
    canonical_family: MarketFamily | None
    required_parameters: tuple[str, ...]
    required_outcomes: tuple[str, ...]
    target_archetype: bool = True
    matchbook: VenueArchetypeStatus
    kalshi: VenueArchetypeStatus
    polymarket: VenueArchetypeStatus
    pairwise: tuple[PairwiseRegistryCell, ...]


def _venue(
    venue: str,
    *,
    availability: VenueAvailability,
    settlement: SettlementProofStatus,
    solver: SolverSupportStatus,
    recognizer: str,
    reason: str,
    evidence: tuple[str, ...] = (),
    kalshi_series_suffix: str | None = None,
    kalshi_series_missing_competitions: tuple[str, ...] = (),
) -> VenueArchetypeStatus:
    return VenueArchetypeStatus(
        venue=venue,
        availability=availability,
        settlement_proof=settlement,
        solver=solver,
        recognizer=recognizer,
        kalshi_series_suffix=kalshi_series_suffix,
        kalshi_series_missing_competitions=kalshi_series_missing_competitions,
        reason=reason,
        evidence=evidence,
    )


def _pair(
    archetype: CatalogueArchetype,
    venue_pair: str,
    *,
    venue_available: bool,
    settlement_proven: bool,
    solver_supported: bool,
    operational_approved: bool,
    diagnostic_state: CatalogueCoverageState,
    operational_state: CatalogueApprovalState,
    reason: str,
    sibling_states: tuple[str, ...] = (),
    phase1_four_family: bool = False,
    paper_assumed_operational: bool = False,
) -> PairwiseRegistryCell:
    return PairwiseRegistryCell(
        archetype=archetype,
        venue_pair=venue_pair,
        phase1_four_family=phase1_four_family,
        venue_available=venue_available,
        settlement_proven=settlement_proven,
        solver_supported=solver_supported,
        operational_approved=operational_approved,
        paper_assumed_operational=paper_assumed_operational,
        diagnostic_state=diagnostic_state,
        operational_state=operational_state,
        reason=reason,
        sibling_states=sibling_states,
    )


def _unavailable(venue: str, reason: str, *, evidence: tuple[str, ...] = ()) -> VenueArchetypeStatus:
    return _venue(
        venue,
        availability=VenueAvailability.UNAVAILABLE,
        settlement=SettlementProofStatus.NOT_APPLICABLE,
        solver=SolverSupportStatus.NONE,
        recognizer="none",
        reason=reason,
        evidence=evidence,
    )


def _recognizer_missing(venue: str, reason: str, *, evidence: tuple[str, ...] = ()) -> VenueArchetypeStatus:
    return _venue(
        venue,
        availability=VenueAvailability.RECOGNIZER_MISSING,
        settlement=SettlementProofStatus.NOT_APPLICABLE,
        solver=SolverSupportStatus.NONE,
        recognizer="none",
        reason=reason,
        evidence=evidence,
    )


_MB_FT_CONV = (
    "Matchbook documented full-time football convention "
    "(_standard_football_settlement). Payloads carry no resolution-rule text."
)
_K_SERIES = "Settings.kalshi_series_tickers / public GET /series retrieved 2026-09-16."
_K_NO_DNB = (
    "No Kalshi DNB series ticker is configured or captured. KalshiNormalizer "
    "raises on 'draw no bet' until draw-refund rules are proven."
)
_K_NO_HANDICAP = "KalshiNormalizer does not infer Asian Handicap from titles. No series ticker."
_K_NO_DC = "No Kalshi Double Chance series or recogniser. Do not invent one."
_K_NO_TTS = "No distinct Team To Score recogniser or series (not First Team To Score)."
_K_NO_CS = "No Team Clean Sheet recogniser or series on captured/live read-only metadata."
_K_NO_TT = "Kalshi named-team totals are not inferred as match or team totals. No TEAMTOTAL series."
_K_NO_INT_TOTAL = (
    "Kalshi integer/quarter totals remain deferred; line_push_possible is not False "
    "raises and no CanonicalMarket is assembled."
)


ARCHETYPE_SPECS: tuple[CatalogueArchetypeSpec, ...] = (
    CatalogueArchetypeSpec(
        archetype=CatalogueArchetype.MATCH_RESULT_1X2,
        display_label="1X2",
        canonical_family=MarketFamily.MATCH_RESULT,
        required_parameters=("period=full_time", "regulation", "no_extra_time", "no_penalties"),
        required_outcomes=("home", "draw", "away"),
        matchbook=_venue(
            VENUE_MATCHBOOK,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.SIMPLE_COMPLETE_SET,
            recognizer="exact name in {match odds, match result, moneyline, full time result}",
            reason=_MB_FT_CONV,
            evidence=("normalization/venues.py MatchbookNormalizer",),
        ),
        kalshi=_venue(
            VENUE_KALSHI,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.SIMPLE_COMPLETE_SET,
            recognizer="YES subtitles HOME/DRAW/AWAY; Get Market enrichment for Match Result only",
            kalshi_series_suffix="GAME",
            reason=(
                "Kalshi GAME series exists. Independent settlement is proven only from "
                "market-specific 90-minute wording (nested list or Get Market). GAMEWIN "
                "template / soccergamewin placeholder is PAPER_ASSUMED_EQUIVALENT in "
                "paper mode when HOME/DRAW/AWAY is complete. Cancel/reschedule-to-"
                "fair-price does not block PAPER admission (Issue #326). "
                "Series ticker never approves. Paper-assumed is never live-execution eligible."
            ),
            evidence=(
                _K_SERIES,
                "docs/APPROVED_MARKET_CATALOGUE_CENSUS_V1.md §3.1/§9",
                "backend/tests/fixtures/kalshi_trade_api_bundesliga_bayern_union_2026-09-18.json",
                "test_issue307_kalshi_metadata_pressure.py enrichment → APPROVED_EQUIVALENT",
            ),
        ),
        polymarket=_venue(
            VENUE_POLYMARKET,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.SIMPLE_COMPLETE_SET,
            recognizer="sportsMarketType moneyline or complementary HOME+DRAW+AWAY Yes contracts",
            reason="Parsed 90-minute wording required. Lone binary moneyline is not 3-way 1X2.",
            evidence=("docs/APPROVED_MARKET_CATALOGUE_CENSUS_V1.md §3.1",),
        ),
        pairwise=(
            _pair(
                CatalogueArchetype.MATCH_RESULT_1X2,
                VENUE_PAIR_MATCHBOOK_KALSHI,
                venue_available=True,
                settlement_proven=False,
                solver_supported=True,
                operational_approved=True,
                paper_assumed_operational=True,
                phase1_four_family=True,
                diagnostic_state=CatalogueCoverageState.PAPER_ASSUMED_EQUIVALENT,
                operational_state=CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT,
                reason=(
                    "Phase-1 Matchbook↔Kalshi 1X2 is PAPER_ASSUMED_EQUIVALENT when "
                    "Kalshi GAME contracts assemble HOME/DRAW/AWAY, fixture identity "
                    "is exact, and period/line are structurally consistent. "
                    "settlement_assumption=regulation_time. This is paper-mode only "
                    "and is never live-execution eligible. Independently proven "
                    "90-minute wording remains APPROVED_EQUIVALENT. Extra time, "
                    "penalties, and to-qualify fail closed. Cancel/reschedule-to-"
                    "fair-price does not block PAPER admission. Series ticker never approves."
                ),
                sibling_states=(
                    "APPROVED_EQUIVALENT: nested/Get Market 90-minute wording independently proves regulation",
                    "UNSUPPORTED/not_registered: extra-time/penalties/to-qualify is a different native archetype",
                    "PAPER_ASSUMED_EQUIVALENT: cancel/reschedule-to-fair-price; owner-approved paper assumption",
                    "REVIEW_REQUIRED: incomplete HOME/DRAW/AWAY outcome set",
                ),
            ),
            _pair(
                CatalogueArchetype.MATCH_RESULT_1X2,
                VENUE_PAIR_MATCHBOOK_POLYMARKET,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason="Matchbook full-time convention plus parsed Polymarket regulation 1X2.",
                sibling_states=(
                    "REVIEW_REQUIRED: Polymarket missing/unparsed settlement wording",
                    "REVIEW_REQUIRED: lone Polymarket moneyline binary is not 3-way 1X2",
                ),
            ),
            _pair(
                CatalogueArchetype.MATCH_RESULT_1X2,
                VENUE_PAIR_KALSHI_POLYMARKET,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason="Approved only when both fingerprints are complete regulation 1X2.",
            ),
        ),
    ),
    CatalogueArchetypeSpec(
        archetype=CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
        display_label="BTTS",
        canonical_family=MarketFamily.BOTH_TEAMS_TO_SCORE,
        required_parameters=("period=full_time", "regulation"),
        required_outcomes=("yes", "no"),
        matchbook=_venue(
            VENUE_MATCHBOOK,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.SIMPLE_COMPLETE_SET,
            recognizer="name contains both teams to score or equals btts",
            reason=_MB_FT_CONV,
        ),
        kalshi=_venue(
            VENUE_KALSHI,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.SIMPLE_COMPLETE_SET,
            recognizer="title/rules contain both teams to score or btts",
            kalshi_series_suffix="BTTS",
            reason=(
                "Kalshi BTTS series exists. Approved when market rules prove regulation. "
                "Issue #326 admits PAPER_ASSUMED_EQUIVALENT for structurally matched "
                "YES/NO without a fresh settlement-proof gate. Get Market is Match Result "
                "only; socceranygoal extra-time default is not applied."
            ),
            evidence=(_K_SERIES, "docs/APPROVED_MARKET_CATALOGUE_CENSUS_V1.md §3.2"),
        ),
        polymarket=_venue(
            VENUE_POLYMARKET,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.SIMPLE_COMPLETE_SET,
            recognizer="sportsMarketType / question contain BTTS",
            reason="Description must parse to regulation.",
        ),
        pairwise=(
            _pair(
                CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
                VENUE_PAIR_MATCHBOOK_KALSHI,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                paper_assumed_operational=True,
                phase1_four_family=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason=(
                    "Approved when Kalshi BTTS rules prove regulation YES/NO. "
                    "Issue #326 also admits PAPER_ASSUMED_EQUIVALENT when YES/NO "
                    "full-time identity matches without a fresh settlement-proof gate."
                ),
                sibling_states=(
                    "PAPER_ASSUMED_EQUIVALENT: structurally matched YES/NO without independent settlement proof",
                    "REVIEW_REQUIRED: incomplete YES/NO outcome set",
                ),
            ),
            _pair(
                CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
                VENUE_PAIR_MATCHBOOK_POLYMARKET,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason="Complete YES/NO regulation BTTS; simple complete-set.",
            ),
            _pair(
                CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
                VENUE_PAIR_KALSHI_POLYMARKET,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason="Approved when both YES/NO fingerprints are complete regulation BTTS.",
            ),
        ),
    ),
    CatalogueArchetypeSpec(
        archetype=CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
        display_label="TOTAL",
        canonical_family=MarketFamily.TOTAL_GOALS,
        required_parameters=("period=full_time", "exact_line", "half_line_no_push"),
        required_outcomes=("over", "under"),
        matchbook=_venue(
            VENUE_MATCHBOOK,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.SIMPLE_COMPLETE_SET,
            recognizer="total goal or (over under and goal); team total tokens fail closed",
            reason="Exact half-line required. 2.5 vs 3.5 is APPROVED_PARAMETER_MISMATCH.",
        ),
        kalshi=_venue(
            VENUE_KALSHI,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.SIMPLE_COMPLETE_SET,
            recognizer="match totals half-line Over/Under; named-team totals not inferred",
            kalshi_series_suffix="TOTAL",
            reason="Kalshi TOTAL series exists. Half-line only. Integer/quarter deferred.",
            evidence=(_K_SERIES, "docs/APPROVED_MARKET_CATALOGUE_CENSUS_V1.md §3.3"),
        ),
        polymarket=_venue(
            VENUE_POLYMARKET,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.SIMPLE_COMPLETE_SET,
            recognizer="match totals; team-scoped titles are TEAM_TOTAL",
            reason="Exact line must match.",
        ),
        pairwise=(
            _pair(
                CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
                VENUE_PAIR_MATCHBOOK_KALSHI,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                paper_assumed_operational=True,
                phase1_four_family=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason=(
                    "Approved when Kalshi half-line Over/Under rules prove regulation. "
                    "Issue #326 also admits PAPER_ASSUMED_EQUIVALENT when the exact "
                    "safe line matches without a fresh settlement-proof gate. "
                    "2.5↔2.5 yes; 2.5↔3.5 is APPROVED_PARAMETER_MISMATCH."
                ),
                sibling_states=(
                    "APPROVED_PARAMETER_MISMATCH: exact line mismatch e.g. 2.5 vs 3.5",
                    "PAPER_ASSUMED_EQUIVALENT: exact half-line identity without independent settlement proof",
                ),
            ),
            _pair(
                CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
                VENUE_PAIR_MATCHBOOK_POLYMARKET,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason="Exact half-line Over/Under; no push; simple complete-set.",
            ),
            _pair(
                CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
                VENUE_PAIR_KALSHI_POLYMARKET,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason="Approved when both half-line fingerprints are complete.",
            ),
        ),
    ),
    CatalogueArchetypeSpec(
        archetype=CatalogueArchetype.TOTAL_GOALS_INTEGER,
        display_label="TOTAL integer",
        canonical_family=MarketFamily.TOTAL_GOALS,
        required_parameters=("period=full_time", "exact_line", "integer_push"),
        required_outcomes=("over", "under"),
        matchbook=_venue(
            VENUE_MATCHBOOK,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.GENERALIZED_PAYOFF,
            recognizer="integer line Over/Under with modelled push",
            reason="Integer totals use generalized payoff when push is modelled.",
        ),
        kalshi=_unavailable(VENUE_KALSHI, _K_NO_INT_TOTAL, evidence=(_K_SERIES,)),
        polymarket=_venue(
            VENUE_POLYMARKET,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.GENERALIZED_PAYOFF,
            recognizer="integer line Over/Under when wording is complete",
            reason="Integer 2.0 is generalized-eligible when wording is complete.",
        ),
        pairwise=(
            _pair(
                CatalogueArchetype.TOTAL_GOALS_INTEGER,
                VENUE_PAIR_MATCHBOOK_KALSHI,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason=_K_NO_INT_TOTAL,
            ),
            _pair(
                CatalogueArchetype.TOTAL_GOALS_INTEGER,
                VENUE_PAIR_MATCHBOOK_POLYMARKET,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason="Integer line Over/Under with modelled push/void; generalized payoff.",
            ),
            _pair(
                CatalogueArchetype.TOTAL_GOALS_INTEGER,
                VENUE_PAIR_KALSHI_POLYMARKET,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason="Kalshi does not normalize integer totals.",
            ),
        ),
    ),
    CatalogueArchetypeSpec(
        archetype=CatalogueArchetype.FIRST_TEAM_TO_SCORE,
        display_label="FTTS",
        canonical_family=MarketFamily.FIRST_TEAM_TO_SCORE,
        required_parameters=("period=full_time", "regulation", "no_goal_contract"),
        required_outcomes=("home", "away", "no_goal"),
        matchbook=_venue(
            VENUE_MATCHBOOK,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.GENERALIZED_PAYOFF,
            recognizer="explicit FTTS phrases or first goal with team-level H/A/NO_GOAL",
            reason="Missing NO_GOAL is incomplete. Two-team books are not equivalent (Tenet 20 §9).",
        ),
        kalshi=_venue(
            VENUE_KALSHI,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.GENERALIZED_PAYOFF,
            recognizer="HOME+AWAY+NO_GOAL and proven REGULATION_TIME",
            kalshi_series_suffix="FTTS",
            kalshi_series_missing_competitions=(
                "championship",
                "international_friendlies",
            ),
            reason=(
                "Kalshi FTTS series exists for EPL/La Liga/cups/Bundesliga/Serie A. "
                "Championship and international friendlies have no FTTS ticker in Settings. "
                "Missing NO_GOAL never becomes a CanonicalMarket. Incomplete settlement "
                "still lists structurally complete HOME/AWAY/NO_GOAL for PAPER admission. "
                "Get Market is not fetched."
            ),
            evidence=(
                _K_SERIES,
                "test_target_competition_discovery.py KXINTLFRIENDLYFTTS absent",
            ),
        ),
        polymarket=_venue(
            VENUE_POLYMARKET,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.GENERALIZED_PAYOFF,
            recognizer="explicit FTTS or 3 team-level outcomes",
            reason="Player first-goalscorer is PLAYER_PROPS, not FTTS.",
        ),
        pairwise=(
            _pair(
                CatalogueArchetype.FIRST_TEAM_TO_SCORE,
                VENUE_PAIR_MATCHBOOK_KALSHI,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                paper_assumed_operational=True,
                phase1_four_family=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason=(
                    "Kalshi assembles FTTS with HOME/AWAY/NO_GOAL. Independently "
                    "proven regulation-time rules remain APPROVED_EQUIVALENT. "
                    "Issue #326 admits PAPER_ASSUMED_EQUIVALENT when the complete "
                    "three-state identity is listed without a fresh settlement-proof gate."
                ),
                sibling_states=(
                    "PAPER_ASSUMED_EQUIVALENT: HOME/AWAY/NO_GOAL listed without independent settlement proof",
                    "REVIEW_REQUIRED: Kalshi missing a NO_GOAL contract",
                    "VENUE_UNAVAILABLE: Championship / international friendlies have no FTTS series",
                ),
            ),
            _pair(
                CatalogueArchetype.FIRST_TEAM_TO_SCORE,
                VENUE_PAIR_MATCHBOOK_POLYMARKET,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason="HOME/AWAY/NO_GOAL regulation-time contract; generalized payoff.",
            ),
            _pair(
                CatalogueArchetype.FIRST_TEAM_TO_SCORE,
                VENUE_PAIR_KALSHI_POLYMARKET,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason="Approved when both sides are complete 3-state regulation FTTS.",
            ),
        ),
    ),
    CatalogueArchetypeSpec(
        archetype=CatalogueArchetype.TEAM_TOTAL_GOALS,
        display_label="TEAM TOTAL",
        canonical_family=MarketFamily.TEAM_TOTAL,
        required_parameters=("named_team", "exact_line", "over_under"),
        required_outcomes=("over", "under"),
        matchbook=_venue(
            VENUE_MATCHBOOK,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.UNPROVEN,
            solver=SolverSupportStatus.NONE,
            recognizer="team total / named-team totals wording",
            reason="Recognized, but CanonicalMarket has no team-scope field. Not operational.",
        ),
        kalshi=_unavailable(VENUE_KALSHI, _K_NO_TT, evidence=(_K_SERIES,)),
        polymarket=_venue(
            VENUE_POLYMARKET,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.UNPROVEN,
            solver=SolverSupportStatus.NONE,
            recognizer="groupItemTitle / named-team totals",
            reason="Recognized; team-scope not extracted on CanonicalMarket.",
        ),
        pairwise=(
            _pair(
                CatalogueArchetype.TEAM_TOTAL_GOALS,
                VENUE_PAIR_MATCHBOOK_KALSHI,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason=_K_NO_TT,
            ),
            _pair(
                CatalogueArchetype.TEAM_TOTAL_GOALS,
                VENUE_PAIR_MATCHBOOK_POLYMARKET,
                venue_available=True,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.REVIEW_REQUIRED,
                operational_state=CatalogueApprovalState.REVIEW_REQUIRED,
                reason="team_scope_not_extracted_on_canonical_market",
            ),
            _pair(
                CatalogueArchetype.TEAM_TOTAL_GOALS,
                VENUE_PAIR_KALSHI_POLYMARKET,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason="Kalshi does not normalize team totals.",
            ),
        ),
    ),
    CatalogueArchetypeSpec(
        archetype=CatalogueArchetype.HANDICAP,
        display_label="HANDICAP",
        canonical_family=MarketFamily.ASIAN_HANDICAP,
        required_parameters=("named_team", "handicap_type", "exact_line"),
        required_outcomes=("home", "away"),
        matchbook=_venue(
            VENUE_MATCHBOOK,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.UNPROVEN,
            solver=SolverSupportStatus.NONE,
            recognizer="Asian Handicap / handicap",
            reason="Recognized but unproven handicap semantics; solver-ineligible.",
        ),
        kalshi=_unavailable(VENUE_KALSHI, _K_NO_HANDICAP, evidence=(_K_SERIES,)),
        polymarket=_venue(
            VENUE_POLYMARKET,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.UNPROVEN,
            solver=SolverSupportStatus.NONE,
            recognizer="handicap / spread",
            reason="Recognized but unproven; no solver.",
        ),
        pairwise=(
            _pair(
                CatalogueArchetype.HANDICAP,
                VENUE_PAIR_MATCHBOOK_KALSHI,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason=_K_NO_HANDICAP,
            ),
            _pair(
                CatalogueArchetype.HANDICAP,
                VENUE_PAIR_MATCHBOOK_POLYMARKET,
                venue_available=True,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.UNSUPPORTED,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason="unproven_handicap_semantics",
            ),
            _pair(
                CatalogueArchetype.HANDICAP,
                VENUE_PAIR_KALSHI_POLYMARKET,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason="Kalshi does not normalize handicap contracts.",
            ),
        ),
    ),
    CatalogueArchetypeSpec(
        archetype=CatalogueArchetype.DRAW_NO_BET,
        display_label="DNB",
        canonical_family=MarketFamily.DRAW_NO_BET,
        required_parameters=("period=full_time", "draw_void"),
        required_outcomes=("home", "away"),
        matchbook=_venue(
            VENUE_MATCHBOOK,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.GENERALIZED_PAYOFF,
            recognizer="Draw No Bet",
            reason="HOME/AWAY with family push convention draw-void.",
        ),
        kalshi=_unavailable(VENUE_KALSHI, _K_NO_DNB, evidence=(_K_SERIES,)),
        polymarket=_venue(
            VENUE_POLYMARKET,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.PROVEN_WHEN_INSTANCE_COMPLETE,
            solver=SolverSupportStatus.GENERALIZED_PAYOFF,
            recognizer="Draw No Bet",
            reason="Complete regulation wording plus draw-void.",
        ),
        pairwise=(
            _pair(
                CatalogueArchetype.DRAW_NO_BET,
                VENUE_PAIR_MATCHBOOK_KALSHI,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason=_K_NO_DNB,
            ),
            _pair(
                CatalogueArchetype.DRAW_NO_BET,
                VENUE_PAIR_MATCHBOOK_POLYMARKET,
                venue_available=True,
                settlement_proven=True,
                solver_supported=True,
                operational_approved=True,
                diagnostic_state=CatalogueCoverageState.APPROVED_EQUIVALENT,
                operational_state=CatalogueApprovalState.APPROVED_EQUIVALENT,
                reason="HOME/AWAY with proven draw-void; generalized payoff.",
            ),
            _pair(
                CatalogueArchetype.DRAW_NO_BET,
                VENUE_PAIR_KALSHI_POLYMARKET,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason="Kalshi Draw No Bet is not assembled.",
            ),
        ),
    ),
    CatalogueArchetypeSpec(
        archetype=CatalogueArchetype.DOUBLE_CHANCE,
        display_label="DOUBLE CHANCE",
        canonical_family=MarketFamily.DOUBLE_CHANCE,
        required_parameters=("period=full_time", "combination_1x_12_x2"),
        required_outcomes=("home_or_draw", "home_or_away", "draw_or_away"),
        matchbook=_venue(
            VENUE_MATCHBOOK,
            availability=VenueAvailability.AVAILABLE,
            settlement=SettlementProofStatus.UNPROVEN,
            solver=SolverSupportStatus.NONE,
            recognizer="name Double Chance",
            reason="Recognized. No solver model.",
        ),
        kalshi=_unavailable(VENUE_KALSHI, _K_NO_DC, evidence=(_K_SERIES,)),
        polymarket=_recognizer_missing(
            VENUE_POLYMARKET,
            "Polymarket has no Double Chance recogniser. Do not invent one.",
        ),
        pairwise=(
            _pair(
                CatalogueArchetype.DOUBLE_CHANCE,
                VENUE_PAIR_MATCHBOOK_KALSHI,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason=_K_NO_DC,
            ),
            _pair(
                CatalogueArchetype.DOUBLE_CHANCE,
                VENUE_PAIR_MATCHBOOK_POLYMARKET,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.UNSUPPORTED,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason="Matchbook recognizes Double Chance. Polymarket has no recogniser. No solver.",
            ),
            _pair(
                CatalogueArchetype.DOUBLE_CHANCE,
                VENUE_PAIR_KALSHI_POLYMARKET,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason=_K_NO_DC,
            ),
        ),
    ),
    CatalogueArchetypeSpec(
        archetype=CatalogueArchetype.TEAM_TO_SCORE,
        display_label="TEAM TO SCORE",
        canonical_family=None,
        required_parameters=("named_team", "yes_no"),
        required_outcomes=("yes", "no"),
        matchbook=_recognizer_missing(
            VENUE_MATCHBOOK,
            "No distinct Team To Score recogniser (not First Team To Score).",
        ),
        kalshi=_unavailable(VENUE_KALSHI, _K_NO_TTS, evidence=(_K_SERIES,)),
        polymarket=_recognizer_missing(
            VENUE_POLYMARKET,
            "No distinct Team To Score recogniser or solver model.",
        ),
        pairwise=(
            _pair(
                CatalogueArchetype.TEAM_TO_SCORE,
                VENUE_PAIR_MATCHBOOK_KALSHI,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason=_K_NO_TTS,
            ),
            _pair(
                CatalogueArchetype.TEAM_TO_SCORE,
                VENUE_PAIR_MATCHBOOK_POLYMARKET,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.UNSUPPORTED,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason="No distinct Team To Score recogniser or solver model.",
            ),
            _pair(
                CatalogueArchetype.TEAM_TO_SCORE,
                VENUE_PAIR_KALSHI_POLYMARKET,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason=_K_NO_TTS,
            ),
        ),
    ),
    CatalogueArchetypeSpec(
        archetype=CatalogueArchetype.TEAM_CLEAN_SHEET,
        display_label="CLEAN SHEET",
        canonical_family=None,
        required_parameters=("named_team", "yes_no"),
        required_outcomes=("yes", "no"),
        matchbook=_recognizer_missing(VENUE_MATCHBOOK, "No Team Clean Sheet recogniser."),
        kalshi=_unavailable(VENUE_KALSHI, _K_NO_CS, evidence=(_K_SERIES,)),
        polymarket=_recognizer_missing(VENUE_POLYMARKET, "No Team Clean Sheet recogniser."),
        pairwise=(
            _pair(
                CatalogueArchetype.TEAM_CLEAN_SHEET,
                VENUE_PAIR_MATCHBOOK_KALSHI,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason=_K_NO_CS,
            ),
            _pair(
                CatalogueArchetype.TEAM_CLEAN_SHEET,
                VENUE_PAIR_MATCHBOOK_POLYMARKET,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.UNSUPPORTED,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason="No Team Clean Sheet recogniser.",
            ),
            _pair(
                CatalogueArchetype.TEAM_CLEAN_SHEET,
                VENUE_PAIR_KALSHI_POLYMARKET,
                venue_available=False,
                settlement_proven=False,
                solver_supported=False,
                operational_approved=False,
                diagnostic_state=CatalogueCoverageState.VENUE_UNAVAILABLE,
                operational_state=CatalogueApprovalState.UNSUPPORTED,
                reason=_K_NO_CS,
            ),
        ),
    ),
)


_SPEC_BY_ARCHETYPE: dict[CatalogueArchetype, CatalogueArchetypeSpec] = {
    spec.archetype: spec for spec in ARCHETYPE_SPECS
}
_CELL_BY_KEY: dict[tuple[CatalogueArchetype, str], PairwiseRegistryCell] = {
    (cell.archetype, cell.venue_pair): cell
    for spec in ARCHETYPE_SPECS
    for cell in spec.pairwise
}


def target_archetypes() -> tuple[CatalogueArchetype, ...]:
    return tuple(spec.archetype for spec in ARCHETYPE_SPECS if spec.target_archetype)


def target_market_families() -> frozenset[MarketFamily]:
    return frozenset(
        spec.canonical_family for spec in ARCHETYPE_SPECS if spec.canonical_family is not None
    )


def archetype_spec(archetype: CatalogueArchetype) -> CatalogueArchetypeSpec:
    return _SPEC_BY_ARCHETYPE[archetype]


def registry_cell(archetype: CatalogueArchetype, venue_pair: str) -> PairwiseRegistryCell:
    try:
        return _CELL_BY_KEY[(archetype, venue_pair)]
    except KeyError as exc:
        raise KeyError(f"No registry cell for {archetype} {venue_pair}") from exc


def family_to_registry_archetype(
    family: MarketFamily,
    *,
    integer_line: bool | None = None,
) -> CatalogueArchetype | None:
    if family is MarketFamily.MATCH_RESULT:
        return CatalogueArchetype.MATCH_RESULT_1X2
    if family is MarketFamily.BOTH_TEAMS_TO_SCORE:
        return CatalogueArchetype.BOTH_TEAMS_TO_SCORE
    if family is MarketFamily.FIRST_TEAM_TO_SCORE:
        return CatalogueArchetype.FIRST_TEAM_TO_SCORE
    if family is MarketFamily.TOTAL_GOALS:
        if integer_line is True:
            return CatalogueArchetype.TOTAL_GOALS_INTEGER
        return CatalogueArchetype.TOTAL_GOALS_HALF_LINE
    if family is MarketFamily.TEAM_TOTAL:
        return CatalogueArchetype.TEAM_TOTAL_GOALS
    if family is MarketFamily.ASIAN_HANDICAP:
        return CatalogueArchetype.HANDICAP
    if family is MarketFamily.DRAW_NO_BET:
        return CatalogueArchetype.DRAW_NO_BET
    if family is MarketFamily.DOUBLE_CHANCE:
        return CatalogueArchetype.DOUBLE_CHANCE
    return None


def phase1_four_family_archetypes() -> tuple[CatalogueArchetype, ...]:
    return (
        CatalogueArchetype.MATCH_RESULT_1X2,
        CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
        CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
        CatalogueArchetype.FIRST_TEAM_TO_SCORE,
    )


def phase1_expensive_work_families() -> frozenset[MarketFamily]:
    """Families that may trigger settlement/depth/solver work in Phase 1."""

    return frozenset(
        {
            MarketFamily.MATCH_RESULT,
            MarketFamily.BOTH_TEAMS_TO_SCORE,
            MarketFamily.TOTAL_GOALS,
            MarketFamily.FIRST_TEAM_TO_SCORE,
        }
    )


def family_is_phase1_expensive_work(family: MarketFamily | None) -> bool:
    return family in phase1_expensive_work_families()


def operational_kalshi_families() -> frozenset[MarketFamily]:
    """Families Kalshi actually offers that Phase-1 may operationalise.

    MATCH_RESULT is included for the paper-assumed 1X2 path even though
    independent settlement proof is not required.
    """

    families: set[MarketFamily] = set()
    for spec in ARCHETYPE_SPECS:
        if spec.canonical_family is None:
            continue
        cell = registry_cell(spec.archetype, VENUE_PAIR_MATCHBOOK_KALSHI)
        if spec.kalshi.availability is not VenueAvailability.AVAILABLE:
            continue
        if cell.operational_approved or cell.paper_assumed_operational or cell.phase1_four_family:
            families.add(spec.canonical_family)
    return frozenset(families)


def kalshi_series_suffixes() -> tuple[str, ...]:
    suffixes = []
    seen: set[str] = set()
    for spec in ARCHETYPE_SPECS:
        suffix = spec.kalshi.kalshi_series_suffix
        if suffix and suffix not in seen:
            seen.add(suffix)
            suffixes.append(suffix)
    return tuple(suffixes)


def display_label(archetype: CatalogueArchetype, *, line: object | None = None) -> str:
    spec = archetype_spec(archetype)
    if line is not None and spec.canonical_family is MarketFamily.TOTAL_GOALS:
        return f"{spec.display_label} {line}"
    return spec.display_label


def coverage_state_from_approval(state: CatalogueApprovalState) -> CatalogueCoverageState:
    return CatalogueCoverageState(state.value)


def venue_status(archetype: CatalogueArchetype, venue: str) -> VenueArchetypeStatus:
    spec = archetype_spec(archetype)
    if venue == VENUE_MATCHBOOK:
        return spec.matchbook
    if venue == VENUE_KALSHI:
        return spec.kalshi
    if venue == VENUE_POLYMARKET:
        return spec.polymarket
    raise KeyError(f"Unknown venue {venue}")


def render_registry_markdown() -> str:
    lines = [
        f"Registry version: {REGISTRY_VERSION}",
        f"Parent: `{REGISTRY_PARENT_COMMIT}`",
        f"Shared by: {', '.join(REGISTRY_SHARED_BY)}",
        f"Data class: {DATA_CLASS}",
        "",
        "| Archetype | TARGET | MB | Kalshi | Settlement (MB↔K) | Solver | Operational MB↔K | Why if not |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for spec in ARCHETYPE_SPECS:
        cell = registry_cell(spec.archetype, VENUE_PAIR_MATCHBOOK_KALSHI)
        lines.append(
            "| "
            + " | ".join(
                [
                    spec.display_label,
                    "yes" if spec.target_archetype else "no",
                    spec.matchbook.availability.value,
                    spec.kalshi.availability.value,
                    "yes" if cell.settlement_proven else "no",
                    "yes" if cell.solver_supported else "no",
                    cell.diagnostic_state.value.upper(),
                    cell.reason.replace("\n", " ")[:140],
                ]
            )
            + " |"
        )
    return "\n".join(lines)

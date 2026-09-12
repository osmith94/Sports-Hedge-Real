from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from pydantic import BaseModel, Field

from sports_hedge.domain.football import MarketFamily
from sports_hedge.facts.catalog import list_competitions
from sports_hedge.odds.models import (
    CanonicalMatchFact,
    MappingException,
    OddsObservation,
    QualityTier,
    QuoteType,
)

COVERAGE_FAMILIES = (
    MarketFamily.MATCH_RESULT,
    MarketFamily.TOTAL_GOALS,
    MarketFamily.BOTH_TEAMS_TO_SCORE,
    MarketFamily.CORNERS,
    MarketFamily.CARDS,
)


class MarketCoverageRow(BaseModel):
    competition_code: str
    season: str
    market_family: str
    matches_in_repository: int
    expected_league_matches: int | None
    matches_with_opening_1x2: int = 0
    matches_with_closing_1x2: int = 0
    matches_with_market: int = 0
    matches_with_timestamped: int = 0
    matches_with_liquidity: int = 0
    coverage_ratio: float = 0.0
    missing_match_count: int = 0
    calendar_gap: int | None = None


class SourceCoverageRow(BaseModel):
    source: str
    required: bool
    observations: int
    matches: int
    quality_tiers: dict[str, int] = Field(default_factory=dict)
    timestamped_observations: int = 0
    liquidity_observations: int = 0
    available: bool = True
    notes: str = ""


class QualitySummaryRow(BaseModel):
    quality_tier: str
    observations: int
    matches: int
    meaning: str


class CoverageReport(BaseModel):
    universe: list[str]
    matches_in_repository: int
    observations: int
    unresolved_mapping_count: int
    market_coverage: list[MarketCoverageRow]
    source_coverage: list[SourceCoverageRow]
    quality_summary: list[QualitySummaryRow]
    mapping_exceptions: list[MappingException]
    smarkets_limitations: dict[str, object] = Field(default_factory=dict)


_TIER_MEANING = {
    QualityTier.A: "timestamped exchange odds + liquidity",
    QualityTier.B: "timestamped bookmaker odds",
    QualityTier.C: "opening/closing odds only",
    QualityTier.D: "non-odds match facts only / unavailable odds",
}


def build_coverage_report(
    *,
    matches: Iterable[CanonicalMatchFact],
    observations: Iterable[OddsObservation],
    exceptions: Iterable[MappingException],
    smarkets_limitations: dict[str, object] | None = None,
) -> CoverageReport:
    match_list = list(matches)
    observation_list = list(observations)
    exception_list = list(exceptions)
    match_ids_by_comp: dict[tuple[str, str], set[str]] = defaultdict(set)
    for match in match_list:
        match_ids_by_comp[(match.competition_id, match.season)].add(match.canonical_match_id)

    obs_by_match: dict[str, list[OddsObservation]] = defaultdict(list)
    for observation in observation_list:
        obs_by_match[observation.canonical_match_id].append(observation)

    market_coverage: list[MarketCoverageRow] = []
    for spec in list_competitions():
        keys = (spec.competition_id, spec.season)
        repo_matches = match_ids_by_comp.get(keys, set())
        for family in COVERAGE_FAMILIES:
            with_market: set[str] = set()
            opening_1x2: set[str] = set()
            closing_1x2: set[str] = set()
            timestamped: set[str] = set()
            liquidity: set[str] = set()
            for match_id in repo_matches:
                for observation in obs_by_match.get(match_id, []):
                    if observation.market_family != family:
                        continue
                    if observation.decimal_odds is None:
                        continue
                    with_market.add(match_id)
                    if observation.quote_type == QuoteType.TIMESTAMPED:
                        timestamped.add(match_id)
                    if observation.liquidity is not None:
                        liquidity.add(match_id)
                    if family == MarketFamily.MATCH_RESULT:
                        if observation.quote_type == QuoteType.OPENING:
                            opening_1x2.add(match_id)
                        if observation.quote_type == QuoteType.CLOSING:
                            closing_1x2.add(match_id)
            total = len(repo_matches)
            covered = len(with_market)
            calendar_gap = None
            if spec.expected_league_matches is not None:
                calendar_gap = max(spec.expected_league_matches - total, 0)
            market_coverage.append(
                MarketCoverageRow(
                    competition_code=spec.competition_id,
                    season=spec.season,
                    market_family=family.value,
                    matches_in_repository=total,
                    expected_league_matches=spec.expected_league_matches,
                    matches_with_opening_1x2=len(opening_1x2),
                    matches_with_closing_1x2=len(closing_1x2),
                    matches_with_market=covered,
                    matches_with_timestamped=len(timestamped),
                    matches_with_liquidity=len(liquidity),
                    coverage_ratio=(covered / total) if total else 0.0,
                    missing_match_count=max(total - covered, 0),
                    calendar_gap=calendar_gap,
                )
            )

    source_rows: dict[str, SourceCoverageRow] = {}
    for observation in observation_list:
        row = source_rows.setdefault(
            observation.source,
            SourceCoverageRow(source=observation.source, required=False, observations=0, matches=0),
        )
        row.observations += 1
        row.quality_tiers[observation.quality_tier.value] = (
            row.quality_tiers.get(observation.quality_tier.value, 0) + 1
        )
        if observation.quote_type == QuoteType.TIMESTAMPED:
            row.timestamped_observations += 1
        if observation.liquidity is not None:
            row.liquidity_observations += 1
    match_sources: dict[str, set[str]] = defaultdict(set)
    for observation in observation_list:
        match_sources[observation.source].add(observation.canonical_match_id)
    for source, ids in match_sources.items():
        source_rows[source].matches = len(ids)

    if smarkets_limitations is not None and "smarkets" not in source_rows:
        source_rows["smarkets"] = SourceCoverageRow(
            source="smarkets",
            required=False,
            observations=0,
            matches=0,
            available=False,
            notes=str(smarkets_limitations.get("notes", "")),
        )

    quality_summary = []
    for tier in QualityTier:
        rows = [item for item in observation_list if item.quality_tier == tier]
        quality_summary.append(
            QualitySummaryRow(
                quality_tier=tier.value,
                observations=len(rows),
                matches=len({item.canonical_match_id for item in rows}),
                meaning=_TIER_MEANING[tier],
            )
        )

    return CoverageReport(
        universe=[
            f"{item.display_name} {item.season}"
            for item in list_competitions()
            if item.in_bounded_universe
        ]
        + ["UEFA Champions League 2025/26 (schema only)"],
        matches_in_repository=len(match_list),
        observations=len(observation_list),
        unresolved_mapping_count=len(exception_list),
        market_coverage=market_coverage,
        source_coverage=sorted(source_rows.values(), key=lambda row: row.source),
        quality_summary=quality_summary,
        mapping_exceptions=exception_list,
        smarkets_limitations=smarkets_limitations or {},
    )

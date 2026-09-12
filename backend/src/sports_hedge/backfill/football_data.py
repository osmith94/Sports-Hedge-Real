from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sports_hedge.historical.catalog import CHAMPIONSHIP, PREMIER_LEAGUE, HistoricalCatalog
from sports_hedge.historical.coverage import CoverageReporter
from sports_hedge.historical.excel import HistoricalExcelExporter
from sports_hedge.historical.football_data import FootballDataHistoricalAdapter
from sports_hedge.historical.ingestion import HistoricalIngestionService
from sports_hedge.historical.repository import SqliteHistoricalRepository
from sports_hedge.odds.adapters.football_data import FootballDataCsvAdapter
from sports_hedge.odds.coverage import build_coverage_report
from sports_hedge.odds.excel import export_odds_workbook
from sports_hedge.odds.ingestion import OddsIngestionService
from sports_hedge.odds.movement import classify_open_close
from sports_hedge.odds.repository import SqliteOddsRepository
from sports_hedge.sources.football_data import (
    BACKFILL_DIVISIONS,
    BACKFILL_SEASON_CODES,
    EXPECTED_FIVE_SEASON_MATCHES,
    EXPECTED_SEASON_MATCHES,
    MAX_MAPPING_EXCEPTIONS,
    FootballDataFileError,
    fetch_public_csv,
    load_csv_text,
    local_csv_path,
    public_csv_url,
    rows_from_csv,
    season_label_from_code,
    validate_season_rows,
)

COMPETITION_BY_DIV = {"E0": PREMIER_LEAGUE, "E1": CHAMPIONSHIP}


class BackfillError(RuntimeError):
    """Raised when the operator backfill cannot honestly complete."""


@dataclass(frozen=True)
class FileResult:
    season_code: str
    season_label: str
    div: str
    source_url: str
    csv_rows: int
    facts_matches: int
    odds_observations_created: int
    odds_observations_duplicate: int
    mapping_exceptions: int


@dataclass(frozen=True)
class BackfillResult:
    retrieved_at: datetime
    files: tuple[FileResult, ...]
    facts_db: Path
    odds_db: Path
    coverage_markdown: Path
    facts_xlsx: Path
    odds_xlsx: Path

    @property
    def divisions(self) -> tuple[FileResult, ...]:
        """Backward-compatible alias used by existing tests."""

        return self.files


def load_file_csv(
    div: str,
    season_code: str,
    *,
    local_dir: Path | None,
    fetch: bool,
) -> tuple[str, str]:
    if local_dir is not None:
        path = local_csv_path(local_dir, season_code, div)
        if not path.exists():
            raise BackfillError(f"Local Football-Data file missing: {path}")
        return public_csv_url(div, season_code), load_csv_text(path)
    if fetch:
        return fetch_public_csv(div, season_code)
    raise BackfillError("Pass --fetch or --local-dir so the backfill is operator-invoked")


def run_backfill(
    *,
    facts_db: Path,
    odds_db: Path,
    output_dir: Path,
    local_dir: Path | None,
    fetch: bool,
    retrieved_at: datetime | None = None,
    min_rows: int | None = None,
    max_exceptions: int = MAX_MAPPING_EXCEPTIONS,
    season_codes: tuple[str, ...] = BACKFILL_SEASON_CODES,
) -> BackfillResult:
    retrieved = retrieved_at or datetime.now(UTC)
    facts_db.parent.mkdir(parents=True, exist_ok=True)
    odds_db.parent.mkdir(parents=True, exist_ok=True)
    catalog = HistoricalCatalog()
    facts_repo = SqliteHistoricalRepository(facts_db)
    odds_repo = SqliteOddsRepository(odds_db)
    facts_service = HistoricalIngestionService(facts_repo, catalog)
    odds_service = OddsIngestionService(odds_repo)
    results: list[FileResult] = []

    for season_code in season_codes:
        season_label = season_label_from_code(season_code)
        for div in BACKFILL_DIVISIONS:
            url, csv_text = load_file_csv(
                div, season_code, local_dir=local_dir, fetch=fetch
            )
            rows = rows_from_csv(csv_text)
            validate_season_rows(rows, div=div, min_rows=min_rows)
            competition = COMPETITION_BY_DIV[div]
            facts_ids = facts_service.ingest_adapter(
                FootballDataHistoricalAdapter(
                    csv_text,
                    retrieved_at=retrieved,
                    source_url=url,
                    div=div,
                    season_label=season_label,
                ),
                competition_id=competition.competition_id,
                season_label=season_label,
            )
            odds_batch = odds_service.ingest_adapter(
                FootballDataCsvAdapter(
                    csv_text,
                    retrieved_at=retrieved,
                    season_label=season_label,
                    source_url=url,
                    div=div,
                )
            )
            if odds_batch.exceptions > max_exceptions:
                raise BackfillError(
                    f"{season_code}/{div} mapping exceptions {odds_batch.exceptions} "
                    f"exceed threshold {max_exceptions}"
                )
            results.append(
                FileResult(
                    season_code=season_code,
                    season_label=season_label,
                    div=div,
                    source_url=url,
                    csv_rows=len(rows),
                    facts_matches=len(set(facts_ids)),
                    odds_observations_created=odds_batch.observations_created,
                    odds_observations_duplicate=odds_batch.observations_duplicate,
                    mapping_exceptions=odds_batch.exceptions,
                )
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    observations = odds_repo.list_observations()
    odds_report = build_coverage_report(
        matches=odds_repo.list_matches(),
        observations=observations,
        exceptions=odds_repo.list_exceptions(),
        smarkets_limitations={"required": False, "notes": "Smarkets not part of this backfill"},
    )
    classified = classify_open_close(observations)
    moves = classified.price_moves
    line_shifts = classified.line_shifts
    facts_xlsx = HistoricalExcelExporter(facts_repo).export(
        output_dir / "football-data-england-facts.xlsx"
    )
    odds_xlsx = export_odds_workbook(
        output_dir / "football-data-england-odds.xlsx",
        observations=observations,
        report=odds_report,
        moves=moves,
        include_observations=False,
        include_moves=False,
    )
    markdown = render_coverage_markdown(
        results,
        facts_cells=CoverageReporter(facts_repo).report(),
        odds_report=odds_report,
        observations=observations,
        moves=moves,
        line_shifts=line_shifts,
        retrieved_at=retrieved,
        season_codes=season_codes,
    )
    coverage_path = output_dir / "football-data-england-coverage.md"
    coverage_path.write_text(markdown, encoding="utf-8")
    facts_repo.close()
    odds_repo.close()
    return BackfillResult(
        retrieved_at=retrieved,
        files=tuple(results),
        facts_db=facts_db,
        odds_db=odds_db,
        coverage_markdown=coverage_path,
        facts_xlsx=facts_xlsx,
        odds_xlsx=odds_xlsx,
    )


def render_coverage_markdown(
    results: list[FileResult],
    *,
    facts_cells,
    odds_report,
    observations,
    moves,
    line_shifts,
    retrieved_at: datetime,
    season_codes: tuple[str, ...],
) -> str:
    total_facts = sum(item.facts_matches for item in results)
    lines = [
        "# Football-Data.co.uk England backfill (five seasons)",
        "",
        "Repository-derived review of **real** public Football-Data files for",
        "Premier League (`E0`) and Championship (`E1`), seasons 2021/22–2025/26.",
        "The SQLite databases are the source of truth; Excel is export-only.",
        "",
        f"- Retrieved at (UTC): `{retrieved_at.isoformat()}`",
        "- Kickoff timezone assumption: Europe/London, converted to UTC in the adapter",
        "- Odds quality: opening/closing snapshots only (**tier C**, `observed_at` empty)",
        "- Max/Avg (and BFE where labelled research-only) are **aggregate/research-only**, not executable venue quotes",
        "- Unavailable from this source: goal minutes, lineups, substitutions, penalties, liquidity, commission, intraday quote timestamps",
        "- Smarkets: not used",
        f"- Sanity check: complete 5×PL + 5×Championship league calendars would be **{EXPECTED_FIVE_SEASON_MATCHES}** matches; imported facts matches: **{total_facts}**",
        "",
        "## Source files",
        "",
        "| Season | Code | Div | Competition | Public URL | CSV rows | Expected | Facts matches | Odds observations created | Mapping exceptions |",
        "|---|---|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for item in results:
        competition = COMPETITION_BY_DIV[item.div]
        expected = EXPECTED_SEASON_MATCHES[item.div]
        lines.append(
            f"| {item.season_label} | {item.season_code} | {item.div} | {competition.name} | "
            f"{item.source_url} | {item.csv_rows} | {expected} | {item.facts_matches} | "
            f"{item.odds_observations_created} | {item.mapping_exceptions} |"
        )
        if item.facts_matches < expected:
            lines.append("")
            lines.append(
                f"**Incomplete vs calendar:** {competition.name} {item.season_label} imported "
                f"{item.facts_matches} / {expected} expected matches. Do not claim complete coverage."
            )
    if total_facts != EXPECTED_FIVE_SEASON_MATCHES and season_codes == BACKFILL_SEASON_CODES:
        lines.extend(
            [
                "",
                f"**Deviation from {EXPECTED_FIVE_SEASON_MATCHES}:** imported {total_facts} facts matches. "
                "This is reported, not forced.",
            ]
        )
    lines.extend(
        [
            "",
            "## Facts field coverage",
            "",
            "| Competition | Season | Field | Matches | Present | Ratio |",
            "|---|---|---|---:|---:|---:|",
        ]
    )
    for cell in facts_cells:
        if cell.competition_id not in {PREMIER_LEAGUE.competition_id, CHAMPIONSHIP.competition_id}:
            continue
        lines.append(
            f"| {cell.competition_name} | {cell.season_label} | {cell.field_name} | "
            f"{cell.matches_total} | {cell.matches_present} | {cell.coverage_ratio:.2%} |"
        )
    lines.extend(
        [
            "",
            "## Odds by league / season / market / bookmaker / quote type",
            "",
            "| Competition | Season | Market | Bookmaker | Quote type | Observations | Research-only |",
            "|---|---|---|---|---|---:|---|",
        ]
    )
    lines.extend(_odds_breakdown_lines(observations, odds_report))
    timestamped = sum(row.matches_with_timestamped for row in odds_report.market_coverage)
    lines.extend(
        [
            "",
            "## Odds quality",
            "",
            "| Tier | Observations | Matches | Meaning |",
            "|---|---:|---:|---|",
        ]
    )
    for row in odds_report.quality_summary:
        lines.append(
            f"| {row.quality_tier} | {row.observations} | {row.matches} | {row.meaning} |"
        )
    lines.extend(
        [
            "",
            f"Timestamped-path matches across markets (must stay 0 for this source): **{timestamped}**",
            f"Opening→closing same-line price pairs (implied-probability/logit): **{len(moves)}**",
            f"Opening→closing line shifts (AH/line changed; not treated as price moves): **{len(line_shifts)}**",
            "",
            "Synthetic test fixtures are separate (`docs/examples/historical-odds-coverage.md`) and are not this backfill.",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _odds_breakdown_lines(observations, odds_report) -> list[str]:
    counts: dict[tuple[str, str, str, str, str], int] = defaultdict(int)
    research: dict[tuple[str, str, str, str], str] = {}
    for observation in observations:
        if observation.decimal_odds is None:
            continue
        key = (
            observation.competition_code,
            observation.season,
            observation.market_family.value,
            observation.bookmaker or "",
            observation.quote_type.value,
        )
        counts[key] += 1
        research[key[:4]] = "yes" if observation.metadata.get("research_only") else "no"
    lines: list[str] = []
    for key, value in sorted(counts.items()):
        competition, season, market, book, quote_type = key
        lines.append(
            f"| {competition} | {season} | {market} | {book} | {quote_type} | {value} | "
            f"{research.get(key[:4], 'no')} |"
        )
    if not lines:
        lines.append("| — | — | — | — | — | 0 | — |")
    # Also list market coverage rows that have observations
    lines.extend(
        [
            "",
            "### Market coverage (repository matches)",
            "",
            "| Competition | Season | Market | Matches | With market | Opening 1X2 | Closing 1X2 |",
            "|---|---|---|---:|---:|---:|---:|",
        ]
    )
    for row in odds_report.market_coverage:
        if row.matches_in_repository == 0:
            continue
        lines.append(
            f"| {row.competition_code} | {row.season} | {row.market_family} | "
            f"{row.matches_in_repository} | {row.matches_with_market} | "
            f"{row.matches_with_opening_1x2} | {row.matches_with_closing_1x2} |"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill PL/Championship 2021/22–2025/26 from public Football-Data.co.uk CSVs"
    )
    parser.add_argument("--facts-db", type=Path, required=True)
    parser.add_argument("--odds-db", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--local-dir",
        type=Path,
        default=None,
        help="Directory containing {season_code}/E0.csv and E1.csv (or {code}_{div}.csv)",
    )
    parser.add_argument("--fetch", action="store_true", help="GET the public season CSVs")
    parser.add_argument("--min-rows", type=int, default=None, help="Override truncated-file floor (tests only)")
    parser.add_argument(
        "--max-exceptions",
        type=int,
        default=MAX_MAPPING_EXCEPTIONS,
        help="Fail if mapping exceptions exceed this count per file",
    )
    parser.add_argument(
        "--seasons",
        default=",".join(BACKFILL_SEASON_CODES),
        help="Comma-separated Football-Data season codes (default all five)",
    )
    args = parser.parse_args(argv)
    season_codes = tuple(code.strip() for code in args.seasons.split(",") if code.strip())
    try:
        result = run_backfill(
            facts_db=args.facts_db,
            odds_db=args.odds_db,
            output_dir=args.output_dir,
            local_dir=args.local_dir,
            fetch=args.fetch,
            min_rows=args.min_rows,
            max_exceptions=args.max_exceptions,
            season_codes=season_codes,
        )
    except (BackfillError, FootballDataFileError) as error:
        print(f"backfill failed: {error}", file=sys.stderr)
        return 1
    print(f"Wrote {result.coverage_markdown}")
    print(f"Wrote {result.facts_xlsx}")
    print(f"Wrote {result.odds_xlsx}")
    for item in result.files:
        print(
            f"{item.season_code}/{item.div}: rows={item.csv_rows} facts={item.facts_matches} "
            f"odds_new={item.odds_observations_created} exceptions={item.mapping_exceptions}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

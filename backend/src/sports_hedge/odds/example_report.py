from __future__ import annotations

from pathlib import Path

from sports_hedge.odds.adapters.smarkets import smarkets_limitations
from sports_hedge.odds.adapters.synthetic import SyntheticOddsAdapter
from sports_hedge.odds.coverage import CoverageReport, build_coverage_report
from sports_hedge.odds.excel import export_odds_workbook
from sports_hedge.odds.ingestion import OddsIngestionService
from sports_hedge.odds.repository import SqliteOddsRepository


def generate_example_artifacts(output_dir: str | Path) -> tuple[CoverageReport, Path, Path]:
    """Ingest synthetic 2025/26 fixtures and write markdown + xlsx examples."""

    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    repository = SqliteOddsRepository()
    service = OddsIngestionService(repository)
    service.ingest_adapter(SyntheticOddsAdapter())
    report = build_coverage_report(
        matches=repository.list_matches(),
        observations=repository.list_observations(),
        exceptions=repository.list_exceptions(),
        smarkets_limitations=smarkets_limitations(),
    )
    markdown_path = destination / "historical-odds-coverage.md"
    markdown_path.write_text(render_coverage_markdown(report), encoding="utf-8")
    workbook_path = export_odds_workbook(
        destination / "historical-odds-export.xlsx",
        observations=repository.list_observations(),
        report=report,
    )
    repository.close()
    return report, markdown_path, workbook_path


def render_coverage_markdown(report: CoverageReport) -> str:
    lines = [
        "# Historical odds coverage example",
        "",
        "Generated from the in-repo **synthetic** adapter for Premier League,",
        "Championship and La Liga 2025/26. The SQLite repository is the source of",
        "truth; this document is a review snapshot.",
        "",
        "## Universe",
        "",
    ]
    for item in report.universe:
        lines.append(f"- {item}")
    lines.extend(
        [
            "",
            f"- Matches in repository: **{report.matches_in_repository}**",
            f"- Odds observations: **{report.observations}**",
            f"- Unresolved mapping count: **{report.unresolved_mapping_count}**",
            "",
            "## Market coverage",
            "",
            "| Competition | Season | Market | Matches | With market | Opening 1X2 | Closing 1X2 | Timestamped | Liquidity | Missing | Calendar gap |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report.market_coverage:
        if not row.matches_in_repository and row.competition_code != "champions-league":
            continue
        lines.append(
            "| {comp} | {season} | {family} | {total} | {with_m} | {open_} | {close} | {ts} | {liq} | {miss} | {gap} |".format(
                comp=row.competition_code,
                season=row.season,
                family=row.market_family,
                total=row.matches_in_repository,
                with_m=row.matches_with_market,
                open_=row.matches_with_opening_1x2,
                close=row.matches_with_closing_1x2,
                ts=row.matches_with_timestamped,
                liq=row.matches_with_liquidity,
                miss=row.missing_match_count,
                gap="" if row.calendar_gap is None else row.calendar_gap,
            )
        )
    lines.extend(
        [
            "",
            "## Source coverage",
            "",
            "| Source | Required | Available | Observations | Matches | Timestamped | Liquidity | Notes |",
            "|---|---|---|---:|---:|---:|---:|---|",
        ]
    )
    for row in report.source_coverage:
        lines.append(
            f"| {row.source} | {row.required} | {row.available} | {row.observations} | "
            f"{row.matches} | {row.timestamped_observations} | {row.liquidity_observations} | "
            f"{row.notes.replace('|', '/')} |"
        )
    lines.extend(
        [
            "",
            "## Quality summary",
            "",
            "| Tier | Observations | Matches | Meaning |",
            "|---|---:|---:|---|",
        ]
    )
    for row in report.quality_summary:
        lines.append(
            f"| {row.quality_tier} | {row.observations} | {row.matches} | {row.meaning} |"
        )
    limitations = report.smarkets_limitations
    lines.extend(
        [
            "",
            "## Smarkets limitations",
            "",
            f"- Required: `{limitations.get('required')}`",
            f"- Live historical archive: `{limitations.get('public_api_historical_archive')}`",
            f"- Network fetch enabled: `{limitations.get('network_fetch_enabled')}`",
            f"- Supported ingest: {limitations.get('supported_ingest')}",
            "",
            str(limitations.get("notes", "")),
            "",
        ]
    )
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[4] / "docs" / "examples"
    generate_example_artifacts(root)
    print(f"Wrote example coverage artifacts under {root}")

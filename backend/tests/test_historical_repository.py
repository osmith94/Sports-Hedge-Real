from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sports_hedge.config import Settings
from sports_hedge.historical.adapters import SyntheticHistoricalAdapter
from sports_hedge.historical.catalog import (
    CHAMPIONS_LEAGUE,
    CHAMPIONSHIP,
    LA_LIGA,
    PREMIER_LEAGUE,
    HistoricalCatalog,
)
from sports_hedge.historical.coverage import CoverageReporter
from sports_hedge.historical.errors import HistoricalConflictError, HistoricalMappingError
from sports_hedge.historical.excel import SHEET_NAMES, HistoricalExcelExporter, read_sheet_rows
from sports_hedge.historical.ingestion import HistoricalIngestionService
from sports_hedge.historical.models import MatchStatus, SourceMatchPayload, Team
from sports_hedge.historical.repository import SqliteHistoricalRepository

SEASON = "2025/26"


def _ingest_universe(repository: SqliteHistoricalRepository) -> list[str]:
    service = HistoricalIngestionService(repository)
    adapter = SyntheticHistoricalAdapter()
    match_ids: list[str] = []
    for competition in (PREMIER_LEAGUE, CHAMPIONSHIP, LA_LIGA):
        match_ids.extend(
            service.ingest_adapter(
                adapter,
                competition_id=competition.competition_id,
                season_label=SEASON,
            )
        )
    return match_ids


def test_synthetic_fixtures_persist_and_query_across_initial_universe() -> None:
    repository = SqliteHistoricalRepository(":memory:")
    match_ids = _ingest_universe(repository)

    assert len(match_ids) == 3
    premier = repository.list_matches(competition_id="premier-league", season_id="premier-league:2025-26")
    championship = repository.list_matches(competition_id="championship")
    la_liga = repository.list_matches(competition_id="la-liga")

    assert [match.home_team_name for match in premier] == ["Arsenal"]
    assert championship[0].away_team_name == "Leicester City"
    assert la_liga[0].home_ft_goals == 3
    assert la_liga[0].away_ft_goals == 2
    assert repository.get_match(premier[0].match_id) is not None
    assert CHAMPIONS_LEAGUE.competition_id == "champions-league"
    assert HistoricalCatalog().resolve_competition("UEFA Champions League").competition_id == (
        "champions-league"
    )


def test_duplicate_ingestion_does_not_create_duplicate_matches_or_events() -> None:
    repository = SqliteHistoricalRepository(":memory:")
    _ingest_universe(repository)
    _ingest_universe(repository)

    assert repository.count_matches() == 3
    assert repository.count_events() == 10
    sources = repository.list_source_records()
    assert len(sources) == 3
    assert {item.source_match_id for item in sources} == {
        "syn-pl-2025-001",
        "syn-ch-2025-001",
        "syn-ll-2025-001",
    }


def test_corners_and_cards_are_queryable_by_match_team_competition_and_season() -> None:
    repository = SqliteHistoricalRepository(":memory:")
    _ingest_universe(repository)
    premier = repository.list_matches(competition_id="premier-league")[0]

    by_match = repository.list_team_stats(match_id=premier.match_id)
    home_stats = next(row for row in by_match if row.is_home)
    away_stats = next(row for row in by_match if not row.is_home)
    assert home_stats.corners == 7
    assert away_stats.yellow_cards == 3
    assert away_stats.red_cards == 1

    arsenal_rows = repository.list_team_stats(team_id=premier.home_team_id)
    assert arsenal_rows[0].corners == 7

    premier_season_rows = repository.list_team_stats(
        competition_id="premier-league",
        season_id="premier-league:2025-26",
    )
    assert {row.team_name for row in premier_season_rows} == {"Arsenal", "Chelsea"}


def test_source_provenance_is_retained() -> None:
    repository = SqliteHistoricalRepository(":memory:")
    _ingest_universe(repository)
    premier = repository.list_matches(competition_id="premier-league")[0]
    records = repository.list_source_records(match_id=premier.match_id)

    assert len(records) == 1
    record = records[0]
    assert record.source_name == "synthetic-fixtures"
    assert record.source_match_id == "syn-pl-2025-001"
    assert record.source_url.endswith("pl-2025-001")
    assert record.retrieved_at == datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    assert record.source_timestamp is not None
    assert len(record.raw_payload_hash) == 64
    assert record.raw_payload["score"] == "2-1"
    assert record.confidence <= 1.0


def test_coverage_report_exposes_missing_fields() -> None:
    repository = SqliteHistoricalRepository(":memory:")
    _ingest_universe(repository)
    report = CoverageReporter(repository).report()

    championship_ht = next(
        cell
        for cell in report
        if cell.competition_id == "championship" and cell.field_name == "ht_score"
    )
    la_liga_corners = next(
        cell for cell in report if cell.competition_id == "la-liga" and cell.field_name == "corners"
    )
    premier_lineups = next(
        cell
        for cell in report
        if cell.competition_id == "premier-league" and cell.field_name == "lineups"
    )

    assert championship_ht.matches_present == 0
    assert championship_ht.missing_match_ids
    assert la_liga_corners.matches_present == 0
    assert premier_lineups.coverage_ratio == 1.0


def test_excel_export_is_deterministic_and_reads_normalized_repository(tmp_path) -> None:
    first_repo = SqliteHistoricalRepository(":memory:")
    second_repo = SqliteHistoricalRepository(":memory:")
    _ingest_universe(first_repo)
    _ingest_universe(second_repo)

    first_path = HistoricalExcelExporter(first_repo).export(tmp_path / "first.xlsx")
    second_path = HistoricalExcelExporter(second_repo).export(tmp_path / "second.xlsx")

    for sheet in SHEET_NAMES:
        assert read_sheet_rows(first_path, sheet) == read_sheet_rows(second_path, sheet)

    matches = read_sheet_rows(first_path, "Matches")
    assert ["Arsenal", "Chelsea", 2, 1, 1, 0] == matches[1][4:10]
    stats = read_sheet_rows(first_path, "Team Stats")
    assert any(row[3] == "Chelsea" and row[5] == 4 for row in stats)
    assert not any("synthetic-fixtures" == cell for row in matches for cell in row)


def test_ambiguous_team_mapping_fails_closed() -> None:
    catalog = HistoricalCatalog()
    catalog.register_team(Team(team_id="manchester-united", canonical_name="Manchester United"))
    catalog.register_team(Team(team_id="newcastle-united", canonical_name="Newcastle United"))
    catalog.add_team_alias("United", "manchester-united")
    catalog.add_team_alias("United", "newcastle-united")

    with pytest.raises(HistoricalMappingError, match="Ambiguous team"):
        catalog.resolve_team("United")

    repository = SqliteHistoricalRepository(":memory:")
    service = HistoricalIngestionService(repository, catalog)
    payload = SourceMatchPayload(
        source_name="synthetic-fixtures",
        source_match_id="ambiguous-1",
        retrieved_at=datetime(2026, 9, 11, 12, 0, tzinfo=UTC),
        competition_name="Premier League",
        season_label=SEASON,
        kickoff_utc=datetime(2025, 8, 20, 19, 0, tzinfo=UTC),
        home_team="United",
        away_team="Arsenal",
        status=MatchStatus.FINISHED,
        home_ft_goals=1,
        away_ft_goals=0,
    )
    with pytest.raises(HistoricalMappingError, match="Ambiguous team"):
        service.ingest(payload)


def test_conflicting_scores_fail_closed() -> None:
    repository = SqliteHistoricalRepository(":memory:")
    service = HistoricalIngestionService(repository)
    adapter = SyntheticHistoricalAdapter()
    original = adapter.fetch_matches("premier-league", SEASON)[0]
    service.ingest(original)

    conflict = original.model_copy(update={"home_ft_goals": 9, "source_match_id": "syn-pl-2025-001-b"})
    with pytest.raises(HistoricalConflictError, match="Conflicting home_ft_goals"):
        service.ingest(conflict)


def test_settings_include_historical_database_path() -> None:
    settings = Settings.model_validate({})
    assert settings.historical_db_path.endswith("historical_football.sqlite")
    assert settings.sports_hedge_execution_enabled is False

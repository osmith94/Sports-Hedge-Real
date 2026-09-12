from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.backfill.football_data import BackfillError, run_backfill
from sports_hedge.historical.catalog import HistoricalCatalog
from sports_hedge.historical.ingestion import HistoricalIngestionService
from sports_hedge.historical.repository import SqliteHistoricalRepository
from sports_hedge.odds.adapters.football_data import FootballDataCsvAdapter
from sports_hedge.odds.ingestion import OddsIngestionService
from sports_hedge.odds.models import QualityTier, QuoteType
from sports_hedge.odds.repository import SqliteOddsRepository
from sports_hedge.sources.football_data import (
    BACKFILL_SEASON_CODES,
    FootballDataFileError,
    public_csv_url,
    rows_from_csv,
    season_label_from_code,
    validate_season_rows,
)

FIXTURES = Path(__file__).parent / "fixtures"
RETRIEVED = datetime(2026, 9, 12, 11, 0, tzinfo=UTC)


def test_catalog_resolves_football_data_labels() -> None:
    catalog = HistoricalCatalog()
    assert catalog.resolve_team("Man City").canonical_name == "Manchester City"
    assert catalog.resolve_team("Nott'm Forest").canonical_name == "Nottingham Forest"
    assert catalog.resolve_team("Sheffield Weds").canonical_name == "Sheffield Wednesday"
    with pytest.raises(Exception, match="Unknown team"):
        catalog.resolve_team("Not A Real Club")


def test_rows_from_csv_strip_bom_and_validate() -> None:
    text = (FIXTURES / "football_data_e0_sample.csv").read_text(encoding="utf-8")
    rows = rows_from_csv(text)
    assert rows[0]["Div"] == "E0"
    assert rows[0]["HomeTeam"] == "Liverpool"
    validate_season_rows(rows, div="E0", min_rows=2)
    with pytest.raises(FootballDataFileError, match="empty"):
        validate_season_rows([], div="E0", min_rows=1)
    with pytest.raises(FootballDataFileError, match="truncated"):
        validate_season_rows(rows, div="E0", min_rows=50)


def test_backfill_from_local_fixtures_is_idempotent(tmp_path: Path) -> None:
    local = tmp_path / "csv"
    local.mkdir()
    (local / "E0.csv").write_bytes((FIXTURES / "football_data_e0_sample.csv").read_bytes())
    (local / "E1.csv").write_bytes((FIXTURES / "football_data_e1_sample.csv").read_bytes())
    first = run_backfill(
        facts_db=tmp_path / "facts.sqlite",
        odds_db=tmp_path / "odds.sqlite",
        output_dir=tmp_path / "out",
        local_dir=local,
        fetch=False,
        retrieved_at=RETRIEVED,
        min_rows=1,
        season_codes=("2526",),
    )
    assert {item.div: item.csv_rows for item in first.divisions} == {"E0": 2, "E1": 2}
    assert all(item.mapping_exceptions == 0 for item in first.divisions)
    assert first.coverage_markdown.exists()
    assert "tier C" in first.coverage_markdown.read_text(encoding="utf-8").casefold() or "opening/closing" in first.coverage_markdown.read_text(encoding="utf-8")

    facts = SqliteHistoricalRepository(tmp_path / "facts.sqlite")
    odds = SqliteOddsRepository(tmp_path / "odds.sqlite")
    facts_matches = facts.list_matches()
    observations = odds.list_observations()
    assert len(facts_matches) == 4
    odds_quotes = [item for item in observations if item.decimal_odds is not None]
    assert odds_quotes
    assert all(item.quality_tier == QualityTier.C for item in odds_quotes)
    assert all(item.observed_at is None for item in odds_quotes)
    assert QuoteType.TIMESTAMPED not in {item.quote_type for item in odds_quotes}
    assert any(item.implied_logit() is not None for item in odds_quotes)

    pl_facts = [item for item in facts_matches if item.competition_id == "premier-league"]
    pl_odds = [item for item in odds.list_matches() if item.competition_code == "premier_league"]
    assert {item.match_id for item in facts_matches if item.competition_id == "premier-league"} == {
        item.canonical_match_id for item in pl_odds
    }
    assert pl_facts[0].home_ht_goals is not None
    assert facts.list_events() == []
    facts.close()
    odds.close()

    second = run_backfill(
        facts_db=tmp_path / "facts.sqlite",
        odds_db=tmp_path / "odds.sqlite",
        output_dir=tmp_path / "out2",
        local_dir=local,
        fetch=False,
        retrieved_at=RETRIEVED,
        min_rows=1,
        season_codes=("2526",),
    )
    assert all(item.odds_observations_created == 0 for item in second.divisions)
    assert all(item.odds_observations_duplicate > 0 for item in second.divisions)
    facts = SqliteHistoricalRepository(tmp_path / "facts.sqlite")
    assert len(facts.list_matches()) == 4
    facts.close()


def test_unknown_team_fails_closed_during_facts_ingest() -> None:
    csv_text = (FIXTURES / "football_data_e0_sample.csv").read_text(encoding="utf-8")
    bad = csv_text.replace("Bournemouth", "Not A Real Club", 1)
    repository = SqliteHistoricalRepository(":memory:")
    service = HistoricalIngestionService(repository)
    from sports_hedge.historical.football_data import FootballDataHistoricalAdapter

    adapter = FootballDataHistoricalAdapter(
        bad, retrieved_at=RETRIEVED, div="E0", season_label="2025/26"
    )
    with pytest.raises(Exception, match="Unknown team"):
        service.ingest_adapter(adapter, competition_id="premier-league", season_label="2025/26")


def test_odds_opening_closing_are_not_timestamped_paths() -> None:
    csv_text = (FIXTURES / "football_data_e0_sample.csv").read_text(encoding="utf-8")
    service = OddsIngestionService(SqliteOddsRepository())
    result = service.ingest_adapter(FootballDataCsvAdapter(csv_text, retrieved_at=RETRIEVED))
    assert result.exceptions == 0
    observations = service.repository.list_observations()
    priced = [item for item in observations if item.decimal_odds is not None]
    assert {item.quote_type for item in priced} <= {QuoteType.OPENING, QuoteType.CLOSING}
    assert all(item.observed_at is None for item in priced)
    assert all(item.liquidity is None for item in priced)


def test_season_codes_are_mapped_to_public_urls() -> None:
    assert BACKFILL_SEASON_CODES == ("2122", "2223", "2324", "2425", "2526")
    assert season_label_from_code("2122") == "2021/22"
    assert public_csv_url("E0", "2122") == "https://www.football-data.co.uk/mmz4281/2122/E0.csv"
    assert public_csv_url("E1", "2526") == "https://www.football-data.co.uk/mmz4281/2526/E1.csv"


def test_header_driven_books_and_season_url() -> None:
    from sports_hedge.odds.movement import classify_open_close, pair_open_close
    from sports_hedge.sources.football_data import public_csv_url

    csv_text = (FIXTURES / "football_data_e0_sample.csv").read_text(encoding="utf-8")
    url = public_csv_url("E0", "2526")
    service = OddsIngestionService(SqliteOddsRepository())
    service.ingest_adapter(
        FootballDataCsvAdapter(
            csv_text,
            retrieved_at=RETRIEVED,
            season_label="2025/26",
            source_url=url,
            div="E0",
        )
    )
    priced = [item for item in service.repository.list_observations() if item.decimal_odds is not None]
    assert {item.source_url for item in priced} == {url}
    books = {item.bookmaker for item in priced}
    assert {"b365", "ps", "max", "avg"} <= books
    assert any(item.metadata.get("research_only") for item in priced if item.bookmaker == "max")
    assert any(not item.metadata.get("research_only") for item in priced if item.bookmaker == "b365")
    moves = pair_open_close(priced)
    assert moves
    assert all(item.observed_at is None for item in priced)
    assert any(move.implied_logit_delta is not None for move in moves)
    classified = classify_open_close(priced)
    ah_price = [item for item in classified.price_moves if item.market_family == "asian_handicap"]
    ah_shifts = [item for item in classified.line_shifts if item.market_family == "asian_handicap"]
    assert ah_price
    assert any(item.line == Decimal("-0.25") and item.implied_logit_delta is not None for item in ah_price)
    assert ah_shifts
    assert all(item.opening_line != item.closing_line for item in ah_shifts)
    assert any(item.opening_line == Decimal("-1.5") and item.closing_line == Decimal("-1.75") for item in ah_shifts)


def test_2021_22_emits_william_hill_open_and_close() -> None:
    csv_text = (FIXTURES / "football_data_e0_2122_sample.csv").read_text(encoding="utf-8")
    records = FootballDataCsvAdapter(
        csv_text,
        retrieved_at=RETRIEVED,
        season_label="2021/22",
        source_url="https://www.football-data.co.uk/mmz4281/2122/E0.csv",
        div="E0",
    ).fetch()
    priced = [item for item in records if item.decimal_odds is not None]
    assert {item.season for item in priced} == {"2021/22"}
    assert all(item.source_url and "2122/E0.csv" in item.source_url for item in priced)
    wh = [item for item in priced if item.bookmaker == "wh"]
    assert {item.quote_type for item in wh} == {QuoteType.OPENING, QuoteType.CLOSING}


def test_catalog_resolves_older_championship_labels() -> None:
    catalog = HistoricalCatalog()
    assert catalog.resolve_team("Peterboro").canonical_name == "Peterborough United"
    assert catalog.resolve_team("Luton").canonical_name == "Luton Town"


def test_backfill_requires_operator_source(tmp_path: Path) -> None:
    with pytest.raises(BackfillError, match="operator-invoked"):
        run_backfill(
            facts_db=tmp_path / "facts.sqlite",
            odds_db=tmp_path / "odds.sqlite",
            output_dir=tmp_path / "out",
            local_dir=None,
            fetch=False,
            min_rows=1,
            season_codes=("2526",),
        )

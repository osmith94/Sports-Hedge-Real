from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel

from sports_hedge.odds.adapters.base import OddsSourceAdapter
from sports_hedge.odds.mapping import OddsMappingError, map_raw_record, mapping_exception_for
from sports_hedge.odds.models import CanonicalMatchFact, RawOddsRecord
from sports_hedge.odds.repository import SqliteOddsRepository


class IngestBatchResult(BaseModel):
    source: str
    observations_created: int
    observations_duplicate: int
    matches_created: int
    exceptions: int
    records_seen: int


class OddsIngestionService:
    """Idempotent, restartable import into the normalized odds repository."""

    def __init__(self, repository: SqliteOddsRepository) -> None:
        self.repository = repository

    def ingest_adapter(self, adapter: OddsSourceAdapter) -> IngestBatchResult:
        records = list(adapter.fetch())
        result = self.ingest_records(adapter.source, records)
        self.repository.set_checkpoint(
            adapter.source,
            cursor_value=f"records:{result.records_seen}",
            updated_at=datetime.now(UTC),
        )
        return result

    def ingest_records(self, source: str, records: Sequence[RawOddsRecord]) -> IngestBatchResult:
        created = 0
        duplicates = 0
        matches_created = 0
        exceptions = 0
        for record in records:
            try:
                observation = map_raw_record(record)
            except OddsMappingError as error:
                self.repository.insert_exception(mapping_exception_for(record, error))
                exceptions += 1
                continue
            match = CanonicalMatchFact(
                canonical_match_id=observation.canonical_match_id,
                competition_code=observation.competition_code,
                season=observation.season,
                home_team=observation.home_team,
                away_team=observation.away_team,
                kickoff_utc=observation.kickoff_utc,
                kickoff_precision=observation.kickoff_precision,
                source=observation.source,
                source_match_id=observation.metadata.get("source_match_id"),
                home_goals=record.home_goals,
                away_goals=record.away_goals,
                retrieved_at=observation.retrieved_at,
                metadata={"source_reference": observation.source_reference},
            )
            if self.repository.upsert_match(match):
                matches_created += 1
            if self.repository.insert_observation(observation):
                created += 1
            else:
                duplicates += 1
        return IngestBatchResult(
            source=source,
            observations_created=created,
            observations_duplicate=duplicates,
            matches_created=matches_created,
            exceptions=exceptions,
            records_seen=len(records),
        )

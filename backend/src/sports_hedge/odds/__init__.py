"""Source-neutral historical odds repository and coverage framework."""

from sports_hedge.odds.adapters import (
    FootballDataCsvAdapter,
    SmarketsHistoricalAdapter,
    SyntheticOddsAdapter,
    smarkets_limitations,
)
from sports_hedge.odds.coverage import CoverageReport, build_coverage_report
from sports_hedge.odds.excel import export_odds_workbook
from sports_hedge.odds.ingestion import IngestBatchResult, OddsIngestionService
from sports_hedge.odds.models import OddsObservation, QualityTier, QuoteType
from sports_hedge.odds.repository import SqliteOddsRepository

__all__ = [
    "CoverageReport",
    "FootballDataCsvAdapter",
    "IngestBatchResult",
    "OddsIngestionService",
    "OddsObservation",
    "QualityTier",
    "QuoteType",
    "SmarketsHistoricalAdapter",
    "SqliteOddsRepository",
    "SyntheticOddsAdapter",
    "build_coverage_report",
    "export_odds_workbook",
    "smarkets_limitations",
]

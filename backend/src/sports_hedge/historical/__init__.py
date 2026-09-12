"""Source-neutral historical football facts repository."""

from sports_hedge.historical.adapters import HistoricalSourceAdapter, SyntheticHistoricalAdapter
from sports_hedge.historical.football_data import FootballDataHistoricalAdapter
from sports_hedge.historical.catalog import (
    CHAMPIONS_LEAGUE,
    CHAMPIONSHIP,
    LA_LIGA,
    PREMIER_LEAGUE,
    HistoricalCatalog,
)
from sports_hedge.historical.coverage import CoverageReporter
from sports_hedge.historical.excel import HistoricalExcelExporter
from sports_hedge.historical.ingestion import HistoricalIngestionService
from sports_hedge.historical.repository import SqliteHistoricalRepository

__all__ = [
    "CHAMPIONSHIP",
    "CHAMPIONS_LEAGUE",
    "LA_LIGA",
    "PREMIER_LEAGUE",
    "CoverageReporter",
    "FootballDataHistoricalAdapter",
    "HistoricalCatalog",
    "HistoricalExcelExporter",
    "HistoricalIngestionService",
    "HistoricalSourceAdapter",
    "SqliteHistoricalRepository",
    "SyntheticHistoricalAdapter",
]

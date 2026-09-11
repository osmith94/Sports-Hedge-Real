from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sports_hedge.odds.models import RawOddsRecord


class SmarketsHistoryUnavailable(RuntimeError):
    """Raised when live/historical Smarkets history is requested without support."""


class SmarketsHistoricalAdapter:
    """Optional Smarkets adapter.

    Public Smarkets REST documents current events, markets and live odds. It
    does not publish a supported bulk historical-odds archive. This adapter
    therefore never calls Smarkets over the network. Authorised local snapshot
    files may be ingested when ``snapshot_path`` is provided; otherwise the
    source is skipped and coverage reports it as unavailable.
    """

    source = "smarkets"
    required = False

    def __init__(self, snapshot_path: str | Path | None = None) -> None:
        self._snapshot_path = Path(snapshot_path) if snapshot_path else None

    @property
    def configured(self) -> bool:
        return self._snapshot_path is not None and self._snapshot_path.is_file()

    def limitations(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "required": False,
            "live_history_supported": False,
            "public_api_historical_archive": False,
            "network_fetch_enabled": False,
            "supported_ingest": "authorised local snapshot files only",
            "quote_types_if_present": ["timestamped", "opening", "closing"],
            "quality_if_sparse": "C",
            "notes": (
                "Smarkets public/trading API surfaces current markets and odds. "
                "No authorised historical odds dump is wired here. Sparse "
                "open/close snapshots, when supplied locally, are stored as "
                "quality C without inventing timestamps or liquidity."
            ),
        }

    def fetch(self) -> Sequence[RawOddsRecord]:
        if not self.configured:
            return []
        raise SmarketsHistoryUnavailable(
            "Local Smarkets snapshots must be mapped by an authorised dump "
            "adapter; this optional adapter does not scrape Smarkets"
        )


def smarkets_limitations() -> dict[str, Any]:
    return SmarketsHistoricalAdapter().limitations()

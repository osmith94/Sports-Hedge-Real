from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from sports_hedge.market_intelligence.ingestion.contracts import ProviderEventRecord


class ProviderNotConfiguredError(RuntimeError):
    """Raised when a future sports-data or news adapter is invoked without config."""


class UnconfiguredProviderFeed:
    """Adapter seam for a future official sports-data or news provider.

    Analytics depend only on ``MarketEventFeed``. Concrete providers must be
    configured explicitly; this class never scrapes or guesses credentials.
    """

    def __init__(self, provider: str) -> None:
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider

    def fetch(self, *, since: datetime | None = None) -> Sequence[ProviderEventRecord]:
        del since
        raise ProviderNotConfiguredError(
            f"Provider '{self.provider}' is an adapter seam only; "
            "configure an official feed before fetching events"
        )


class OfficialSportsDataFeed(UnconfiguredProviderFeed):
    def __init__(self) -> None:
        super().__init__("official_sports_data")


class NewsProviderFeed(UnconfiguredProviderFeed):
    def __init__(self) -> None:
        super().__init__("news_provider")

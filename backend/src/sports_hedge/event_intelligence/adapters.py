from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from sports_hedge.event_intelligence.models import EventIntelligenceFact


class EventIntelligenceFeed(Protocol):
    """Adapter seam for later approved, already-configured sources."""

    @property
    def provider(self) -> str: ...

    def fetch(self, *, since: datetime | None = None) -> Sequence[EventIntelligenceFact]: ...


class ProviderNotConfiguredError(RuntimeError):
    """Raised when a future approved source is invoked without configuration."""


class UnconfiguredApprovedSourceFeed:
    """Placeholder adapter. Never scrapes, never invents credentials."""

    def __init__(self, provider: str) -> None:
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider

    def fetch(self, *, since: datetime | None = None) -> Sequence[EventIntelligenceFact]:
        del since
        raise ProviderNotConfiguredError(
            f"Provider '{self.provider}' is an adapter seam only; "
            "configure an approved source before fetching event intelligence"
        )


class FixtureEventIntelligenceFeed:
    """Deterministic in-process feed for tests. Explicitly fixture provenance."""

    def __init__(self, records: Sequence[EventIntelligenceFact], *, provider: str) -> None:
        self._records = list(records)
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider

    def fetch(self, *, since: datetime | None = None) -> Sequence[EventIntelligenceFact]:
        if since is None:
            return list(self._records)
        return [record for record in self._records if record.published_at >= since]

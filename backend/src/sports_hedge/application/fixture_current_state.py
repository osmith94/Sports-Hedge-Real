from __future__ import annotations

from sports_hedge.application.collector import (
    CollectionReport,
    DiscoveredFixture,
    FixtureDetailReadModel,
    FixtureMarketInventoryRow,
)
from sports_hedge.paper.models import PaperScanDecision


class FixtureCurrentStateStore:
    """Process-memory canonical fixture current-state (v1).

    Coordinator-owned. Dual-cadence (#158/#159) will upsert HOT/UNIVERSE lanes
    into this same store with TTL merge. This slice atomically replaces the
    current collection generation and resolves explicit identity aliases.

    Do not fuzzy-match fixture names. Do not fabricate demo fixtures.
    """

    def __init__(self) -> None:
        self._generation = 0
        self._fixtures: dict[str, DiscoveredFixture] = {}
        self._markets: dict[str, list[FixtureMarketInventoryRow]] = {}
        self._aliases: dict[str, str] = {}

    def clear(self) -> None:
        self._generation = 0
        self._fixtures = {}
        self._markets = {}
        self._aliases = {}

    @property
    def generation(self) -> int:
        return self._generation

    def replace_from_report(self, report: CollectionReport) -> None:
        fixtures = {
            fixture.canonical_event_id: fixture for fixture in report.discovered_fixtures
        }
        markets = {
            event_id: list(rows) for event_id, rows in report.fixture_markets.items()
        }
        aliases = _aliases_from_report(report, fixtures)
        for alias, canonical_id in self._aliases.items():
            if canonical_id in fixtures and alias not in aliases:
                aliases[alias] = canonical_id
        self._generation += 1
        self._fixtures = fixtures
        self._markets = markets
        self._aliases = aliases

    def resolve_canonical_id(self, identity: str) -> str | None:
        wanted = identity.strip()
        if not wanted:
            return None
        canonical_id = self._aliases.get(wanted)
        if canonical_id is None and wanted in self._fixtures:
            return wanted
        if canonical_id is None or canonical_id not in self._fixtures:
            return None
        return canonical_id

    def identities_for(self, identity: str) -> frozenset[str]:
        canonical_id = self.resolve_canonical_id(identity)
        if canonical_id is None:
            return frozenset()
        aliases = {alias for alias, target in self._aliases.items() if target == canonical_id}
        aliases.add(canonical_id)
        return frozenset(aliases)

    def detail(self, identity: str) -> FixtureDetailReadModel | None:
        canonical_id = self.resolve_canonical_id(identity)
        if canonical_id is None:
            return None
        fixture = self._fixtures[canonical_id]
        return FixtureDetailReadModel(
            fixture=fixture,
            markets=list(self._markets.get(canonical_id, [])),
        )


def _aliases_from_report(
    report: CollectionReport,
    fixtures: dict[str, DiscoveredFixture],
) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for fixture in fixtures.values():
        aliases[fixture.canonical_event_id] = fixture.canonical_event_id
        source_id = str(fixture.source_event_id).strip()
        if source_id:
            aliases[source_id] = fixture.canonical_event_id
    for alias, canonical_id in report.fixture_identity_aliases.items():
        key = alias.strip()
        target = canonical_id.strip()
        if key and target in fixtures:
            aliases[key] = target
    for decision in report.paper_decisions:
        _alias_decision(aliases, decision, fixtures)
    return aliases


def _alias_decision(
    aliases: dict[str, str],
    decision: PaperScanDecision,
    fixtures: dict[str, DiscoveredFixture],
) -> None:
    cluster_id = (decision.fixture_canonical_event_id or "").strip()
    decision_id = (decision.canonical_event_id or "").strip()
    if cluster_id and cluster_id in fixtures:
        aliases[cluster_id] = cluster_id
        if decision_id:
            aliases[decision_id] = cluster_id
        return
    if decision_id and decision_id in fixtures:
        aliases[decision_id] = decision_id

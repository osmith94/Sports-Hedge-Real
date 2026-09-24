"""#516 composed Phase 2 candidate: identity, fees, and console read model.

Proves the integration did not drop a source row or replace a later invariant
with an earlier one. Catalogue and shard membership are registry facts.
Fee examples use the Phase 1 seed schedule, not live account quotes.
UI checks read the operator console sources that consume backend fields.
PAPER / read-only. No live execution.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from sports_hedge.application.collector import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.application.fixture_clusters import VenueEvent
from sports_hedge.application.target_competitions import (
    OPERATOR_COMPETITION_REGISTRY_VERSION,
    PRINCIPAL_OPERATOR_COMPETITION_COUNT,
    TARGET_COMPETITIONS,
    TargetCompetitionCode,
    operator_competition_catalog,
    resolve_target_competition,
)
from sports_hedge.application.universe_identity_shards import (
    UNRESOLVED_COMPETITION,
    partition_identity_shards,
)
from sports_hedge.domain.football import CanonicalEvent, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction
from sports_hedge.fees.resolver import (
    MATCHBOOK_OVERRIDE_SOURCE,
    MATCHBOOK_PROVIDER_DEFAULT_COMMISSION,
    MATCHBOOK_STANDARD_FOOTBALL_COMMISSION,
    MATCHBOOK_VENUE_COMMISSION_CLASS,
    UnknownRequiredCostError,
    VenueCostResolver,
    phase1_seed_rules,
)
from sports_hedge.matching.events import (
    DEFAULT_EVENT_MATCH_THRESHOLD,
    PAPER_EVENT_MATCH_THRESHOLD,
)
from sports_hedge.matching.identity_graph import DEFAULT_ASSIGNMENT_MARGIN
from sports_hedge.nba.constants import NBA_SPORT
from sports_hedge.ncaab.constants import (
    NCAAB_COMPETITION,
    NCAAB_PAIR_UNAPPROVED_REASON,
    NCAAB_SPORT,
)
from sports_hedge.ncaab.register import NCAAB_PAPER_APPROVED_CELLS
from sports_hedge.ncaab.settlement import ncaab_automatic_settlement_blocker

AS_OF = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
REPO_ROOT = Path(__file__).resolve().parents[2]

# Union of the owner-live NBA catalogue and the #507 / #509 additions.
# A branch overwrite would drop one of these codes or shrink the count.
EXPECTED_CATALOGUE_CODES = frozenset(
    {
        "premier_league",
        "championship",
        "la_liga",
        "carabao_cup",
        "fa_cup",
        "international_friendlies",
        "bundesliga",
        "serie_a",
        "champions_league",
        "europa_league",
        "conference_league",
        "uefa_nations_league",
        "super_lig",
        "mls",
        "league_one",
        "league_two",
        "copa_del_rey",
        "dfb_pokal",
        "coppa_italia",
        "ligue_1",
        "eredivisie",
        "primeira_liga",
        "scottish_premiership",
        "belgian_pro_league",
        "liga_mx",
        "brasileirao",
        "argentina_primera",
        "copa_libertadores",
        "saudi_pro_league",
        "j1_league",
        "south_african_premiership",
        "nfl",
        "nba",
        "ncaab",
        "atp",
        "wta",
    }
)


def _event(
    venue: VenueName,
    source_event_id: str,
    *,
    home: str,
    away: str,
    competition: str,
    sport: str,
) -> VenueEvent:
    canonical = CanonicalEvent(
        sport=sport,
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source_event_id,
    )
    return VenueEvent(
        venue=venue,
        raw={"id": source_event_id, "title": f"{home} vs {away}"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


def test_composed_catalogue_keeps_nba_ncaab_and_nations_league() -> None:
    codes = {item.code for item in TARGET_COMPETITIONS}
    values = {item.code.value for item in TARGET_COMPETITIONS}
    assert len(TARGET_COMPETITIONS) == 36
    assert len(codes) == 36
    assert values == EXPECTED_CATALOGUE_CODES
    assert PRINCIPAL_OPERATOR_COMPETITION_COUNT == 36
    assert OPERATOR_COMPETITION_REGISTRY_VERSION == 8
    assert {
        TargetCompetitionCode.NBA,
        TargetCompetitionCode.NCAAB,
        TargetCompetitionCode.UEFA_NATIONS_LEAGUE,
        TargetCompetitionCode.NFL,
    } <= codes

    catalog = {row["code"]: row for row in operator_competition_catalog()}
    assert set(catalog) == EXPECTED_CATALOGUE_CODES
    for code in ("nba", "ncaab", "uefa_nations_league"):
        assert catalog[code]["selectable"] is True
        assert catalog[code]["default_selected"] is False

    nations = resolve_target_competition("UEFA Nations League")
    assert nations is not None
    aliases = {alias.casefold() for alias in nations.aliases}
    assert {"uefa nations league a", "uefa nations league b", "uefa nations league d"} <= aliases
    assert "uefa nations league c" not in aliases
    assert resolve_target_competition("UEFA Nations League C") is None
    assert resolve_target_competition("CONCACAF Nations League") is None
    assert resolve_target_competition("UEFA Women's Nations League") is None

    ncaab = resolve_target_competition("NCAA Men's Basketball")
    assert ncaab is not None
    assert ncaab.code is TargetCompetitionCode.NCAAB
    assert resolve_target_competition("NCAAW") is None
    assert resolve_target_competition("NCAA Women's Basketball") is None
    assert resolve_target_competition("Women's College Basketball") is None


def test_sharding_keeps_nba_ncaab_and_nations_league_apart() -> None:
    items = [
        _event(
            VenueName.MATCHBOOK,
            "nl-mb",
            home="Netherlands",
            away="Germany",
            competition="UEFA Nations League",
            sport="football",
        ),
        _event(
            VenueName.KALSHI,
            "nba-k",
            home="Boston Celtics",
            away="Detroit Pistons",
            competition="NBA",
            sport=NBA_SPORT,
        ),
        _event(
            VenueName.POLYMARKET,
            "ncaab-pm",
            home="Duke",
            away="Xavier",
            competition=NCAAB_COMPETITION,
            sport=NCAAB_SPORT,
        ),
        _event(
            VenueName.MATCHBOOK,
            "dense-mb",
            home="Unresolved Athletic",
            away="Unresolved Wanderers",
            competition="",
            sport="football",
        ),
    ]
    partition = partition_identity_shards(items, kickoff_tolerance=timedelta(minutes=5))
    shard_ids = {shard.shard_id for shard in partition.shards}
    assert "football/uefa_nations_league" in shard_ids
    assert "basketball/nba" in shard_ids
    assert "basketball/ncaab" in shard_ids
    assert f"football/{UNRESOLVED_COMPETITION}" in shard_ids
    by_id = {shard.shard_id: shard for shard in partition.shards}
    assert {event.source_event_id for event in by_id["football/uefa_nations_league"].events} == {
        "nl-mb"
    }
    assert {event.source_event_id for event in by_id["basketball/nba"].events} == {"nba-k"}
    assert {event.source_event_id for event in by_id["basketball/ncaab"].events} == {"ncaab-pm"}
    assert DEFAULT_EVENT_MATCH_THRESHOLD == 0.92
    assert PAPER_EVENT_MATCH_THRESHOLD == 0.80
    assert DEFAULT_ASSIGNMENT_MARGIN == 0.03
    assert DEFAULT_PROVIDER_CONCURRENCY == {
        VenueName.MATCHBOOK: 4,
        VenueName.POLYMARKET: 8,
        VenueName.KALSHI: 4,
    }


def test_matchbook_fee_is_venue_wide_while_ncaab_stays_fail_closed() -> None:
    assert MATCHBOOK_STANDARD_FOOTBALL_COMMISSION == MATCHBOOK_PROVIDER_DEFAULT_COMMISSION
    assert {rule.market_class for rule in phase1_seed_rules()} == {MATCHBOOK_VENUE_COMMISSION_CLASS}
    resolver = VenueCostResolver()
    families = (
        MarketFamily.MATCH_RESULT,
        MarketFamily.GAME_WINNER,
        MarketFamily.POINT_SPREAD,
        MarketFamily.TOTAL_POINTS,
        "future_sport_match_winner",
    )
    snapshots = [
        resolver.resolve(
            venue=VenueName.MATCHBOOK,
            market_class=family,
            action=MarketAction.BACK,
            as_of=AS_OF,
        )
        for family in families
    ]
    expected_classes = {
        family.value if isinstance(family, MarketFamily) else family for family in families
    }
    assert {snapshot.rate for snapshot in snapshots} == {MATCHBOOK_PROVIDER_DEFAULT_COMMISSION}
    assert {snapshot.market_class for snapshot in snapshots} == expected_classes

    class _AccountOverride:
        def get_override(self) -> tuple[Decimal, datetime]:
            return Decimal("0.015"), AS_OF

    overridden = VenueCostResolver(matchbook_fee_store=_AccountOverride())  # type: ignore[arg-type]
    soccer = overridden.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.MATCH_RESULT,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    ncaab_family = overridden.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.GAME_WINNER,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    assert soccer.rate == ncaab_family.rate == Decimal("0.015")
    assert soccer.source == ncaab_family.source == MATCHBOOK_OVERRIDE_SOURCE

    with pytest.raises(UnknownRequiredCostError):
        resolver.resolve(
            venue=VenueName.KALSHI,
            market_class=MarketFamily.GAME_WINNER,
            action=MarketAction.BACK,
            as_of=AS_OF,
        )
    with pytest.raises(UnknownRequiredCostError):
        resolver.resolve(
            venue=VenueName.MATCHBOOK,
            market_class="unknown",
            action=MarketAction.BACK,
            as_of=AS_OF,
        )

    assert NCAAB_PAPER_APPROVED_CELLS == frozenset()
    trade = SimpleNamespace(competition=NCAAB_COMPETITION, sport=NCAAB_SPORT, legs=())
    assert ncaab_automatic_settlement_blocker(trade) == NCAAB_PAIR_UNAPPROVED_REASON


def test_console_sources_consume_integrated_read_model_fields() -> None:
    paper_api = (REPO_ROOT / "backend/src/sports_hedge/api/paper.py").read_text(encoding="utf-8")
    price_engine = (
        REPO_ROOT / "backend/src/sports_hedge/application/price_engine.py"
    ).read_text(encoding="utf-8")
    assert "annotate_active_trade_economics" in paper_api
    assert "MATCHBOOK_VENUE_COMMISSION_CLASS" in paper_api
    assert "pricing_fixtures=" in price_engine
    assert 'key.startswith("NCAAB_")' in price_engine

    book = (REPO_ROOT / "frontend/components/paper-trade-book.tsx").read_text(encoding="utf-8")
    exit_display = (REPO_ROOT / "frontend/lib/active-trade-exit-display.ts").read_text(
        encoding="utf-8"
    )
    hot = (REPO_ROOT / "frontend/lib/hot-fixture-roster-display.ts").read_text(encoding="utf-8")
    scan = (REPO_ROOT / "frontend/components/run-paper-scan.tsx").read_text(encoding="utf-8")
    assert 'from "../lib/active-trade-exit-display"' in book
    assert "formatEntryArb(trade)" in book
    assert "formatCurrentExit(trade)" in book
    assert "trade.entry_net_edge" in exit_display
    assert "trade.current_exit_pct" in exit_display
    assert 'return "—"' in exit_display
    assert "pricing_fixtures" in hot
    assert 'HOT_PRICING_HEADING = "HOT PRICING"' in hot
    assert "DEFERRED / AWAITING CROSS-VENUE" in hot
    assert "PAPER MODE · NO EXECUTION" in scan

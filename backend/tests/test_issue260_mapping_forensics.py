"""Issue #260 forensic follow-up: live-shaped 1X2 mapping without loosening settlement.

Deterministic fixtures shaped like owner-live Matchbook/Kalshi/Polymarket payloads.
Not owner-live evidence.

HOT vs UNIVERSE: both lanes share `_load_kalshi_markets`. Historical/demo Kalshi
1X2s complete because nested `rules_primary` carries explicit regulation wording
(K1 / demo fixtures). GAME/Opta/title never complete settlement. Pre-strict
matching of incomplete Kalshi GAME vs Matchbook regulation is not restored.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from test_issue260_mapping_census import _collect as _baseline_census_collect
from venue_cost_helpers import matchbook_polymarket_costs

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.hot_market_relationships import relationships_from_fixture_markets
from sports_hedge.application.lane_venues import participation_from_lists
from sports_hedge.application.mapping_census import (
    CENSUS_DATA_CLASS_FIXTURE,
    census_from_report,
    render_census,
)
from sports_hedge.application.mapping_forensics import (
    VENUE_SCOPE_ALL,
    VENUE_SCOPE_UNIVERSE,
    forensics_as_public_dict,
    forensics_from_report,
    parse_fixture_filter,
    render_forensics,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.application.universe_mapping_census import (
    OWNER_LIVE_CENSUS_ALL_VENUES_ENV,
    resolve_census_venue_scope,
)
from sports_hedge.config import Settings
from sports_hedge.domain.football import FootballPeriod, MarketFamily, SettlementScope
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.kalshi_contract_terms import (
    SOCCERANYGOAL_SHA256,
    SOCCEREXACTSCORE_SHA256,
    SOCCERGAMEWIN_SHA256,
    classify_kalshi_contract_terms_text,
    kalshi_apply_match_result_family_default,
    kalshi_contract_family_match_result_default,
    kalshi_contract_terms_url_is_allowlisted,
    lookup_kalshi_contract_family,
)
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    PolymarketNormalizer,
    _kalshi_settlement,
    classify_kalshi_contract_rule_layer,
    classify_kalshi_rule_field,
    classify_kalshi_rule_structure,
    classify_settlement_wording,
    kalshi_documented_result_scope_fingerprint,
    kalshi_documented_selector_presence,
    kalshi_documented_structured_fields,
    kalshi_secondary_is_scope_catalog,
    merge_kalshi_contract_rules,
    resolve_kalshi_rule_field_precedence,
    resolve_kalshi_settlement_from_rule_fields,
)
from sports_hedge.paper.models import FxRateSnapshot

KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
REGULATION = (
    "Resolves based on 90 minutes of regulation time. Extra time and penalties do not count."
)
# Abstracted live-shaped pattern. Not owner-live contract text.
SCOPE_CATALOG_SECONDARY = (
    "Series contract terms define result scope values that may be one of the following: "
    "regulation time, extra time, or full match including extra time and penalties. "
    "Example: a later penalty winner is not the regulation-time result. "
    "The market-selected scope is stated in the primary rules."
)
CONTRADICTORY_SECONDARY = (
    "This market resolves including extra time and penalties."
)
NINETY_MIN_ABBREV_PRIMARY = "Settles on 90 mins of play."
# Abstracted live-shaped SOCCERGAME template. Not owner-live contract text.
GENERIC_SOCCERGAME_TEMPLATE = (
    "Series contract terms define result scope that may take one of the following: "
    "first half, regulation time, second half, extra time, or full match. "
    "Payout criterion examples mention regulation time, extra time, and penalties. "
    "The Exchange lists iterations corresponding to each result scope."
)
STRUCTURED_TARGET_ID = "2ef4d31c-0b46-4f43-a403-f44d62489034"
BETIS = "Real Betis"
GETAFE = "Getafe"
KALSHI_GAME_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
    "contract_terms_url": "https://assets.kalshi.com/contract_terms/SOCCERGAMEWIN.pdf",
}


def _fx() -> list[FxRateSnapshot]:
    return [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test")]


def _costs() -> list:
    captured = datetime.now(UTC)
    return [
        *matchbook_polymarket_costs("0.02", "0.02", captured_at=captured),
        kalshi_cost_from_series(KALSHI_GAME_SERIES, captured_at=captured),
    ]


def _mb_match_odds() -> dict[str, Any]:
    return {
        "id": 9601,
        "name": "Match Odds",
        "runners": [
            {
                "id": 1,
                "name": BETIS,
                "prices": [{"side": "back", "odds": "2.10", "available-amount": "80"}],
            },
            {
                "id": 2,
                "name": "Draw",
                "prices": [{"side": "back", "odds": "3.40", "available-amount": "80"}],
            },
            {
                "id": 3,
                "name": GETAFE,
                "prices": [{"side": "back", "odds": "3.60", "available-amount": "80"}],
            },
        ],
    }


def _mb_to_qualify() -> dict[str, Any]:
    return {
        "id": 9607,
        "name": "To Qualify",
        "runners": [
            {
                "id": 51,
                "name": BETIS,
                "prices": [{"side": "back", "odds": "1.70", "available-amount": "80"}],
            },
            {
                "id": 52,
                "name": GETAFE,
                "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}],
            },
        ],
    }


class BetisMatchbook:
    def __init__(self, extra_markets: list[dict[str, Any]] | None = None) -> None:
        self.extra_markets = list(extra_markets or [])
        self.list_events_calls = 0

    def event_payload(self) -> dict[str, Any]:
        return {
            "id": 9600,
            "name": f"{BETIS} vs {GETAFE}",
            "start": KICKOFF.isoformat(),
            "competition-name": "Premier League",
        }

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {"events": [self.event_payload()]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": [_mb_match_odds(), *self.extra_markets]}

    async def get_market(
        self,
        event_id: int | str,
        market_id: int | str,
        **filters: Any,
    ) -> dict[str, Any]:
        del filters
        for market in [_mb_match_odds(), *self.extra_markets]:
            if str(market.get("id")) == str(market_id):
                return market
        from sports_hedge.venues.matchbook import MatchbookMarketGoneError

        raise MatchbookMarketGoneError(event_id, market_id, 404)


class EmptyPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"asset_id": "x", "bids": [], "asks": []}


AMBIGUOUS = "Winner of the match."


def _betis_kalshi_tickers() -> list[str]:
    return [str(item["ticker"]) for item in BetisKalshi()._markets()]


class BetisKalshi:
    def __init__(
        self,
        *,
        rules_on_event: bool = False,
        rules_on_markets: bool = False,
        event_rules_text: str | None = None,
        market_rules_text: str | None = None,
        market_secondary_text: str | None = None,
        contract_terms_url: str | None = None,
        contract_terms_sha256: str | None = None,
    ) -> None:
        self.event_rules_text = (
            REGULATION if rules_on_event and event_rules_text is None else event_rules_text
        )
        self.market_rules_text = (
            REGULATION if rules_on_markets and market_rules_text is None else market_rules_text
        )
        self.market_secondary_text = market_secondary_text
        self.contract_terms_url = (
            contract_terms_url or KALSHI_GAME_SERIES["contract_terms_url"]
        )
        self.contract_terms_sha256 = contract_terms_sha256 or SOCCERGAMEWIN_SHA256
        self.list_events_calls = 0
        self.contract_terms_calls: list[str] = []

    def _markets(self) -> list[dict[str, Any]]:
        ticker = "KXEPLGAME-26SEP20BETGET"
        markets = []
        for suffix, subtitle in (("BET", BETIS), ("DRAW", "Draw"), ("GET", GETAFE)):
            item = {
                "ticker": f"{ticker}-{suffix}",
                "event_ticker": ticker,
                "title": f"{BETIS} vs {GETAFE}",
                "yes_sub_title": subtitle,
            }
            if self.market_rules_text:
                item["rules_primary"] = self.market_rules_text
            if self.market_secondary_text:
                item["rules_secondary"] = self.market_secondary_text
            markets.append(item)
        return markets

    def event_payload(self) -> dict[str, Any]:
        event = {
            "event_ticker": "KXEPLGAME-26SEP20BETGET",
            "series_ticker": "KXEPLGAME",
            "title": f"{BETIS} vs {GETAFE}",
            "category": "Sports",
            "strike_date": KICKOFF.isoformat(),
            "product_metadata": {"competition": "EPL", "competition_scope": "Game"},
            "markets": self._markets(),
        }
        if self.event_rules_text:
            event["rules_primary"] = self.event_rules_text
        return event

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {"events": [self.event_payload()]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        del series_ticker
        return {**KALSHI_GAME_SERIES, "contract_terms_url": self.contract_terms_url}

    async def get_contract_terms_document(self, url: str) -> dict[str, Any]:
        self.contract_terms_calls.append(str(url))
        return {
            "url": str(url),
            "sha256": self.contract_terms_sha256,
            "byte_length": 60221,
        }

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.33", "100.00"]],
                "no_dollars": [["0.64", "200.00"]],
            }
        }


class BetisPolymarketBinaries:
    def __init__(self, *, with_regulation: bool = False, include_draw: bool = True) -> None:
        self.with_regulation = with_regulation
        self.include_draw = include_draw

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            {
                "id": "pm-bet-get",
                "title": f"{BETIS} vs {GETAFE}",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
            }
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        description = REGULATION if self.with_regulation else ""
        markets = [
            {
                "id": "pm-betis-win",
                "question": f"Will {BETIS} win?",
                "sportsMarketType": "moneyline",
                "groupItemTitle": BETIS,
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["by", "bn"]',
                "description": description,
                "feesEnabled": False,
            },
            {
                "id": "pm-getafe-win",
                "question": f"Will {GETAFE} win?",
                "sportsMarketType": "moneyline",
                "groupItemTitle": GETAFE,
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["gy", "gn"]',
                "description": description,
                "feesEnabled": False,
            },
        ]
        if self.include_draw:
            markets.insert(
                1,
                {
                    "id": "pm-draw",
                    "question": "Will the match end in a draw?",
                    "sportsMarketType": "moneyline",
                    "groupItemTitle": "Draw",
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["dy", "dn"]',
                    "description": description,
                    "feesEnabled": False,
                },
            )
        return markets

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        now_ms = int(datetime.now(UTC).timestamp() * 1000) - 150
        return {
            "asset_id": str(outcome_id),
            "timestamp": now_ms,
            "bids": [{"price": "0.30", "size": "200"}],
            "asks": [{"price": "0.32", "size": "200"}],
        }


async def _scan(
    matchbook,
    polymarket,
    kalshi=None,
    *,
    scan_lane: str = ScanLane.UNIVERSE.value,
    identity_scope: list[str] | None = None,
    known_source_events: dict[str, list[dict[str, Any]]] | None = None,
    hot_market_relationships=None,
):
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            scan_lane=scan_lane,
            identity_scope=identity_scope,
            known_source_events=known_source_events,
            hot_market_relationships=hot_market_relationships,
        )
        census = census_from_report(report, data_class=CENSUS_DATA_CLASS_FIXTURE)
        forensics = forensics_from_report(
            report,
            data_class=CENSUS_DATA_CLASS_FIXTURE,
            venue_scope=VENUE_SCOPE_UNIVERSE,
            enabled_venues=[item.value for item in report.enabled_venues],
            detail=True,
            fixture_filter=parse_fixture_filter(f"{BETIS} / {GETAFE}"),
        )
        return report, census, forensics
    finally:
        repository.close()


def _known_source_events(
    canonical_id: str,
    *,
    matchbook_event: dict[str, Any],
    kalshi_event: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    return {
        canonical_id: [
            {
                "venue": VenueName.MATCHBOOK.value,
                "source_event_id": str(matchbook_event.get("id") or ""),
                "raw": deepcopy(matchbook_event),
            },
            {
                "venue": VenueName.KALSHI.value,
                "source_event_id": str(kalshi_event.get("event_ticker") or ""),
                "raw": deepcopy(kalshi_event),
            },
        ]
    }


def _cross_venue_fixture(report):
    return next(
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    )


def _safe_identity(report) -> dict[str, Any]:
    fixture = _cross_venue_fixture(report)
    return {
        "canonical_event_id": fixture.canonical_event_id,
        "home_team": fixture.home_team,
        "away_team": fixture.away_team,
        "competition": fixture.competition,
        "kickoff_utc": fixture.kickoff_utc.isoformat(),
    }


def _safe_lane_snapshot(report, census, forensics) -> dict[str, Any]:
    mbk = forensics.matchbook_kalshi_match_result
    candidates = []
    for item in forensics.candidates:
        if item.family != MarketFamily.MATCH_RESULT.value:
            continue
        venues = [
            {
                "venue": venue.venue,
                "family": venue.family,
                "period": venue.period,
                "outcome_space": list(venue.outcome_space),
                "settlement_scope": venue.settlement_scope,
                "settlement_key": venue.settlement_key,
                "settlement_complete": venue.settlement_complete,
            }
            for venue in sorted(item.venues, key=lambda row: row.venue)
        ]
        candidates.append(
            {
                "family": item.family,
                "period": item.period,
                "line": item.line,
                "comparison_status": item.comparison_status,
                "matcher_reasons": sorted(item.matcher_reasons),
                "venues": venues,
            }
        )
    candidates.sort(
        key=lambda item: (
            item["comparison_status"] or "",
            item["period"] or "",
            item["line"] or "",
        )
    )
    return {
        "identity": _safe_identity(report),
        "equivalent_market_pairs": census.equivalent_market_pairs,
        "matched_equivalent": mbk.matched_equivalent,
        "both_complete_3way": mbk.both_complete_3way,
        "both_settlement_complete": mbk.both_settlement_complete,
        "rejection": dict(forensics.candidate_rejection_histogram),
        "candidates": candidates,
        "kalshi_match_result_rule_enrichment": dict(census.kalshi_match_result_rule_enrichment),
    }


async def _hot_from_same_payload(universe_report, *, kalshi):
    matchbook = BetisMatchbook()
    fixture = _cross_venue_fixture(universe_report)
    known = _known_source_events(
        fixture.canonical_event_id,
        matchbook_event=matchbook.event_payload(),
        kalshi_event=kalshi.event_payload(),
    )
    report, census, forensics = await _scan(
        matchbook,
        EmptyPolymarket(),
        kalshi,
        scan_lane=ScanLane.HOT.value,
        identity_scope=[fixture.canonical_event_id],
        known_source_events=known,
        hot_market_relationships=relationships_from_fixture_markets(
            universe_report.fixture_markets
        ),
    )
    assert matchbook.list_events_calls == 0
    assert kalshi.list_events_calls == 0
    return report, census, forensics


@pytest.mark.asyncio
async def test_baseline_census_forensics_has_complete_3way_match_result() -> None:
    report, _census = await _baseline_census_collect()
    forensic = forensics_from_report(
        report,
        data_class=CENSUS_DATA_CLASS_FIXTURE,
        venue_scope=VENUE_SCOPE_UNIVERSE,
        enabled_venues=["matchbook", "polymarket", "kalshi"],
        detail=True,
        match_result_sample=8,
    )
    rendered = render_forensics(forensic)
    assert "decimal_odds" not in rendered
    assert "available-amount" not in rendered
    assert "size_at_touch" not in rendered
    assert forensic.match_result_by_venue["matchbook"].complete_3way_home_draw_away >= 1
    assert forensic.candidate_rejection_histogram.get("matched") or any(
        item.comparison_status == "matched_equivalent" for item in forensic.candidates
    )


def test_kalshi_event_level_rules_complete_settlement_without_name_inference() -> None:
    normalizer = KalshiNormalizer()
    event_payload = {
        "event_ticker": "KXEPLGAME-26SEP20BETGET",
        "title": f"{BETIS} vs {GETAFE}",
        "strike_date": KICKOFF.isoformat(),
        "rules_primary": REGULATION,
    }
    event = normalizer.normalize_event(event_payload, series=KALSHI_GAME_SERIES)
    markets = [
        {
            "ticker": "KXEPLGAME-BET",
            "title": f"{BETIS} vs {GETAFE}",
            "yes_sub_title": BETIS,
        },
        {
            "ticker": "KXEPLGAME-DRAW",
            "title": f"{BETIS} vs {GETAFE}",
            "yes_sub_title": "Draw",
        },
        {
            "ticker": "KXEPLGAME-GET",
            "title": f"{BETIS} vs {GETAFE}",
            "yes_sub_title": GETAFE,
        },
    ]
    without_event_rules = normalizer.assemble_canonical_markets(
        event, markets, series=KALSHI_GAME_SERIES
    )
    with_event_rules = normalizer.assemble_canonical_markets(
        event, markets, series=KALSHI_GAME_SERIES, event_payload=event_payload
    )
    assert len(without_event_rules) == 1
    assert without_event_rules[0].family is MarketFamily.MATCH_RESULT
    assert without_event_rules[0].settlement.scope is SettlementScope.UNKNOWN
    assert without_event_rules[0].settlement.is_economically_complete() is False
    assert with_event_rules[0].settlement.scope is SettlementScope.REGULATION_TIME
    assert with_event_rules[0].settlement.is_economically_complete() is True
    mb = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(
            {
                "id": 9600,
                "name": f"{BETIS} vs {GETAFE}",
                "start": KICKOFF.isoformat(),
                "competition-name": "Premier League",
            }
        ),
        _mb_match_odds(),
    )
    matcher = MarketMatcher()
    unnamed = matcher.match(mb, without_event_rules[0])
    assert unnamed.matched is True
    assert "settlement_unknown_not_contradictory" in unnamed.reasons
    assert matcher.match(mb, with_event_rules[0]).matched is True
    named_only_event = {
        "event_ticker": "KXEPLGAME-26SEP20BETGET",
        "title": f"{BETIS} vs {GETAFE} Game",
        "strike_date": KICKOFF.isoformat(),
        "product_metadata": {"competition": "EPL", "competition_scope": "Game"},
    }
    named_only = normalizer.assemble_canonical_markets(
        event, markets, series=KALSHI_GAME_SERIES, event_payload=named_only_event
    )
    assert named_only[0].settlement.is_economically_complete() is False
    named = matcher.match(mb, named_only[0])
    assert named.matched is True
    assert "settlement_unknown_not_contradictory" in named.reasons


@pytest.mark.asyncio
async def test_live_shaped_kalshi_without_rules_maps_ordinary_1x2() -> None:
    report, census, forensics = await _scan(
        BetisMatchbook(),
        EmptyPolymarket(),
        BetisKalshi(rules_on_event=False, rules_on_markets=False),
    )
    assert census.equivalent_market_pairs == 1
    mb = forensics.match_result_by_venue["matchbook"]
    kalshi = forensics.match_result_by_venue["kalshi"]
    assert mb.complete_3way_home_draw_away == 1
    assert kalshi.complete_3way_home_draw_away == 1
    assert report.matched_event_pairs >= 1
    mbk = forensics.matchbook_kalshi_match_result
    assert mbk.cross_venue_fixtures_with_both_match_result == 1
    assert mbk.both_complete_3way == 1
    assert mbk.both_settlement_complete == 0
    assert mbk.matched_equivalent == 0
    assert any(
        "settlement_unknown_not_contradictory" in item.matcher_reasons
        for item in forensics.candidates
    )


@pytest.mark.asyncio
async def test_live_shaped_kalshi_event_rules_maps_ordinary_1x2() -> None:
    _report, census, forensics = await _scan(
        BetisMatchbook(),
        EmptyPolymarket(),
        BetisKalshi(rules_on_event=True, rules_on_markets=False),
    )
    assert census.equivalent_market_pairs == 1
    assert census.market_family_breakdown.get("match_result") == 1
    assert any(item.comparison_status == "matched_equivalent" for item in forensics.candidates)
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 1
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 1


class BetisKalshiGetMarket(BetisKalshi):
    def __init__(
        self,
        *,
        rules_text: str = REGULATION,
        secondary_text: str = "",
        event_rules_text: str | None = None,
        market_rules_text: str | None = None,
        market_secondary_text: str | None = None,
        fail_tickers: tuple[str, ...] = (),
        rules_on_event: bool = False,
        rules_on_markets: bool = False,
        structured_fields: dict[str, Any] | None = None,
        contract_terms_url: str | None = None,
        contract_terms_sha256: str | None = None,
    ) -> None:
        super().__init__(
            rules_on_event=rules_on_event,
            rules_on_markets=rules_on_markets,
            event_rules_text=event_rules_text,
            market_rules_text=market_rules_text,
            market_secondary_text=market_secondary_text,
            contract_terms_url=contract_terms_url,
            contract_terms_sha256=contract_terms_sha256,
        )
        self.rules_text = rules_text
        self.secondary_text = secondary_text
        self.fail_tickers = {str(item) for item in fail_tickers}
        self.structured_fields = dict(structured_fields or {})
        self.get_market_calls: list[str] = []

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.get_market_calls.append(str(ticker))
        if str(ticker) in self.fail_tickers:
            raise RuntimeError(f"Kalshi get_market failed for {ticker}")
        nested = next((item for item in self._markets() if item.get("ticker") == ticker), {})
        return {
            **nested,
            "ticker": ticker,
            "rules_primary": self.rules_text,
            "rules_secondary": self.secondary_text,
            **self.structured_fields,
        }


@pytest.mark.asyncio
async def test_get_market_rules_map_ordinary_1x2_without_name_inference() -> None:
    kalshi = BetisKalshiGetMarket(rules_text=REGULATION)
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), kalshi)
    assert census.equivalent_market_pairs == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 1
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 1
    assert sorted(kalshi.get_market_calls) == sorted(
        item["ticker"] for item in BetisKalshi()._markets()
    )
    assert census.kalshi_match_result_rule_enrichment["attempted"] == 3
    assert census.kalshi_match_result_rule_enrichment["applied"] == 3
    assert census.kalshi_match_result_rule_enrichment["skipped_complete"] == 0
    assert forensics.get_market_status_histogram.get("ok") == 3
    assert forensics.get_market_wording_kind_histogram.get("complete") == 3
    rendered = render_forensics(forensics) + render_census(census)
    assert REGULATION not in rendered
    assert "kalshi_match_result_rule_enrichment=" in rendered
    assert forensics.kalshi_rule_layers
    for item in forensics.kalshi_rule_layers:
        assert item.get_market_called is True
        assert item.get_market_status == "ok"
        get_layer = next(layer for layer in item.layers if layer.layer == "get_market")
        assert get_layer.rules_primary_nonempty is True
        assert get_layer.classified_scope == "regulation_time"
        assert get_layer.wording_kind == "complete"
        nested = next(layer for layer in item.layers if layer.layer == "nested_list")
        assert nested.any_rule_field_nonempty is False


@pytest.mark.asyncio
async def test_get_market_empty_or_ambiguous_rules_stay_incomplete() -> None:
    empty = BetisKalshiGetMarket(rules_text="")
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), empty)
    assert census.equivalent_market_pairs == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 0
    assert census.kalshi_match_result_rule_enrichment["rules_empty"] == 3
    assert census.kalshi_match_result_rule_enrichment["empty"] == 3
    assert census.kalshi_match_result_rule_enrichment["unchanged_existing"] == 0
    ambiguous = BetisKalshiGetMarket(rules_text=AMBIGUOUS)
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), ambiguous)
    assert census.equivalent_market_pairs == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0


def test_merge_prefers_get_market_rules_over_ambiguous_list_text() -> None:
    target = {"rules_primary": AMBIGUOUS, "title": f"{BETIS} vs {GETAFE}"}
    assert merge_kalshi_contract_rules(target, {"rules_primary": REGULATION, "title": "ignored"})
    assert target["rules_primary"] == REGULATION
    assert target["title"] == f"{BETIS} vs {GETAFE}"
    assert merge_kalshi_contract_rules(target, {"rules_primary": ""}) is False
    assert target["rules_primary"] == REGULATION


def test_safe_rule_layer_classification_does_not_expose_wording() -> None:
    complete = classify_kalshi_contract_rule_layer(
        {"rules_primary": REGULATION, "title": f"{BETIS} vs {GETAFE}"},
        layer="nested_list",
    )
    dumped = str(complete)
    assert REGULATION not in dumped
    assert BETIS not in dumped
    assert complete["wording_kind"] == "complete"
    assert complete["classified_scope"] == "regulation_time"
    assert complete["has_regulation_tokens"] is True
    ambiguous = classify_kalshi_contract_rule_layer(
        {"rules_primary": AMBIGUOUS},
        layer="nested_list",
    )
    assert ambiguous["wording_kind"] == "generic_ambiguous"
    assert ambiguous["classified_scope"] == "unknown"
    assert AMBIGUOUS not in str(ambiguous)
    gap = classify_kalshi_contract_rule_layer(
        {"rules_primary": "Settles on 90 mins of play."},
        layer="get_market",
        fetch_status="ok",
    )
    assert gap["wording_kind"] == "present_unclassified_with_settlement_tokens"
    assert gap["has_ninety_minute_abbrev"] is True
    assert gap["economically_complete"] is False
    assert "90 mins" not in str(gap)
    skipped = classify_kalshi_contract_rule_layer(
        None,
        layer="get_market",
        fetch_status="not_called_already_complete",
    )
    assert skipped["wording_kind"] == "not_called"
    failed = classify_kalshi_contract_rule_layer(
        None,
        layer="get_market",
        fetch_status="transport_failed",
    )
    assert failed["wording_kind"] == "absent"


def test_per_field_classification_separates_primary_from_catalog_secondary() -> None:
    payload = {
        "rules_primary": REGULATION,
        "rules_secondary": SCOPE_CATALOG_SECONDARY,
        "strike_type": "custom",
        "title": f"{BETIS} vs {GETAFE}",
    }
    primary = classify_kalshi_rule_field("rules_primary", REGULATION)
    secondary = classify_kalshi_rule_field("rules_secondary", SCOPE_CATALOG_SECONDARY)
    combined_scope, combined_et, combined_pen = classify_settlement_wording(
        f"{REGULATION} {SCOPE_CATALOG_SECONDARY}"
    )
    precedence = resolve_kalshi_rule_field_precedence(REGULATION, SCOPE_CATALOG_SECONDARY)
    layer = classify_kalshi_contract_rule_layer(payload, layer="get_market", fetch_status="ok")
    dumped = str(primary) + str(secondary) + str(layer)
    assert REGULATION not in dumped
    assert SCOPE_CATALOG_SECONDARY not in dumped
    assert BETIS not in dumped
    assert primary["economically_complete"] is True
    assert primary["classified_scope"] == "regulation_time"
    assert secondary["economically_complete"] is False or secondary["classified_scope"] != "regulation_time"
    assert combined_scope is SettlementScope.UNKNOWN
    assert combined_et is None and combined_pen is None
    assert precedence == (SettlementScope.REGULATION_TIME, False, False)
    assert layer["classified_scope"] == "unknown"
    assert layer["precedence_classified_scope"] == "regulation_time"
    assert layer["precedence_economically_complete"] is True
    assert layer["fingerprint_classified_scope"] == "unknown"
    assert layer["fingerprint_economically_complete"] is False
    assert layer["fingerprint_uses_rule_precedence"] is False
    by_field = {item["field"]: item for item in layer["fields"]}
    assert by_field["rules_primary"]["classified_scope"] == "regulation_time"
    assert by_field["rules_primary"]["economically_complete"] is True
    assert by_field["rules_secondary"]["present_nonempty"] is True
    selectors = kalshi_documented_selector_presence(payload)
    assert selectors["strike_type"] is True
    assert selectors["custom_strike"] is False
    assert "yes_sub_title" not in selectors
    assert "ticker" not in selectors


def test_unclassified_primary_is_not_guessed_from_catalog_secondary() -> None:
    precedence = resolve_kalshi_rule_field_precedence(AMBIGUOUS, SCOPE_CATALOG_SECONDARY)
    assert precedence[0] is SettlementScope.UNKNOWN
    abbrev = classify_kalshi_rule_field("rules_primary", NINETY_MIN_ABBREV_PRIMARY)
    assert abbrev["wording_kind"] == "present_unclassified_with_settlement_tokens"
    assert abbrev["economically_complete"] is False
    assert abbrev["has_ninety_minute_abbrev"] is True
    assert NINETY_MIN_ABBREV_PRIMARY not in str(abbrev)
    abbrev_prec = resolve_kalshi_rule_field_precedence(
        NINETY_MIN_ABBREV_PRIMARY, SCOPE_CATALOG_SECONDARY
    )
    assert abbrev_prec[0] is SettlementScope.UNKNOWN


def test_contradictory_complete_secondary_fails_closed() -> None:
    precedence = resolve_kalshi_rule_field_precedence(REGULATION, CONTRADICTORY_SECONDARY)
    assert precedence[0] is SettlementScope.UNKNOWN
    assert precedence[1] is None and precedence[2] is None
    listed_scopes = (
        "Regulation time is one defined scope. Another defined scope is full match "
        "including extra time and penalties."
    )
    assert kalshi_secondary_is_scope_catalog(listed_scopes) is True
    assert resolve_kalshi_rule_field_precedence(REGULATION, listed_scopes) == (
        SettlementScope.REGULATION_TIME,
        False,
        False,
    )
    assert kalshi_secondary_is_scope_catalog(CONTRADICTORY_SECONDARY) is False


def test_match_result_tickers_fetch_only_when_settlement_incomplete() -> None:
    normalizer = KalshiNormalizer()
    event_payload = {
        "event_ticker": "KXEPLGAME-26SEP20BETGET",
        "title": f"{BETIS} vs {GETAFE}",
        "strike_date": KICKOFF.isoformat(),
        "product_metadata": {"competition": "EPL", "competition_scope": "Game"},
    }
    event = normalizer.normalize_event(event_payload, series=KALSHI_GAME_SERIES)
    nested = BetisKalshi()._markets()
    missing = normalizer.match_result_tickers_missing_contract_rules(
        event, nested, series=KALSHI_GAME_SERIES, event_payload=event_payload
    )
    assert missing == _betis_kalshi_tickers()

    event_payload["rules_primary"] = AMBIGUOUS
    ambiguous_event = normalizer.match_result_tickers_missing_contract_rules(
        event, nested, series=KALSHI_GAME_SERIES, event_payload=event_payload
    )
    assert ambiguous_event == _betis_kalshi_tickers()

    event_payload["rules_primary"] = REGULATION
    complete_event = normalizer.match_result_tickers_missing_contract_rules(
        event, nested, series=KALSHI_GAME_SERIES, event_payload=event_payload
    )
    assert complete_event == []

    complete_nested = [{**item, "rules_primary": REGULATION} for item in nested]
    complete_market = normalizer.match_result_tickers_missing_contract_rules(
        event, complete_nested, series=KALSHI_GAME_SERIES, event_payload=event_payload
    )
    assert complete_market == []

    ambiguous_nested = [{**item, "rules_primary": AMBIGUOUS} for item in nested]
    incomplete_nested = normalizer.match_result_tickers_missing_contract_rules(
        event, ambiguous_nested, series=KALSHI_GAME_SERIES, event_payload=event_payload
    )
    assert incomplete_nested == _betis_kalshi_tickers()


@pytest.mark.asyncio
async def test_ambiguous_event_rules_plus_get_market_regulation_maps_1x2() -> None:
    kalshi = BetisKalshiGetMarket(rules_text=REGULATION, event_rules_text=AMBIGUOUS)
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), kalshi)
    assert census.equivalent_market_pairs == 1
    assert census.market_family_breakdown.get("match_result") == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 1
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 1
    assert sorted(kalshi.get_market_calls) == sorted(_betis_kalshi_tickers())


@pytest.mark.asyncio
async def test_ambiguous_nested_rules_plus_get_market_regulation_maps_1x2() -> None:
    kalshi = BetisKalshiGetMarket(rules_text=REGULATION, market_rules_text=AMBIGUOUS)
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), kalshi)
    assert census.equivalent_market_pairs == 1
    assert census.market_family_breakdown.get("match_result") == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 1
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 1
    assert sorted(kalshi.get_market_calls) == sorted(_betis_kalshi_tickers())


@pytest.mark.asyncio
async def test_complete_event_or_market_rules_skip_get_market() -> None:
    event_complete = BetisKalshiGetMarket(rules_text="should-not-be-fetched", rules_on_event=True)
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), event_complete)
    assert census.equivalent_market_pairs == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 1
    assert event_complete.get_market_calls == []
    assert census.kalshi_match_result_rule_enrichment["skipped_complete"] == 3
    assert census.kalshi_match_result_rule_enrichment["attempted"] == 0
    assert forensics.get_market_status_histogram.get("not_called_already_complete") == 3

    market_complete = BetisKalshiGetMarket(rules_text="should-not-be-fetched", rules_on_markets=True)
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), market_complete)
    assert census.equivalent_market_pairs == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 1
    assert market_complete.get_market_calls == []
    assert census.kalshi_match_result_rule_enrichment["skipped_complete"] == 3
    assert census.kalshi_match_result_rule_enrichment["attempted"] == 0


@pytest.mark.asyncio
async def test_ambiguous_current_and_get_market_rules_stay_incomplete() -> None:
    event_ambiguous = BetisKalshiGetMarket(rules_text=AMBIGUOUS, event_rules_text=AMBIGUOUS)
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), event_ambiguous)
    assert census.equivalent_market_pairs == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0
    assert sorted(event_ambiguous.get_market_calls) == sorted(_betis_kalshi_tickers())

    nested_ambiguous = BetisKalshiGetMarket(rules_text=AMBIGUOUS, market_rules_text=AMBIGUOUS)
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), nested_ambiguous)
    assert census.equivalent_market_pairs == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0
    assert sorted(nested_ambiguous.get_market_calls) == sorted(_betis_kalshi_tickers())
    assert census.kalshi_match_result_rule_enrichment["attempted"] == 3
    assert census.kalshi_match_result_rule_enrichment["unchanged_existing"] == 3
    assert census.kalshi_match_result_rule_enrichment["applied"] == 0
    assert census.kalshi_match_result_rule_enrichment["rules_empty"] == 0
    assert census.kalshi_match_result_rule_enrichment["empty"] == 0
    assert forensics.get_market_wording_kind_histogram.get("generic_ambiguous") == 3
    rendered = render_forensics(forensics)
    assert AMBIGUOUS not in rendered
    get_layer = next(
        layer
        for item in forensics.kalshi_rule_layers
        for layer in item.layers
        if layer.layer == "get_market"
    )
    assert get_layer.rules_primary_nonempty is True
    assert get_layer.classified_scope == "unknown"
    assert get_layer.wording_kind == "generic_ambiguous"
    assert get_layer.has_regulation_tokens is False


@pytest.mark.asyncio
async def test_get_market_failure_for_one_ticker_fails_closed() -> None:
    failed = _betis_kalshi_tickers()[0]
    kalshi = BetisKalshiGetMarket(rules_text=REGULATION, fail_tickers=(failed,))
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), kalshi)
    assert failed in kalshi.get_market_calls
    assert sorted(kalshi.get_market_calls) == sorted(_betis_kalshi_tickers())
    assert census.kalshi_match_result_rule_enrichment["failed"] >= 1
    assert census.kalshi_match_result_rule_enrichment["applied"] == 2
    assert forensics.get_market_status_histogram.get("transport_failed") == 1
    assert forensics.get_market_status_histogram.get("ok") == 2


@pytest.mark.asyncio
async def test_catalog_secondary_does_not_complete_from_retired_precedence() -> None:
    kalshi = BetisKalshi(
        rules_on_markets=True,
        market_rules_text=REGULATION,
        market_secondary_text=SCOPE_CATALOG_SECONDARY,
    )
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), kalshi)
    assert census.equivalent_market_pairs == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 0
    rendered = render_forensics(forensics)
    assert REGULATION not in rendered
    assert SCOPE_CATALOG_SECONDARY not in rendered
    assert "field=rules_primary" in rendered
    assert "field=rules_secondary" in rendered
    assert "fingerprint_uses_precedence=False" in rendered
    assert "combined_scope=unknown" in rendered
    nested = next(
        layer
        for item in forensics.kalshi_rule_layers
        for layer in item.layers
        if layer.layer == "nested_list"
    )
    assert nested.fingerprint_economically_complete is False
    assert nested.fingerprint_uses_rule_precedence is False
    secondary = next(field for field in nested.fields if field.field == "rules_secondary")
    assert secondary.looks_like_generic_contract_template is True


@pytest.mark.asyncio
async def test_hot_and_universe_agree_on_catalog_secondary_precedence() -> None:
    universe_kalshi = BetisKalshi(
        rules_on_markets=True,
        market_rules_text=REGULATION,
        market_secondary_text=SCOPE_CATALOG_SECONDARY,
    )
    universe_report, universe_census, universe_forensics = await _scan(
        BetisMatchbook(),
        EmptyPolymarket(),
        universe_kalshi,
    )
    hot_kalshi = BetisKalshi(
        rules_on_markets=True,
        market_rules_text=REGULATION,
        market_secondary_text=SCOPE_CATALOG_SECONDARY,
    )
    hot_report, hot_census, hot_forensics = await _hot_from_same_payload(
        universe_report, kalshi=hot_kalshi
    )
    assert _safe_lane_snapshot(universe_report, universe_census, universe_forensics)[
        "matched_equivalent"
    ] == _safe_lane_snapshot(hot_report, hot_census, hot_forensics)["matched_equivalent"]
    assert universe_census.equivalent_market_pairs == 1


@pytest.mark.asyncio
async def test_contradictory_secondary_stays_nonequivalent() -> None:
    kalshi = BetisKalshi(
        rules_on_markets=True,
        market_rules_text=REGULATION,
        market_secondary_text=CONTRADICTORY_SECONDARY,
    )
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), kalshi)
    assert census.equivalent_market_pairs == 0
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 0
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0


@pytest.mark.asyncio
async def test_unclassified_primary_plus_catalog_secondary_stays_incomplete() -> None:
    kalshi = BetisKalshi(
        rules_on_markets=True,
        market_rules_text=NINETY_MIN_ABBREV_PRIMARY,
        market_secondary_text=SCOPE_CATALOG_SECONDARY,
    )
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), kalshi)
    assert census.equivalent_market_pairs == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 0
    get_layer = next(
        layer
        for item in forensics.kalshi_rule_layers
        for layer in item.layers
        if layer.layer == "nested_list"
    )
    primary = next(field for field in get_layer.fields if field.field == "rules_primary")
    assert primary.wording_kind == "present_unclassified_with_settlement_tokens"
    assert primary.economically_complete is False
    assert get_layer.precedence_economically_complete is False
    rendered = render_forensics(forensics)
    assert NINETY_MIN_ABBREV_PRIMARY not in rendered
    assert "90 mins" not in rendered


@pytest.mark.asyncio
async def test_to_qualify_still_nonequivalent_with_catalog_secondary() -> None:
    report, census, _forensics = await _scan(
        BetisMatchbook(extra_markets=[_mb_to_qualify()]),
        EmptyPolymarket(),
        BetisKalshi(
            rules_on_markets=True,
            market_rules_text=REGULATION,
            market_secondary_text=SCOPE_CATALOG_SECONDARY,
        ),
    )
    rows = [row for items in report.fixture_markets.values() for row in items]
    qualify = [row for row in rows if row.family == "to_qualify"]
    assert qualify
    assert all(row.comparison_status.value != "matched_equivalent" for row in qualify)
    assert census.equivalent_market_pairs == 1
    assert census.market_family_breakdown.get("match_result") == 1


@pytest.mark.asyncio
async def test_identical_get_market_rules_count_as_unchanged_existing() -> None:
    kalshi = BetisKalshiGetMarket(
        rules_text=AMBIGUOUS,
        market_rules_text=AMBIGUOUS,
        rules_on_markets=True,
    )
    _report, census, _forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), kalshi)
    assert census.equivalent_market_pairs == 1
    assert census.kalshi_match_result_rule_enrichment["attempted"] == 3
    assert census.kalshi_match_result_rule_enrichment["unchanged_existing"] == 3
    assert census.kalshi_match_result_rule_enrichment["applied"] == 0
    assert census.kalshi_match_result_rule_enrichment["rules_empty"] == 0
    assert census.kalshi_match_result_rule_enrichment["empty"] == 0


@pytest.mark.asyncio
async def test_hot_and_universe_agree_on_live_shaped_incomplete_kalshi() -> None:
    """Same Betis/Getafe payload: HOT known-source-events vs UNIVERSE discovery.

    Live-shaped nested GAME omits contract rules. Both lanes match ordinary
    1X2 structurally. GAME/Opta/title still do not complete settlement.
    """

    universe_kalshi = BetisKalshi(rules_on_event=False, rules_on_markets=False)
    universe_report, universe_census, universe_forensics = await _scan(
        BetisMatchbook(),
        EmptyPolymarket(),
        universe_kalshi,
    )
    assert universe_kalshi.list_events_calls == 1
    hot_kalshi = BetisKalshi(rules_on_event=False, rules_on_markets=False)
    hot_report, hot_census, hot_forensics = await _hot_from_same_payload(
        universe_report, kalshi=hot_kalshi
    )
    universe_snap = _safe_lane_snapshot(universe_report, universe_census, universe_forensics)
    hot_snap = _safe_lane_snapshot(hot_report, hot_census, hot_forensics)
    assert universe_snap["identity"] == hot_snap["identity"]
    assert universe_snap["matched_equivalent"] == 0
    assert hot_report.discovered_fixtures
    target = next(
        item
        for item in hot_report.discovered_fixtures
        if item.canonical_event_id == universe_snap["identity"]["canonical_event_id"]
    )
    assert target.market_evaluation_state != "hot_relationship_missing"
    assert (target.matched_equivalent_count or 0) >= 1
    assert universe_snap["identity"]["home_team"] == BETIS
    assert universe_snap["identity"]["away_team"] == GETAFE
    assert universe_snap["matched_equivalent"] == 0
    assert universe_snap["both_complete_3way"] == 1
    assert universe_snap["both_settlement_complete"] == 0
    kalshi_venues = [
        venue
        for item in universe_snap["candidates"]
        for venue in item["venues"]
        if venue["venue"] == "kalshi"
    ]
    assert kalshi_venues
    assert all(venue["settlement_complete"] is False for venue in kalshi_venues)
    assert all(venue["settlement_scope"] == "unknown" for venue in kalshi_venues)
    assert all(
        set(venue["outcome_space"]) >= {"home", "draw", "away"} for venue in kalshi_venues
    )
    rendered = render_census(universe_census) + render_forensics(universe_forensics)
    assert "kalshi_match_result_rule_enrichment=" in rendered
    assert "attempted=0" in rendered
    assert "applied=0" in rendered
    assert "empty=" in rendered
    assert "failed=" in rendered
    assert universe_census.kalshi_match_result_rule_enrichment["attempted"] == 0
    assert universe_census.kalshi_match_result_rule_enrichment["applied"] == 0
    assert hot_census.kalshi_match_result_rule_enrichment["attempted"] == 0


@pytest.mark.asyncio
async def test_hot_and_universe_agree_on_historical_nested_rules_primary() -> None:
    """K1/demo historical path: nested rules_primary regulation wording.

    That field — not GAME/Opta/title — is what made Kalshi 1X2 settlement
    complete. Both lanes map the ordinary Match Result. Strict matcher is kept.
    """

    universe_kalshi = BetisKalshi(rules_on_markets=True)
    universe_report, universe_census, universe_forensics = await _scan(
        BetisMatchbook(),
        EmptyPolymarket(),
        universe_kalshi,
    )
    hot_kalshi = BetisKalshi(rules_on_markets=True)
    hot_report, hot_census, hot_forensics = await _hot_from_same_payload(
        universe_report, kalshi=hot_kalshi
    )
    universe_snap = _safe_lane_snapshot(universe_report, universe_census, universe_forensics)
    hot_snap = _safe_lane_snapshot(hot_report, hot_census, hot_forensics)
    assert universe_snap["identity"] == hot_snap["identity"]
    assert universe_snap["matched_equivalent"] == hot_snap["matched_equivalent"] == 1
    assert universe_snap["both_settlement_complete"] == 1
    assert universe_census.kalshi_match_result_rule_enrichment["skipped_complete"] == 3
    assert universe_census.kalshi_match_result_rule_enrichment["attempted"] == 0
    assert hot_census.kalshi_match_result_rule_enrichment["attempted"] == 0

    names_only = BetisKalshi()._markets()
    historical = [{**item, "rules_primary": REGULATION} for item in names_only]
    normalizer = KalshiNormalizer()
    event_payload = BetisKalshi().event_payload()
    event = normalizer.normalize_event(event_payload, series=KALSHI_GAME_SERIES)
    complete = normalizer.assemble_canonical_markets(
        event, historical, series=KALSHI_GAME_SERIES, event_payload=event_payload
    )
    incomplete = normalizer.assemble_canonical_markets(
        event, names_only, series=KALSHI_GAME_SERIES, event_payload=event_payload
    )
    assert complete[0].settlement.is_economically_complete() is True
    assert complete[0].settlement.scope is SettlementScope.REGULATION_TIME
    assert incomplete[0].settlement.is_economically_complete() is False
    mb = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(BetisMatchbook().event_payload()),
        _mb_match_odds(),
    )
    matcher = MarketMatcher()
    assert matcher.match(mb, complete[0]).matched is True
    incomplete_match = matcher.match(mb, incomplete[0])
    assert incomplete_match.matched is True
    assert "settlement_unknown_not_contradictory" in incomplete_match.reasons


@pytest.mark.asyncio
async def test_hot_and_universe_agree_on_get_market_regulation() -> None:
    universe_kalshi = BetisKalshiGetMarket(rules_text=REGULATION)
    universe_report, universe_census, universe_forensics = await _scan(
        BetisMatchbook(),
        EmptyPolymarket(),
        universe_kalshi,
    )
    assert sorted(universe_kalshi.get_market_calls) == sorted(_betis_kalshi_tickers())
    hot_kalshi = BetisKalshiGetMarket(rules_text=REGULATION)
    hot_report, hot_census, hot_forensics = await _hot_from_same_payload(
        universe_report, kalshi=hot_kalshi
    )
    universe_snap = _safe_lane_snapshot(universe_report, universe_census, universe_forensics)
    hot_snap = _safe_lane_snapshot(hot_report, hot_census, hot_forensics)
    assert universe_snap["identity"] == hot_snap["identity"]
    assert universe_snap["candidates"] == hot_snap["candidates"]
    assert universe_snap["matched_equivalent"] == hot_snap["matched_equivalent"] == 1
    assert universe_snap["both_settlement_complete"] == 1
    assert hot_kalshi.get_market_calls == []
    assert universe_census.kalshi_match_result_rule_enrichment["attempted"] == 3
    assert universe_census.kalshi_match_result_rule_enrichment["applied"] == 3
    assert hot_census.kalshi_match_result_rule_enrichment["attempted"] == 0
    assert hot_census.kalshi_match_result_rule_enrichment["applied"] == 0
    assert universe_census.kalshi_match_result_rule_enrichment["failed"] == 0
    assert universe_census.kalshi_match_result_rule_enrichment["empty"] == 0


@pytest.mark.asyncio
async def test_polymarket_binaries_without_draw_do_not_match_3way() -> None:
    _report, census, forensics = await _scan(
        BetisMatchbook(),
        BetisPolymarketBinaries(with_regulation=True, include_draw=False),
    )
    assert census.equivalent_market_pairs == 0
    pm = forensics.match_result_by_venue["polymarket"]
    assert pm.incomplete_or_binary_yes_no >= 1 or pm.incomplete_other >= 1


@pytest.mark.asyncio
async def test_polymarket_complete_binaries_with_regulation_map_1x2() -> None:
    _report, census, forensics = await _scan(
        BetisMatchbook(),
        BetisPolymarketBinaries(with_regulation=True, include_draw=True),
    )
    assert census.equivalent_market_pairs == 1
    assert census.market_family_breakdown.get("match_result") == 1
    assert forensics.match_result_by_venue["polymarket"].complete_3way_home_draw_away == 1


@pytest.mark.asyncio
async def test_to_qualify_remains_nonequivalent_to_regulation_1x2() -> None:
    report, census, _forensics = await _scan(
        BetisMatchbook(extra_markets=[_mb_to_qualify()]),
        BetisPolymarketBinaries(with_regulation=True, include_draw=True),
    )
    rows = [row for items in report.fixture_markets.values() for row in items]
    qualify = [row for row in rows if row.family == "to_qualify"]
    assert qualify
    assert all(row.comparison_status.value != "matched_equivalent" for row in qualify)
    assert census.market_family_breakdown.get("match_result") == 1


@pytest.mark.asyncio
async def test_matchbook_to_qualify_not_equivalent_to_kalshi_regulation_1x2() -> None:
    report, census, _forensics = await _scan(
        BetisMatchbook(extra_markets=[_mb_to_qualify()]),
        EmptyPolymarket(),
        BetisKalshi(rules_on_event=True, rules_on_markets=False),
    )
    rows = [row for items in report.fixture_markets.values() for row in items]
    qualify = [row for row in rows if row.family == "to_qualify"]
    assert qualify
    assert all(row.comparison_status.value != "matched_equivalent" for row in qualify)
    assert census.equivalent_market_pairs == 1
    assert census.market_family_breakdown.get("match_result") == 1


def test_fixture_filter_and_venue_scope_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    assert parse_fixture_filter("Real Betis / Getafe") == ["Real Betis", "Getafe"]
    participation = participation_from_lists(
        ["matchbook", "kalshi"],
        ["matchbook", "kalshi"],
        source="operator",
    )
    monkeypatch.setattr(
        "sports_hedge.application.universe_mapping_census.resolve_lane_venue_participation",
        lambda *args, **kwargs: participation,
    )
    monkeypatch.setattr(
        "sports_hedge.application.universe_mapping_census.get_lane_venue_settings_store",
        lambda: object(),
    )
    scope = resolve_census_venue_scope(settings=Settings(), environ={})
    assert scope.venue_scope == VENUE_SCOPE_UNIVERSE
    assert scope.participation_source == "operator"
    assert VenueName.POLYMARKET not in scope.enabled_venues
    assert VenueName.MATCHBOOK in scope.enabled_venues
    all_scope = resolve_census_venue_scope(
        settings=Settings(),
        environ={OWNER_LIVE_CENSUS_ALL_VENUES_ENV: "1"},
    )
    assert all_scope.venue_scope == VENUE_SCOPE_ALL
    assert VenueName.POLYMARKET in all_scope.enabled_venues
    assert all_scope.participation_source == "explicit_all_venues"


def test_polymarket_moneyline_yes_outcome_from_live_shaped_question() -> None:
    event = PolymarketNormalizer().normalize_event(
        {
            "id": "pm-bet-get",
            "title": f"{BETIS} vs {GETAFE}",
            "startTime": KICKOFF.isoformat(),
            "competition": "Premier League",
        }
    )
    from sports_hedge.normalization.venues import polymarket_moneyline_yes_outcome

    assert polymarket_moneyline_yes_outcome(
        f"Will {BETIS} win?", home_team=event.home_team, away_team=event.away_team
    ) is not None


def test_forensics_public_dict_strips_prices_and_keeps_safe_metadata() -> None:
    from sports_hedge.application.mapping_forensics import MappingForensics, MatchResultVenueCounts

    payload = forensics_as_public_dict(
        MappingForensics(
            data_class=CENSUS_DATA_CLASS_FIXTURE,
            venue_scope=VENUE_SCOPE_UNIVERSE,
            enabled_venues=["matchbook", "kalshi"],
            match_result_by_venue={
                "matchbook": MatchResultVenueCounts(
                    complete_3way_home_draw_away=1, total=1
                )
            },
            candidate_rejection_histogram={"incomplete_settlement": 1},
            notes=["PAPER MODE · EXECUTION DISABLED · mapping metadata only"],
        )
    )
    dumped = str(payload)
    assert "decimal_odds" not in dumped
    assert "available-amount" not in dumped
    assert payload["candidate_rejection_histogram"]["incomplete_settlement"] == 1
    assert payload["match_result_by_venue"]["matchbook"]["complete_3way_home_draw_away"] == 1


def _live_shaped_get_market_structured() -> dict[str, Any]:
    return {
        "strike_type": "structured",
        "market_type": "binary",
        "custom_strike": {"soccer_team": STRUCTURED_TARGET_ID},
        "functional_strike": None,
        "primary_participant_key": "soccer_team",
        "subtitle": "",
        "product_metadata": {"competition": "EPL", "competition_scope": "Game"},
        "milestone": {
            "type": "sports",
            "details": {"home_team_id": STRUCTURED_TARGET_ID, "away_team_id": STRUCTURED_TARGET_ID},
        },
    }


def test_generic_template_fields_are_structurally_unselected_scope() -> None:
    primary = classify_kalshi_rule_field("rules_primary", GENERIC_SOCCERGAME_TEMPLATE)
    secondary = classify_kalshi_rule_field("rules_secondary", GENERIC_SOCCERGAME_TEMPLATE)
    structure = classify_kalshi_rule_structure(GENERIC_SOCCERGAME_TEMPLATE)
    dumped = str(primary) + str(secondary) + str(structure)
    assert GENERIC_SOCCERGAME_TEMPLATE not in dumped
    assert primary["economically_complete"] is False
    assert primary["classified_scope"] == "unknown"
    assert primary["looks_like_generic_contract_template"] is True
    assert primary["contains_result_scope_placeholder"] is True
    assert primary["contains_multiple_scope_definitions"] is True
    assert primary["clause_token_pattern"] == "mixed_regulation_et_penalties"
    assert secondary["looks_like_generic_contract_template"] is True
    assert resolve_kalshi_settlement_from_rule_fields(
        GENERIC_SOCCERGAME_TEMPLATE, GENERIC_SOCCERGAME_TEMPLATE
    ) == (SettlementScope.UNKNOWN, None, None)
    fingerprint = _kalshi_settlement(
        {
            "rules_primary": GENERIC_SOCCERGAME_TEMPLATE,
            "rules_secondary": GENERIC_SOCCERGAME_TEMPLATE,
            "ticker": "KXEPLGAME-26SEP20BETGET-BET",
            **_live_shaped_get_market_structured(),
            "yes_sub_title": BETIS,
        },
        series=KALSHI_GAME_SERIES,
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
    )
    assert fingerprint.scope is SettlementScope.UNKNOWN
    assert fingerprint.is_economically_complete() is False


def test_get_market_structured_fields_are_safe_and_do_not_select_result_scope() -> None:
    payload = {
        "rules_primary": GENERIC_SOCCERGAME_TEMPLATE,
        "rules_secondary": GENERIC_SOCCERGAME_TEMPLATE,
        "yes_sub_title": BETIS,
        "title": f"{BETIS} vs {GETAFE}",
        **_live_shaped_get_market_structured(),
    }
    structured = kalshi_documented_structured_fields(payload)
    layer = classify_kalshi_contract_rule_layer(payload, layer="get_market", fetch_status="ok")
    dumped = str(structured) + str(layer)
    assert STRUCTURED_TARGET_ID not in dumped
    assert BETIS not in dumped
    assert GETAFE not in dumped
    assert GENERIC_SOCCERGAME_TEMPLATE not in dumped
    assert structured["strike_type"] == "structured"
    assert structured["market_type"] == "binary"
    assert structured["custom_strike_present"] is True
    assert structured["custom_strike_keys"] == ["soccer_team"]
    assert structured["custom_strike_value_types"] == ["uuid_string"]
    assert structured["custom_strike_entity_target"] is True
    assert structured["custom_strike_selects_result_scope"] is False
    assert structured["functional_strike_present"] is False
    assert structured["functional_strike_shape"] == "absent"
    assert structured["primary_participant_key_present"] is True
    assert structured["yes_sub_title_scope_selectors"]["contains_explicit_regulation_selector"] is False
    assert structured["yes_sub_title_scope_selectors"]["contains_explicit_extra_time_selector"] is False
    assert structured["yes_sub_title_scope_selectors"]["contains_explicit_full_match_selector"] is False
    assert structured["product_metadata_keys"] == ["competition", "competition_scope"]
    assert structured["product_metadata_value_classes"]["competition"] == "competition_code"
    assert (
        structured["product_metadata_value_classes"]["competition_scope"]
        == "series_or_product_family_not_result_scope"
    )
    assert "home_team_id" in structured["milestone_detail_keys"]
    assert structured["milestone_detail_value_types"]["home_team_id"] == "uuid_string"
    assert structured["documented_result_scope_selector"] == "none"
    assert kalshi_documented_result_scope_fingerprint(payload) is None
    presence = kalshi_documented_selector_presence(payload)
    assert presence["strike_type"] is True
    assert presence["custom_strike"] is True
    assert presence["market_type"] is True
    assert presence["functional_strike"] is False
    assert presence["primary_participant_key"] is True
    assert "yes_sub_title" not in presence
    assert layer["fingerprint_economically_complete"] is False
    assert layer["fingerprint_uses_rule_precedence"] is False
    by_field = {item["field"]: item for item in layer["fields"]}
    assert by_field["rules_primary"]["looks_like_generic_contract_template"] is True
    assert by_field["rules_secondary"]["contains_payout_criterion"] is True


def test_title_or_yes_sub_title_scope_words_do_not_complete_settlement() -> None:
    payload = {
        "rules_primary": GENERIC_SOCCERGAME_TEMPLATE,
        "yes_sub_title": "Betis regulation time",
        "subtitle": "full match including extra time",
        "strike_type": "structured",
        "custom_strike": {"soccer_team": STRUCTURED_TARGET_ID},
        "market_type": "binary",
    }
    flags = kalshi_documented_structured_fields(payload)
    assert flags["yes_sub_title_scope_selectors"]["contains_explicit_regulation_selector"] is True
    assert flags["subtitle_scope_selectors"]["contains_explicit_full_match_selector"] is True
    assert flags["subtitle_scope_selectors"]["contains_explicit_extra_time_selector"] is True
    assert kalshi_documented_result_scope_fingerprint(payload) is None
    fingerprint = _kalshi_settlement(
        payload,
        series=KALSHI_GAME_SERIES,
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
    )
    assert fingerprint.scope is SettlementScope.UNKNOWN
    dumped = str(flags)
    assert "Betis regulation time" not in dumped
    assert STRUCTURED_TARGET_ID not in dumped


def test_complete_primary_alone_still_maps_without_template_secondary() -> None:
    fingerprint = _kalshi_settlement(
        {"rules_primary": REGULATION, "ticker": "KXEPLGAME-BET"},
        series=KALSHI_GAME_SERIES,
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
    )
    assert fingerprint.scope is SettlementScope.REGULATION_TIME
    assert fingerprint.is_economically_complete() is True
    catalog_blocked = resolve_kalshi_settlement_from_rule_fields(
        REGULATION, SCOPE_CATALOG_SECONDARY
    )
    assert catalog_blocked == (SettlementScope.UNKNOWN, None, None)
    contradictory = resolve_kalshi_settlement_from_rule_fields(
        REGULATION, CONTRADICTORY_SECONDARY
    )
    assert contradictory == (SettlementScope.UNKNOWN, None, None)


def test_extra_time_and_unknown_structured_selectors_stay_fail_closed() -> None:
    extra_time = _kalshi_settlement(
        {
            "rules_primary": "Resolves including extra time without penalties.",
            "strike_type": "structured",
            "custom_strike": {"soccer_team": STRUCTURED_TARGET_ID},
            "market_type": "binary",
        },
        series=KALSHI_GAME_SERIES,
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
    )
    assert extra_time.scope is SettlementScope.INCLUDING_EXTRA_TIME
    unknown_selector = kalshi_documented_result_scope_fingerprint(
        {
            "strike_type": "custom",
            "market_type": "scalar",
            "custom_strike": {"result_scope": "regulation_time"},
        }
    )
    assert unknown_selector is None


@pytest.mark.asyncio
async def test_live_shaped_template_plus_structured_get_market_stays_incomplete() -> None:
    kalshi = BetisKalshiGetMarket(
        rules_text=GENERIC_SOCCERGAME_TEMPLATE,
        secondary_text=GENERIC_SOCCERGAME_TEMPLATE,
        structured_fields=_live_shaped_get_market_structured(),
    )
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), kalshi)
    assert census.equivalent_market_pairs == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 0
    assert sorted(kalshi.get_market_calls) == sorted(_betis_kalshi_tickers())
    rendered = render_forensics(forensics)
    assert GENERIC_SOCCERGAME_TEMPLATE not in rendered
    assert STRUCTURED_TARGET_ID not in rendered
    get_layer = next(
        layer
        for item in forensics.kalshi_rule_layers
        for layer in item.layers
        if layer.layer == "get_market"
    )
    assert get_layer.structured_fields["strike_type"] == "structured"
    assert get_layer.structured_fields["market_type"] == "binary"
    assert get_layer.structured_fields["custom_strike_entity_target"] is True
    assert get_layer.structured_fields["custom_strike_selects_result_scope"] is False
    assert get_layer.structured_fields["documented_result_scope_selector"] == "none"
    assert get_layer.fingerprint_economically_complete is False
    primary = next(field for field in get_layer.fields if field.field == "rules_primary")
    assert primary.looks_like_generic_contract_template is True
    assert primary.contains_result_scope_placeholder is True
    assert "generic_template=True" in rendered
    assert "structured_fields=" in rendered


@pytest.mark.asyncio
async def test_hot_and_universe_agree_on_structured_template_payload() -> None:
    structured = _live_shaped_get_market_structured()
    universe_kalshi = BetisKalshiGetMarket(
        rules_text=GENERIC_SOCCERGAME_TEMPLATE,
        secondary_text=GENERIC_SOCCERGAME_TEMPLATE,
        market_rules_text=GENERIC_SOCCERGAME_TEMPLATE,
        market_secondary_text=GENERIC_SOCCERGAME_TEMPLATE,
        rules_on_markets=True,
        structured_fields=structured,
    )
    universe_report, universe_census, universe_forensics = await _scan(
        BetisMatchbook(),
        EmptyPolymarket(),
        universe_kalshi,
    )
    hot_kalshi = BetisKalshiGetMarket(
        rules_text=GENERIC_SOCCERGAME_TEMPLATE,
        secondary_text=GENERIC_SOCCERGAME_TEMPLATE,
        market_rules_text=GENERIC_SOCCERGAME_TEMPLATE,
        market_secondary_text=GENERIC_SOCCERGAME_TEMPLATE,
        rules_on_markets=True,
        structured_fields=structured,
    )
    hot_report, hot_census, hot_forensics = await _hot_from_same_payload(
        universe_report, kalshi=hot_kalshi
    )
    assert _safe_lane_snapshot(universe_report, universe_census, universe_forensics)["matched_equivalent"] == (
        _safe_lane_snapshot(hot_report, hot_census, hot_forensics)["matched_equivalent"]
    )
    assert universe_census.equivalent_market_pairs == 1
    assert universe_forensics.matchbook_kalshi_match_result.both_settlement_complete == 0


GAMEWIN_TERMS_SHAPE = (
    "Official product name kind will team win. "
    "The result scope is specified by the Exchange. "
    "Result scope values first half, regulation time, second half, extra time, or full match. "
    "The Exchange may list iterations corresponding to each result scope."
)
EXACTSCORE_DEFAULT_SHAPE = (
    "The time period where not specified otherwise shall be understood to refer to "
    "regulation time only. Specified by the Exchange listed iterations may use extra time "
    "or full match."
)
ANYGOAL_DEFAULT_SHAPE = (
    "The time period where not specified otherwise shall be understood to refer to the "
    "sum of regulation time and extra time. Specified by the Exchange."
)
UNKNOWN_DEFAULT_SHAPE = (
    "The time period where not specified otherwise shall be understood to refer to the "
    "applicable listed iteration."
)
GAMEWIN_URL = "https://assets.kalshi.com/contract_terms/SOCCERGAMEWIN.pdf"
EXACTSCORE_URL = "https://assets.kalshi.com/contract_terms/SOCCEREXACTSCORE.pdf"
ANYGOAL_URL = "https://assets.kalshi.com/contract_terms/SOCCERANYGOAL.pdf"


def test_contract_terms_allowlist_rejects_arbitrary_hosts() -> None:
    assert kalshi_contract_terms_url_is_allowlisted(GAMEWIN_URL) is True
    assert kalshi_contract_terms_url_is_allowlisted(EXACTSCORE_URL) is True
    assert kalshi_contract_terms_url_is_allowlisted(
        "https://kalshi-public-docs.s3.amazonaws.com/contract_terms/SOCCERGAMEWIN.pdf"
    )
    assert kalshi_contract_terms_url_is_allowlisted(
        "https://assets.kalshi.com/regulatory/product-certifications/SOCCERGAMEWIN.pdf"
    ) is False
    assert kalshi_contract_terms_url_is_allowlisted(
        "https://evil.example/contract_terms/SOCCERGAMEWIN.pdf"
    ) is False
    assert kalshi_contract_terms_url_is_allowlisted(GAMEWIN_URL + "?x=1") is False
    assert kalshi_contract_terms_url_is_allowlisted(
        "http://assets.kalshi.com/contract_terms/SOCCERGAMEWIN.pdf"
    ) is False


def test_gamewin_terms_have_no_default_result_scope() -> None:
    classified = classify_kalshi_contract_terms_text(GAMEWIN_TERMS_SHAPE)
    assert classified["has_default_clause"] is False
    assert classified["default_result_scope"] is None
    assert classified["placeholder_specified_by_exchange"] is True
    assert classified["looks_like_soccergamewin"] is True
    family = lookup_kalshi_contract_family(url=GAMEWIN_URL, sha256=SOCCERGAMEWIN_SHA256)
    assert family["family_id"] == "soccergamewin"
    assert family["defines_default_result_scope"] is False
    assert family["placeholder_specified_by_exchange"] is True
    assert family["verified"] == "sha256"
    assert family["match_result_default_scope"] == "none"
    assert kalshi_contract_family_match_result_default(family) is None
    assert kalshi_apply_match_result_family_default(family) is None


def test_exactscore_default_is_regulation_but_not_match_result() -> None:
    classified = classify_kalshi_contract_terms_text(EXACTSCORE_DEFAULT_SHAPE)
    assert classified["has_default_clause"] is True
    assert classified["default_result_scope"] == "regulation_time"
    family = lookup_kalshi_contract_family(url=EXACTSCORE_URL, sha256=SOCCEREXACTSCORE_SHA256)
    assert family["family_id"] == "soccerexactscore"
    assert family["defines_default_result_scope"] is True
    assert family["default_result_scope"] == "regulation_time"
    assert family["default_applies_to_match_result"] is False
    assert family["match_result_default_scope"] == "none"
    assert kalshi_apply_match_result_family_default(family) is None


def test_anygoal_default_is_extra_time_sum_not_match_result() -> None:
    classified = classify_kalshi_contract_terms_text(ANYGOAL_DEFAULT_SHAPE)
    assert classified["has_default_clause"] is True
    assert classified["default_result_scope"] == "including_extra_time"
    family = lookup_kalshi_contract_family(url=ANYGOAL_URL, sha256=SOCCERANYGOAL_SHA256)
    assert family["family_id"] == "socceranygoal"
    assert family["default_applies_to_match_result"] is False
    assert kalshi_apply_match_result_family_default(family) is None


def test_unknown_default_clause_and_hash_mismatch_fail_closed() -> None:
    classified = classify_kalshi_contract_terms_text(UNKNOWN_DEFAULT_SHAPE)
    assert classified["has_default_clause"] is True
    assert classified["default_result_scope"] == "unknown"
    mismatched = lookup_kalshi_contract_family(
        url=GAMEWIN_URL,
        sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    )
    assert mismatched["family_id"] == "unknown"
    assert mismatched["verified"] == "hash_mismatch"
    assert kalshi_apply_match_result_family_default(mismatched) is None
    filename_only = lookup_kalshi_contract_family(url=GAMEWIN_URL)
    assert filename_only["family_id"] == "soccergamewin"
    assert filename_only["verified"] == "url_filename"
    assert kalshi_apply_match_result_family_default(filename_only) is None


def test_synthetic_match_result_default_requires_sha256_and_does_not_override_et() -> None:
    synthetic = {
        "defines_default_result_scope": True,
        "default_applies_to_match_result": True,
        "default_result_scope": "regulation_time",
        "verified": "sha256",
    }
    assert kalshi_apply_match_result_family_default(synthetic) == "regulation_time"
    template = _kalshi_settlement(
        {"rules_primary": GENERIC_SOCCERGAME_TEMPLATE, "ticker": "SYN-BET"},
        series={"contract_family": synthetic},
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
    )
    assert template.scope is SettlementScope.REGULATION_TIME
    assert template.is_economically_complete() is True
    extra_time = _kalshi_settlement(
        {
            "rules_primary": "Resolves including extra time without penalties.",
            "ticker": "SYN-ET",
        },
        series={"contract_family": synthetic},
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
    )
    assert extra_time.scope is SettlementScope.INCLUDING_EXTRA_TIME
    unverified = _kalshi_settlement(
        {"rules_primary": GENERIC_SOCCERGAME_TEMPLATE, "ticker": "SYN-UNVERIFIED"},
        series={
            "contract_family": {**synthetic, "verified": "url_filename"},
        },
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
    )
    assert unverified.scope is SettlementScope.UNKNOWN
    assert unverified.is_economically_complete() is False


def test_gamewin_family_does_not_complete_template_settlement() -> None:
    family = lookup_kalshi_contract_family(url=GAMEWIN_URL, sha256=SOCCERGAMEWIN_SHA256)
    fingerprint = _kalshi_settlement(
        {"rules_primary": GENERIC_SOCCERGAME_TEMPLATE, "ticker": "KXEPLGAME-BET"},
        series={**KALSHI_GAME_SERIES, "contract_family": family},
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
    )
    assert fingerprint.scope is SettlementScope.UNKNOWN
    assert fingerprint.is_economically_complete() is False
    exactscore = lookup_kalshi_contract_family(
        url=EXACTSCORE_URL, sha256=SOCCEREXACTSCORE_SHA256
    )
    leaked = _kalshi_settlement(
        {"rules_primary": GENERIC_SOCCERGAME_TEMPLATE, "ticker": "KXEPLGAME-BET"},
        series={"contract_terms_url": EXACTSCORE_URL, "contract_family": exactscore},
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        line=None,
    )
    assert leaked.scope is SettlementScope.UNKNOWN


@pytest.mark.asyncio
async def test_live_shaped_gamewin_contract_terms_stay_incomplete() -> None:
    kalshi = BetisKalshiGetMarket(
        rules_text=GENERIC_SOCCERGAME_TEMPLATE,
        secondary_text=GENERIC_SOCCERGAME_TEMPLATE,
        structured_fields=_live_shaped_get_market_structured(),
    )
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), kalshi)
    assert census.equivalent_market_pairs == 1
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 0
    assert kalshi.contract_terms_calls == [GAMEWIN_URL]
    rendered = render_forensics(forensics)
    assert GENERIC_SOCCERGAME_TEMPLATE not in rendered
    assert SOCCERGAMEWIN_SHA256 not in rendered
    series_layer = next(
        layer
        for item in forensics.kalshi_rule_layers
        for layer in item.layers
        if layer.layer == "series"
    )
    assert series_layer.contract_terms_url_present is True
    family = series_layer.contract_family
    assert family["family_id"] == "soccergamewin"
    assert family["defines_default_result_scope"] is False
    assert family["match_result_default_scope"] == "none"
    assert family["placeholder_specified_by_exchange"] is True
    assert family["verified"] == "sha256"
    assert family["fetch_status"] == "ok"
    assert family["filename"] == "SOCCERGAMEWIN.pdf"
    assert "SOCCERGAMEWIN.pdf" in rendered
    assert "defines_default_result_scope=False" in rendered


@pytest.mark.asyncio
async def test_exactscore_family_default_does_not_complete_1x2() -> None:
    kalshi = BetisKalshiGetMarket(
        rules_text=GENERIC_SOCCERGAME_TEMPLATE,
        secondary_text=GENERIC_SOCCERGAME_TEMPLATE,
        contract_terms_url=EXACTSCORE_URL,
        contract_terms_sha256=SOCCEREXACTSCORE_SHA256,
    )
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), kalshi)
    assert census.equivalent_market_pairs == 1
    assert census.ordinary_1x2_structural_admissions == 0
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 0
    series_layer = next(
        layer
        for item in forensics.kalshi_rule_layers
        for layer in item.layers
        if layer.layer == "series"
    )
    assert series_layer.contract_family["family_id"] == "soccerexactscore"
    assert series_layer.contract_family["default_result_scope"] == "regulation_time"
    assert series_layer.contract_family["match_result_default_scope"] == "none"


@pytest.mark.asyncio
async def test_hot_and_universe_agree_on_gamewin_contract_family() -> None:
    universe_kalshi = BetisKalshiGetMarket(
        rules_text=GENERIC_SOCCERGAME_TEMPLATE,
        secondary_text=GENERIC_SOCCERGAME_TEMPLATE,
        market_rules_text=GENERIC_SOCCERGAME_TEMPLATE,
        market_secondary_text=GENERIC_SOCCERGAME_TEMPLATE,
        rules_on_markets=True,
    )
    universe_report, universe_census, universe_forensics = await _scan(
        BetisMatchbook(),
        EmptyPolymarket(),
        universe_kalshi,
    )
    hot_kalshi = BetisKalshiGetMarket(
        rules_text=GENERIC_SOCCERGAME_TEMPLATE,
        secondary_text=GENERIC_SOCCERGAME_TEMPLATE,
        market_rules_text=GENERIC_SOCCERGAME_TEMPLATE,
        market_secondary_text=GENERIC_SOCCERGAME_TEMPLATE,
        rules_on_markets=True,
    )
    hot_report, hot_census, hot_forensics = await _hot_from_same_payload(
        universe_report, kalshi=hot_kalshi
    )
    assert _safe_lane_snapshot(universe_report, universe_census, universe_forensics)["matched_equivalent"] == (
        _safe_lane_snapshot(hot_report, hot_census, hot_forensics)["matched_equivalent"]
    )
    assert universe_census.equivalent_market_pairs == 1
    assert universe_kalshi.contract_terms_calls == [GAMEWIN_URL]
    assert hot_kalshi.contract_terms_calls == []


@pytest.mark.asyncio
async def test_contract_terms_fetch_rejects_non_allowlisted_host() -> None:
    from sports_hedge.config import Settings
    from sports_hedge.venues.kalshi import KalshiClient, KalshiDiscoveryError

    client = KalshiClient(Settings())
    with pytest.raises(KalshiDiscoveryError, match="allowlisted"):
        await client.get_contract_terms_document(
            "https://evil.example/contract_terms/SOCCERGAMEWIN.pdf"
        )

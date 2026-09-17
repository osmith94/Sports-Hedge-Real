"""Issue #260 forensic follow-up: live-shaped 1X2 mapping without loosening settlement.

Deterministic fixtures shaped like owner-live Matchbook/Kalshi/Polymarket payloads.
Not owner-live evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
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
from sports_hedge.domain.football import MarketFamily, SettlementScope
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    PolymarketNormalizer,
    classify_kalshi_contract_rule_layer,
    merge_kalshi_contract_rules,
)
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs

from test_issue260_mapping_census import _collect as _baseline_census_collect

KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
REGULATION = (
    "Resolves based on 90 minutes of regulation time. Extra time and penalties do not count."
)
BETIS = "Real Betis"
GETAFE = "Getafe"
KALSHI_GAME_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
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

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    "id": 9600,
                    "name": f"{BETIS} vs {GETAFE}",
                    "start": KICKOFF.isoformat(),
                    "competition-name": "Premier League",
                }
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": [_mb_match_odds(), *self.extra_markets]}


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
    ) -> None:
        self.event_rules_text = (
            REGULATION if rules_on_event and event_rules_text is None else event_rules_text
        )
        self.market_rules_text = (
            REGULATION if rules_on_markets and market_rules_text is None else market_rules_text
        )

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
            markets.append(item)
        return markets

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
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
        return {"events": [event]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        del series_ticker
        return KALSHI_GAME_SERIES

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


async def _scan(matchbook, polymarket, kalshi=None):
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
            scan_lane=ScanLane.UNIVERSE.value,
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
    assert matcher.match(mb, without_event_rules[0]).matched is False
    assert "incomplete_settlement" in matcher.match(mb, without_event_rules[0]).reasons
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
    assert matcher.match(mb, named_only[0]).matched is False


@pytest.mark.asyncio
async def test_live_shaped_kalshi_without_rules_is_not_equivalent() -> None:
    report, census, forensics = await _scan(
        BetisMatchbook(),
        EmptyPolymarket(),
        BetisKalshi(rules_on_event=False, rules_on_markets=False),
    )
    assert census.equivalent_market_pairs == 0
    assert "incomplete_settlement" in forensics.candidate_rejection_histogram
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
        event_rules_text: str | None = None,
        market_rules_text: str | None = None,
        fail_tickers: tuple[str, ...] = (),
        rules_on_event: bool = False,
        rules_on_markets: bool = False,
    ) -> None:
        super().__init__(
            rules_on_event=rules_on_event,
            rules_on_markets=rules_on_markets,
            event_rules_text=event_rules_text,
            market_rules_text=market_rules_text,
        )
        self.rules_text = rules_text
        self.fail_tickers = {str(item) for item in fail_tickers}
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
            "rules_secondary": "",
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
    assert census.equivalent_market_pairs == 0
    assert "incomplete_settlement" in forensics.candidate_rejection_histogram
    ambiguous = BetisKalshiGetMarket(rules_text=AMBIGUOUS)
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), ambiguous)
    assert census.equivalent_market_pairs == 0
    assert "incomplete_settlement" in forensics.candidate_rejection_histogram


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
    assert census.equivalent_market_pairs == 0
    assert "incomplete_settlement" in forensics.candidate_rejection_histogram
    assert sorted(event_ambiguous.get_market_calls) == sorted(_betis_kalshi_tickers())

    nested_ambiguous = BetisKalshiGetMarket(rules_text=AMBIGUOUS, market_rules_text=AMBIGUOUS)
    _report, census, forensics = await _scan(BetisMatchbook(), EmptyPolymarket(), nested_ambiguous)
    assert census.equivalent_market_pairs == 0
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0
    assert "incomplete_settlement" in forensics.candidate_rejection_histogram
    assert sorted(nested_ambiguous.get_market_calls) == sorted(_betis_kalshi_tickers())
    assert census.kalshi_match_result_rule_enrichment["attempted"] == 3
    assert census.kalshi_match_result_rule_enrichment["empty"] == 3
    assert census.kalshi_match_result_rule_enrichment["applied"] == 0
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
    assert census.equivalent_market_pairs == 0
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0
    assert failed in kalshi.get_market_calls
    assert sorted(kalshi.get_market_calls) == sorted(_betis_kalshi_tickers())
    assert all(item.comparison_status != "matched_equivalent" for item in forensics.candidates)
    assert census.kalshi_match_result_rule_enrichment["failed"] >= 1
    assert census.kalshi_match_result_rule_enrichment["applied"] == 2
    assert forensics.get_market_status_histogram.get("transport_failed") == 1
    assert forensics.get_market_status_histogram.get("ok") == 2


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

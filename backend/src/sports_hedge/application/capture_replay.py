"""Issue #291/#293 read-only capture + offline replay harness.

Feeds a sanitized Matchbook↔Kalshi bundle through the real production path:

    venue payload replay
      -> discovery
      -> event clustering
      -> canonical fixture
      -> approved-market recognition / catalogue
      -> market inventory
      -> price / depth
      -> comparison / solver gate

Phase 1 remains PAPER / read-only. No venue writes. Polymarket is off.
Live capture never fabricates a missing Matchbook side.
When both venues are reachable, attempt-live computes genuine same-event
overlap through production identity clustering rather than leaving overlap=False.
Kalshi discovery uses the bounded configured catalogue-relevant football
series set (GAME/BTTS/TOTAL/FTTS), not GAME-only.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.application.collector import (
    CollectionReport,
    DEFAULT_MAX_EVENT_PAIRS,
    ReadOnlyCrossVenueCollector,
    _NormalizedEvent,
)
from sports_hedge.application.equivalence_diagnostics import (
    zero_equivalent_reason_from_inventory,
)
from sports_hedge.application.fixture_clusters import (
    FixtureCluster,
    cluster_canonical_event_id,
    cluster_member_events,
    cluster_venue_events,
    to_venue_event,
)
from sports_hedge.application.fixture_inventory import (
    InventoryComparisonStatus,
    inventory_is_comparable_opportunity,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.target_competitions import (
    filter_in_scope_events,
    resolve_target_competition_from_kalshi_ticker,
)
from sports_hedge.catalogue.classify import PayloadSide, classify_payload_pair
from sports_hedge.catalogue.corpus import GAMEWIN_URL, KALSHI_GAMEWIN_SERIES
from sports_hedge.config import Settings
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.ordinary_1x2 import is_ordinary_full_time_1x2
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    VenueNormalizationError,
)
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookAuthError, MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient

SCHEMA_VERSION = 2
FORBIDDEN_WRITE_METHODS = (
    "place_order",
    "cancel_order",
    "place_bet",
    "sign_wallet",
    "submit_order",
)

DATA_CLASS_LIVE_CAPTURE = "live_read_only_capture"
DATA_CLASS_HISTORICAL_REPLAY = "historical_replay"
DATA_CLASS_FIXTURE_DEMO = "fixture_demo"
DATA_CLASS_CAPTURED_PUBLIC = "captured_public_payload_shape"
DATA_CLASS_UNAVAILABLE = "unavailable"

PROVENANCE_LIVE = "live_read_only_capture"
PROVENANCE_HISTORICAL = "historical_replay"
PROVENANCE_FIXTURE = "fixture_demo"
PROVENANCE_CAPTURED_PUBLIC = "captured_public_payload_shape"
PROVENANCE_UNAVAILABLE = "unavailable"

_APPROVED_HINT_FAMILIES = frozenset(
    {
        MarketFamily.BOTH_TEAMS_TO_SCORE,
        MarketFamily.TOTAL_GOALS,
        MarketFamily.FIRST_TEAM_TO_SCORE,
        MarketFamily.TEAM_TOTAL,
        MarketFamily.DRAW_NO_BET,
        MarketFamily.DOUBLE_CHANCE,
    }
)
_CAPTURE_FAMILY_PRIORITY = (
    MarketFamily.MATCH_RESULT.value,
    MarketFamily.BOTH_TEAMS_TO_SCORE.value,
    MarketFamily.TOTAL_GOALS.value,
    MarketFamily.FIRST_TEAM_TO_SCORE.value,
)

_SECRET_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "secret",
    "authorization",
    "cookie",
    "session",
    "mfa",
    "api_key",
    "apikey",
    "username",
    "private_key",
    "privatekey",
    "wallet",
    "credit_card",
    "account_number",
)
_SECRET_KEY_EXACT = frozenset({"token", "email", "headers", "authorization"})
_SECRET_KEY_ALLOW = frozenset(
    {
        "event_ticker",
        "series_ticker",
        "market_ticker",
        "ticker",
        "yes_sub_title",
        "no_sub_title",
        "subtitle",
        "title",
        "sub_title",
    }
)

DEFAULT_KALSHI_BOOK: dict[str, Any] = {
    "orderbook_fp": {
        "yes_dollars": [["0.47", "100.00"]],
        "no_dollars": [["0.52", "200.00"]],
    }
}

SCENARIO3_KICKOFF = datetime(2026, 9, 18, 18, 30, tzinfo=UTC)
SCENARIO3_SERIES: dict[str, Any] = {
    "ticker": "KXSERIEAGAME",
    "title": "Serie A",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "contract_terms_url": GAMEWIN_URL,
    "contract_family": dict(KALSHI_GAMEWIN_SERIES["contract_family"]),
}


class CaptureReplayError(RuntimeError):
    """Replay or capture refused without fabricating venue data."""


class ReplayVenueSide(BaseModel):
    present: bool = False
    provenance: str = PROVENANCE_UNAVAILABLE
    data_class: str = DATA_CLASS_UNAVAILABLE
    unavailable_reason: str | None = None
    event: dict[str, Any] | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    markets: list[dict[str, Any]] = Field(default_factory=list)
    markets_by_event: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    series: dict[str, Any] | None = None
    series_by_ticker: dict[str, dict[str, Any]] = Field(default_factory=dict)
    order_books: dict[str, dict[str, Any]] = Field(default_factory=dict)
    contract_terms: dict[str, dict[str, Any]] = Field(default_factory=dict)
    identified_as: str | None = None


class ReplayBundle(BaseModel):
    bundle_id: str
    schema_version: int = SCHEMA_VERSION
    captured_at: datetime
    data_class: str
    identified_as: str
    paper_mode: str = "paper"
    execution_enabled: bool = False
    polymarket_included: bool = False
    football_only: bool = True
    matchbook: ReplayVenueSide = Field(default_factory=ReplayVenueSide)
    kalshi: ReplayVenueSide = Field(default_factory=ReplayVenueSide)
    fx_snapshots: list[dict[str, Any]] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def assert_paper_only(self) -> None:
        if self.paper_mode != "paper":
            raise CaptureReplayError("replay bundle must remain SPORTS_HEDGE_MODE=paper")
        if self.execution_enabled:
            raise CaptureReplayError("replay bundle must keep execution disabled")
        if self.polymarket_included:
            raise CaptureReplayError("Issue #291 replay is Matchbook↔Kalshi only")


class QuoteSummary(BaseModel):
    outcome: str
    decimal_odds: Decimal | None = None
    size_at_touch: Decimal | None = None


class ReplayRowSummary(BaseModel):
    display_name: str
    family: str | None = None
    period: str | None = None
    line: Decimal | None = None
    comparison_status: str
    catalogue_admission_allowed: bool | None = None
    settlement_status: str | None = None
    entered_solver: bool = False
    solver_model: str | None = None
    matchbook_event_id: str | None = None
    matchbook_market_id: str | None = None
    kalshi_event_ticker: str | None = None
    kalshi_market_id: str | None = None
    matchbook_prices: list[QuoteSummary] = Field(default_factory=list)
    kalshi_prices: list[QuoteSummary] = Field(default_factory=list)
    matchbook_depth: Decimal | None = None
    kalshi_depth: Decimal | None = None
    net_edge: Decimal | None = None
    arb: bool = False
    block_reason: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)


class ReplaySummary(BaseModel):
    captured_at: datetime
    bundle_id: str
    data_class: str
    identified_as: str
    fixture: str | None = None
    competition: str | None = None
    matchbook_event_id: str | None = None
    matchbook_market_ids: list[str] = Field(default_factory=list)
    kalshi_event_ticker: str | None = None
    kalshi_tickers: list[str] = Field(default_factory=list)
    canonical_fixture_id: str | None = None
    canonical_market_key: str | None = None
    catalogue_state: str | None = None
    settlement_status: str | None = None
    matchbook_matched: bool = False
    kalshi_matched: bool = False
    matched_equivalent: bool = False
    catalogue_admission_allowed: bool | None = None
    entered_solver: bool = False
    comparison_economics_computed: bool = False
    matchbook_prices: list[QuoteSummary] = Field(default_factory=list)
    kalshi_prices: list[QuoteSummary] = Field(default_factory=list)
    matchbook_depth: Decimal | None = None
    kalshi_depth: Decimal | None = None
    net_edge: Decimal | None = None
    arb: bool = False
    block_reason: str | None = None
    rows: list[ReplayRowSummary] = Field(default_factory=list)
    paper_mode: str = "paper"
    execution_enabled: bool = False
    network_used: bool = False
    provenance: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class VenueAttempt(BaseModel):
    venue: str
    credentials_present: bool
    reachable: bool | None = None
    events_listed: int = 0
    unavailable_reason: str | None = None
    provenance: str
    data_class: str
    public_event_summaries: list[dict[str, Any]] = Field(default_factory=list)


class CaptureAttemptReport(BaseModel):
    captured_at: datetime
    data_class: str = DATA_CLASS_LIVE_CAPTURE
    identified_as: str
    paper_mode: str = "paper"
    execution_enabled: bool = False
    polymarket_included: bool = False
    football_only: bool = True
    existing_same_event_capture_found: bool = False
    matchbook: VenueAttempt
    kalshi: VenueAttempt
    same_event_overlap_found: bool = False
    overlap_count: int = 0
    overlap_fixture: str | None = None
    overlap_competition: str | None = None
    approved_family_on_both: bool | None = None
    catalogue_state: str | None = None
    canonical_market_key: str | None = None
    matched_equivalent: bool | None = None
    comparison_economics_computed: bool | None = None
    arb: bool | None = None
    block_reason: str | None = None
    replay_bundle_path: str | None = None
    notes: list[str] = Field(default_factory=list)


class ReplayKalshi:
    def __init__(self, side: ReplayVenueSide) -> None:
        self.side = side
        self.get_market_calls: list[str] = []
        if side.contract_terms:
            self.get_contract_terms_document = self._get_contract_terms_document

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        events = _replay_kalshi_events(self.side)
        series_tickers = filters.get("series_tickers")
        ticker = filters.get("series_ticker")
        wanted: set[str] = set()
        if isinstance(series_tickers, str):
            wanted = {part.strip() for part in series_tickers.split(",") if part.strip()}
        elif series_tickers:
            wanted = {str(item).strip() for item in series_tickers if str(item).strip()}
        elif ticker:
            wanted = {str(ticker).strip()}
        if wanted:
            events = [
                item
                for item in events
                if str(item.get("series_ticker") or "").strip() in wanted
            ]
        return {"events": [sanitize_payload(item) for item in events]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        by_event = self.side.markets_by_event.get(str(event_id))
        if by_event:
            return {"markets": list(by_event)}
        return {"markets": list(self.side.markets or [])}

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.get_market_calls.append(ticker)
        for market in _kalshi_markets(self.side):
            if str(market.get("ticker") or "") == str(ticker):
                return dict(market)
        raise LookupError(f"replay bundle has no Kalshi market {ticker}")

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        book = self.side.order_books.get(str(market_id))
        if book is None:
            raise LookupError(f"replay bundle has no captured Kalshi book for {market_id}")
        return dict(book)

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        mapped = (self.side.series_by_ticker or {}).get(str(series_ticker))
        if mapped:
            return dict(mapped)
        series = dict(self.side.series or {})
        series.setdefault("ticker", series_ticker)
        return series

    async def _get_contract_terms_document(self, url: str) -> dict[str, Any]:
        document = (self.side.contract_terms or {}).get(str(url))
        if document is None:
            raise LookupError(f"replay bundle has no captured Kalshi contract terms for {url}")
        return dict(document)


class ReplayMatchbook:
    def __init__(self, side: ReplayVenueSide) -> None:
        self.side = side

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        if not self.side.present or not self.side.event:
            return {"events": []}
        event = {key: value for key, value in self.side.event.items() if key != "markets"}
        return {"events": [sanitize_payload(event)]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        markets = list(self.side.markets)
        if not markets and isinstance(self.side.event, dict):
            nested = self.side.event.get("markets")
            if isinstance(nested, list):
                markets = [item for item in nested if isinstance(item, dict)]
        return {"markets": [sanitize_payload(item) for item in markets]}


class DisabledPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []


def backend_fixtures_dir() -> Path:
    return Path(__file__).resolve().parents[3] / "tests" / "fixtures"


def captured_bayern_union_path() -> Path:
    return backend_fixtures_dir() / "kalshi_trade_api_bundesliga_bayern_union_2026-09-18.json"


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def dump_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=_json_default) + "\n", encoding="utf-8")


def sanitize_payload(value: Any) -> Any:
    """Drop credentials, auth headers, session tokens and personal fields."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _key_is_secret(str(key)):
                continue
            cleaned[str(key)] = sanitize_payload(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


def assert_no_write_methods() -> None:
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            if hasattr(client, method):
                raise CaptureReplayError(f"{client.__name__}.{method} must not exist")


def replay_fx_snapshots(bundle: ReplayBundle) -> list[FxRateSnapshot]:
    if bundle.fx_snapshots:
        return [FxRateSnapshot.model_validate(item) for item in bundle.fx_snapshots]
    if bundle.data_class == DATA_CLASS_FIXTURE_DEMO:
        captured = datetime.now(UTC)
        return [
            FxRateSnapshot(
                currency="USD",
                gbp_per_unit=Decimal("0.75"),
                source="fixture_demo_fx",
                captured_at=captured,
            ),
            FxRateSnapshot(
                currency="GBP",
                gbp_per_unit=Decimal("1"),
                source="functional_currency",
                captured_at=captured,
            ),
        ]
    raise CaptureReplayError(
        "replay bundle is missing FX snapshots; live/historical economics must not invent FX"
    )


def replay_venue_costs(bundle: ReplayBundle) -> list[Any]:
    captured = datetime.now(UTC) if bundle.data_class == DATA_CLASS_FIXTURE_DEMO else bundle.captured_at
    from sports_hedge.fees.cost import MarketAction, VenueCostSnapshot

    matchbook_cost = VenueCostSnapshot.per_quote_profit_commission(
        VenueName.MATCHBOOK,
        Decimal("0.02"),
        action=MarketAction.BACK,
        source=f"{bundle.data_class}_matchbook_fee",
        captured_at=captured,
        currency="GBP",
        detail="Issue #291 replay uses the documented Matchbook paper commission snapshot",
    )
    kalshi_cost = kalshi_cost_from_series(
        bundle.kalshi.series or SCENARIO3_SERIES,
        event=bundle.kalshi.event,
        captured_at=captured,
    )
    return [matchbook_cost, kalshi_cost]


async def replay_bundle(bundle: ReplayBundle) -> tuple[CollectionReport, ReplaySummary]:
    """Run the production collector against a sanitized replay bundle. No network."""

    bundle.assert_paper_only()
    assert_no_write_methods()
    settings = Settings()
    if settings.sports_hedge_mode != "paper" or settings.sports_hedge_execution_enabled:
        raise CaptureReplayError("Settings must remain paper-only with execution disabled")
    sanitized = ReplayBundle.model_validate(sanitize_payload(bundle.model_dump(mode="json")))
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=ReplayMatchbook(sanitized.matchbook),
        polymarket=DisabledPolymarket(),
        kalshi=ReplayKalshi(sanitized.kalshi),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=replay_venue_costs(sanitized),
            fx_snapshots=replay_fx_snapshots(sanitized),
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            unbounded_cycle=True,
        )
    finally:
        repository.close()
    return report, summarize_replay(sanitized, report)


def summarize_replay(bundle: ReplayBundle, report: CollectionReport) -> ReplaySummary:
    clustered = [
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched or item.kalshi_matched
    ]
    fixture = clustered[0] if clustered else (report.discovered_fixtures[0] if report.discovered_fixtures else None)
    rows = report.fixture_markets.get(fixture.canonical_event_id, []) if fixture else []
    equivalent = [
        row
        for row in rows
        if inventory_is_comparable_opportunity(row.comparison_status)
    ]
    proven = [
        row
        for row in equivalent
        if row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
    ]
    focus = proven[0] if proven else (equivalent[0] if equivalent else (rows[0] if rows else None))
    catalogue_state, catalogue_allowed, settlement_status = _catalogue_fields(bundle, focus)
    block_reason = None
    if fixture is not None and not equivalent:
        block_reason = zero_equivalent_reason_from_inventory(rows) or fixture.no_comparison_reason
    if focus is not None and focus.reason and not equivalent:
        block_reason = block_reason or focus.reason
    row_summaries = [_row_summary(row, catalogue_allowed, settlement_status) for row in rows]
    focus_summary = next(
        (item for item in row_summaries if item.comparison_status == InventoryComparisonStatus.MATCHED_EQUIVALENT.value),
        row_summaries[0] if row_summaries else None,
    )
    return ReplaySummary(
        captured_at=bundle.captured_at,
        bundle_id=bundle.bundle_id,
        data_class=bundle.data_class,
        identified_as=bundle.identified_as,
        fixture=_fixture_label(fixture, bundle),
        competition=fixture.competition if fixture is not None else _competition(bundle),
        matchbook_event_id=_matchbook_event_id(bundle),
        matchbook_market_ids=_matchbook_market_ids(bundle),
        kalshi_event_ticker=_kalshi_event_ticker(bundle),
        kalshi_tickers=_kalshi_tickers(bundle),
        canonical_fixture_id=fixture.canonical_event_id if fixture is not None else None,
        canonical_market_key=_canonical_market_key(focus),
        catalogue_state=catalogue_state,
        settlement_status=settlement_status,
        matchbook_matched=bool(fixture.matchbook_matched) if fixture is not None else False,
        kalshi_matched=bool(fixture.kalshi_matched) if fixture is not None else False,
        matched_equivalent=bool(equivalent),
        catalogue_admission_allowed=catalogue_allowed,
        entered_solver=any(row.entered_solver for row in equivalent),
        comparison_economics_computed=any(
            row.entered_solver and row.current_net_edge is not None for row in equivalent
        ),
        matchbook_prices=list(focus_summary.matchbook_prices) if focus_summary is not None else [],
        kalshi_prices=list(focus_summary.kalshi_prices) if focus_summary is not None else [],
        matchbook_depth=focus.matchbook.usable_depth_at_touch if focus is not None and focus.matchbook else None,
        kalshi_depth=focus.kalshi.usable_depth_at_touch if focus is not None and focus.kalshi else None,
        net_edge=focus.current_net_edge if focus is not None else None,
        arb=any(row.solver_is_arbitrage for row in equivalent),
        block_reason=None if equivalent else block_reason,
        rows=row_summaries,
        paper_mode=bundle.paper_mode,
        execution_enabled=False,
        network_used=False,
        provenance={
            "bundle": bundle.data_class,
            "matchbook": bundle.matchbook.provenance,
            "kalshi": bundle.kalshi.provenance,
        },
        notes=list(bundle.notes),
    )


def scenario2_bayern_fair_price_bundle(
    captured_kalshi: dict[str, Any] | None = None,
) -> ReplayBundle:
    """Negative fixture/demo: captured Kalshi fair-price wording + synthetic Matchbook."""

    captured = captured_kalshi or load_json(captured_bayern_union_path())
    payload = sanitize_payload(captured.get("payload") or captured)
    kickoff = str(payload.get("strike_date") or SCENARIO3_KICKOFF.isoformat())
    mb_event = {
        "id": 27801,
        "name": "Bayern Munich vs Union Berlin",
        "start": kickoff,
        "competition-name": "Bundesliga",
    }
    mb_market = {
        "id": 27810,
        "name": "Match Odds",
        "runners": [
            {
                "id": 1,
                "name": "Bayern Munich",
                "prices": [{"side": "back", "odds": "1.20", "available-amount": "80"}],
            },
            {
                "id": 2,
                "name": "Draw",
                "prices": [{"side": "back", "odds": "7.50", "available-amount": "80"}],
            },
            {
                "id": 3,
                "name": "Union Berlin",
                "prices": [{"side": "back", "odds": "15.00", "available-amount": "80"}],
            },
        ],
    }
    series = {
        "ticker": "KXBUNDESLIGAGAME",
        "title": "Bundesliga",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "contract_terms_url": GAMEWIN_URL,
        "contract_family": dict(KALSHI_GAMEWIN_SERIES["contract_family"]),
    }
    return ReplayBundle(
        bundle_id="issue291-scenario2-bayern-union-fair-price-negative",
        captured_at=datetime(2026, 9, 18, 8, 15, tzinfo=UTC),
        data_class=DATA_CLASS_FIXTURE_DEMO,
        identified_as=(
            "FIXTURE/DEMO negative regression. Kalshi side is the captured public "
            "Bayern Munich vs Union Berlin Trade API payload (not live quotes). "
            "Matchbook side is a deterministic synthetic Match Odds representation "
            "for the same club names — not authenticated live Matchbook, not a "
            "real same-event capture."
        ),
        matchbook=ReplayVenueSide(
            present=True,
            provenance=PROVENANCE_FIXTURE,
            data_class=DATA_CLASS_FIXTURE_DEMO,
            event=mb_event,
            markets=[mb_market],
            identified_as="Deterministic Matchbook-shaped fixture/demo. Not live Matchbook.",
        ),
        kalshi=ReplayVenueSide(
            present=True,
            provenance=PROVENANCE_CAPTURED_PUBLIC,
            data_class=DATA_CLASS_CAPTURED_PUBLIC,
            event=payload,
            series=series,
            order_books=_kalshi_books_for_event(payload),
            identified_as=str(captured.get("identified_as") or "captured public Kalshi payload"),
        ),
        notes=[
            "Scenario 2 must stay blocked: Kalshi cancel/reschedule-to-fair-price is unmodelled.",
            "Do not treat this as a live Matchbook↔Kalshi overlap or a backtest.",
        ],
    )


def scenario3_safe_90m_bundle(
    captured_kalshi: dict[str, Any] | None = None,
) -> ReplayBundle:
    """Positive fixture/demo control: explicit 90m wording, no fair-price sibling."""

    captured = captured_kalshi or load_json(captured_bayern_union_path())
    payload = rewrite_captured_kalshi(
        captured.get("payload") or captured,
        home="AC Monza",
        away="Sassuolo Calcio",
        title="AC Monza vs Sassuolo Calcio",
        competition="Serie A",
        ticker="KXSERIEAGAME-MONSAS",
        series_ticker="KXSERIEAGAME",
        include_fair_price=False,
    )
    mb_event = {
        "id": 28901,
        "name": "Monza vs Sassuolo",
        "start": SCENARIO3_KICKOFF.isoformat(),
        "competition-name": "Serie A",
    }
    mb_market = {
        "id": 28910,
        "name": "Match Odds",
        "runners": [
            {
                "id": 1,
                "name": "Monza",
                "prices": [{"side": "back", "odds": "2.10", "available-amount": "80"}],
            },
            {
                "id": 2,
                "name": "Draw",
                "prices": [{"side": "back", "odds": "3.40", "available-amount": "80"}],
            },
            {
                "id": 3,
                "name": "Sassuolo",
                "prices": [{"side": "back", "odds": "3.60", "available-amount": "80"}],
            },
        ],
    }
    return ReplayBundle(
        bundle_id="issue291-scenario3-safe-90m-positive",
        captured_at=SCENARIO3_KICKOFF,
        data_class=DATA_CLASS_FIXTURE_DEMO,
        identified_as=(
            "FIXTURE/DEMO positive control. Deterministic Matchbook Match Odds plus "
            "a rewritten captured-Kalshi 90-minute nested shape without the "
            "cancel/reschedule fair-price sibling. Not live quotes, not owner-live "
            "books, not a real same-event capture."
        ),
        matchbook=ReplayVenueSide(
            present=True,
            provenance=PROVENANCE_FIXTURE,
            data_class=DATA_CLASS_FIXTURE_DEMO,
            event=mb_event,
            markets=[mb_market],
            identified_as="Deterministic Matchbook-shaped fixture/demo. Not live Matchbook.",
        ),
        kalshi=ReplayVenueSide(
            present=True,
            provenance=PROVENANCE_FIXTURE,
            data_class=DATA_CLASS_FIXTURE_DEMO,
            event=payload,
            series=dict(SCENARIO3_SERIES),
            order_books=_kalshi_books_for_event(payload),
            identified_as="Rewritten captured Kalshi 90-minute shape. Fixture/demo, not live.",
        ),
        notes=[
            "Scenario 3 is the existing safe 90m combined-path control from the #289 composition.",
            "A MATCHED_EQUIVALENT row here does not prove a live venue overlap.",
        ],
    )


def rewrite_captured_kalshi(
    payload: dict[str, Any],
    *,
    home: str,
    away: str,
    title: str,
    competition: str,
    ticker: str,
    series_ticker: str,
    include_fair_price: bool,
) -> dict[str, Any]:
    rewritten = sanitize_payload(payload)
    rewritten["event_ticker"] = ticker
    rewritten["series_ticker"] = series_ticker
    rewritten["title"] = title
    rewritten["product_metadata"] = {"competition": competition, "competition_scope": "Game"}
    replacements = (
        ("Bayern Munich", home),
        ("Union Berlin", away),
        ("Bundesliga", competition),
        ("KXBUNDESLIGAGAME-26SEP18BMUUNI", ticker),
        ("KXBUNDESLIGAGAME", series_ticker),
    )
    markets = []
    for market in rewritten.get("markets") or []:
        if not isinstance(market, dict):
            continue
        item = dict(market)
        for field in (
            "ticker",
            "event_ticker",
            "title",
            "yes_sub_title",
            "no_sub_title",
            "rules_primary",
            "rules_secondary",
        ):
            text = str(item.get(field) or "")
            for old, new in replacements:
                text = text.replace(old, new)
            item[field] = text
        if not include_fair_price:
            item["rules_secondary"] = ""
        markets.append(item)
    rewritten["markets"] = markets
    return rewritten


async def attempt_live_read_only_capture(
    *,
    settings: Settings | None = None,
    matchbook_client: Any | None = None,
    kalshi_client: Any | None = None,
    venue_costs: list[Any] | None = None,
    fx_snapshots: list[FxRateSnapshot] | None = None,
    bundle_out: Path | None = None,
) -> CaptureAttemptReport:
    """Bounded football-only Matchbook+Kalshi discovery. Never fabricates overlap.

    When both venues are reachable, events are clustered through the production
    EventMatcher / canonical-identity path. Markets, books and the
    collector/catalogue/solver gate run only for a selected genuine overlap.
    """

    captured_at = datetime.now(UTC)
    cfg = settings or Settings(
        kalshi_event_page_limit=25,
        kalshi_event_max_pages=1,
    )
    if cfg.sports_hedge_mode != "paper" or cfg.sports_hedge_execution_enabled:
        raise CaptureReplayError("live capture requires paper mode and execution disabled")
    assert_no_write_methods()
    owns_matchbook = matchbook_client is None
    owns_kalshi = kalshi_client is None
    matchbook_raw: list[dict[str, Any]] = []
    kalshi_raw: list[dict[str, Any]] = []
    matchbook_handle: Any | None = matchbook_client
    kalshi_handle: Any | None = kalshi_client
    overlap_result: _LiveOverlapResult | None = None
    try:
        matchbook, matchbook_raw, matchbook_handle = await _attempt_matchbook(
            cfg, client=matchbook_handle
        )
        kalshi, kalshi_raw, kalshi_handle = await _attempt_kalshi(
            cfg, client=kalshi_handle
        )
        both_reachable = bool(
            matchbook.reachable
            and kalshi.reachable
            and matchbook.events_listed
            and kalshi.events_listed
            and matchbook_handle is not None
            and kalshi_handle is not None
        )
        if both_reachable:
            overlap_result = await _capture_genuine_overlap(
                matchbook_client=matchbook_handle,
                kalshi_client=kalshi_handle,
                matchbook_events=matchbook_raw,
                kalshi_events=kalshi_raw,
                captured_at=captured_at,
                settings=cfg,
                venue_costs=venue_costs,
                fx_snapshots=fx_snapshots,
                bundle_out=bundle_out,
            )
    finally:
        if owns_matchbook:
            await _aclose_client(matchbook_handle)
        if owns_kalshi:
            await _aclose_client(kalshi_handle)

    notes = [
        "Phase A: no genuine same-event Matchbook+Kalshi capture exists in repo fixtures.",
        "Existing Kalshi Bayern/Union Berlin and Matchbook Chelsea/Hull are different events.",
        "Polymarket was not queried.",
        "No venue write/order methods were invoked.",
        "Overlap uses production event identity/clustering, not title equality.",
    ]
    if not matchbook.credentials_present and matchbook_client is None:
        notes.append(
            "Matchbook credentials are absent in this environment; the Matchbook "
            "side was not fabricated."
        )
    overlap_found = bool(overlap_result and overlap_result.same_event_overlap_found)
    if overlap_result is not None:
        notes.extend(overlap_result.notes)
    elif matchbook.credentials_present and kalshi.events_listed:
        notes.append("Both venues listed events but no production-identity overlap was selected.")

    if overlap_result is not None:
        block_reason = overlap_result.block_reason
        approved = overlap_result.approved_family_on_both
        matched = overlap_result.matched_equivalent
        economics = overlap_result.comparison_economics_computed
        arb = overlap_result.arb
    elif not matchbook.credentials_present and matchbook_client is None:
        block_reason = matchbook.unavailable_reason
        approved = None
        matched = None
        economics = None
        arb = None
    else:
        block_reason = matchbook.unavailable_reason or kalshi.unavailable_reason or "no_same_event_overlap"
        approved = None
        matched = None
        economics = None
        arb = None

    return CaptureAttemptReport(
        captured_at=captured_at,
        identified_as=(
            "LIVE read-only capture attempt for Issue #293. Public identifiers only. "
            "Not a paper fill. Not an executable opportunity. A missing Matchbook "
            "side is not replaced with synthetic events. Same-event overlap is "
            "computed through the production identity/catalogue path."
        ),
        paper_mode="paper",
        execution_enabled=False,
        existing_same_event_capture_found=False,
        matchbook=matchbook,
        kalshi=kalshi,
        same_event_overlap_found=overlap_found,
        overlap_count=0 if overlap_result is None else overlap_result.overlap_count,
        overlap_fixture=None if overlap_result is None else overlap_result.overlap_fixture,
        overlap_competition=None if overlap_result is None else overlap_result.overlap_competition,
        approved_family_on_both=approved,
        catalogue_state=None if overlap_result is None else overlap_result.catalogue_state,
        canonical_market_key=None if overlap_result is None else overlap_result.canonical_market_key,
        matched_equivalent=matched,
        comparison_economics_computed=economics,
        arb=arb,
        block_reason=block_reason,
        replay_bundle_path=None if overlap_result is None else overlap_result.replay_bundle_path,
        notes=notes,
    )


def existing_repo_same_event_capture() -> dict[str, Any]:
    """Phase A inspection result. Does not invent a pair."""

    bayern = load_json(captured_bayern_union_path())
    chelsea = load_json(backend_fixtures_dir() / "matchbook_event_chelsea_hull.json")
    bayern_title = str((bayern.get("payload") or {}).get("title") or "")
    chelsea_name = str((chelsea.get("payload") or {}).get("name") or "")
    return {
        "same_event_capture_found": False,
        "kalshi_fixture": {
            "path": str(captured_bayern_union_path().name),
            "title": bayern_title,
            "data_class": bayern.get("data_class"),
            "identified_as": bayern.get("identified_as"),
        },
        "matchbook_fixture": {
            "path": "matchbook_event_chelsea_hull.json",
            "name": chelsea_name,
            "source": chelsea.get("source"),
            "identified_as": chelsea.get("identified_as"),
        },
        "reason": (
            "Kalshi Bayern Munich vs Union Berlin and Matchbook Chelsea vs Hull "
            "are different football matches and cannot prove real cross-venue discovery."
        ),
    }


async def _attempt_matchbook(
    settings: Settings,
    *,
    client: Any | None = None,
) -> tuple[VenueAttempt, list[dict[str, Any]], Any | None]:
    handle = client
    if handle is None:
        username_present = bool(str(settings.matchbook_username or "").strip())
        password_present = bool(str(settings.matchbook_password or "").strip())
        if not username_present or not password_present:
            return (
                VenueAttempt(
                    venue=VenueName.MATCHBOOK.value,
                    credentials_present=False,
                    reachable=None,
                    events_listed=0,
                    unavailable_reason=(
                        "MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD are not set in this "
                        "agent environment. MatchbookClient.list_events requires a session "
                        "and was not called. The Matchbook side was not fabricated."
                    ),
                    provenance=PROVENANCE_UNAVAILABLE,
                    data_class=DATA_CLASS_UNAVAILABLE,
                ),
                [],
                None,
            )
        handle = MatchbookClient(settings)
    try:
        payload = await handle.list_events()
        events = [
            item
            for item in (payload.get("events") if isinstance(payload, dict) else []) or []
            if isinstance(item, dict)
        ]
        summaries = [_public_matchbook_summary(item) for item in events]
        return (
            VenueAttempt(
                venue=VenueName.MATCHBOOK.value,
                credentials_present=True,
                reachable=True,
                events_listed=len(events),
                provenance=PROVENANCE_LIVE,
                data_class=DATA_CLASS_LIVE_CAPTURE,
                public_event_summaries=summaries,
            ),
            events,
            handle,
        )
    except MatchbookAuthError as exc:
        return (
            VenueAttempt(
                venue=VenueName.MATCHBOOK.value,
                credentials_present=True,
                reachable=False,
                events_listed=0,
                unavailable_reason=_safe_exception(exc),
                provenance=PROVENANCE_UNAVAILABLE,
                data_class=DATA_CLASS_UNAVAILABLE,
            ),
            [],
            handle,
        )
    except Exception as exc:
        return (
            VenueAttempt(
                venue=VenueName.MATCHBOOK.value,
                credentials_present=True,
                reachable=False,
                events_listed=0,
                unavailable_reason=_safe_exception(exc),
                provenance=PROVENANCE_UNAVAILABLE,
                data_class=DATA_CLASS_UNAVAILABLE,
            ),
            [],
            handle,
        )


async def _attempt_kalshi(
    settings: Settings,
    *,
    client: Any | None = None,
) -> tuple[VenueAttempt, list[dict[str, Any]], Any | None]:
    handle = client if client is not None else KalshiClient(settings)
    try:
        reachable = True
        health_fn = getattr(handle, "health", None)
        if callable(health_fn):
            health = await health_fn()
            reachable = bool(getattr(health, "ok", True))
        payload = await handle.list_events(
            series_tickers=catalogue_relevant_kalshi_series(settings),
            limit=min(25, settings.kalshi_event_page_limit),
            with_nested_markets="true",
            with_milestones="true",
        )
        events = [
            item
            for item in (payload.get("events") if isinstance(payload, dict) else []) or []
            if isinstance(item, dict)
        ]
        summaries = [_public_kalshi_summary(item) for item in events]
        return (
            VenueAttempt(
                venue=VenueName.KALSHI.value,
                credentials_present=False,
                reachable=reachable,
                events_listed=len(events),
                provenance=PROVENANCE_LIVE,
                data_class=DATA_CLASS_LIVE_CAPTURE,
                public_event_summaries=summaries,
            ),
            events,
            handle,
        )
    except Exception as exc:
        return (
            VenueAttempt(
                venue=VenueName.KALSHI.value,
                credentials_present=False,
                reachable=False,
                events_listed=0,
                unavailable_reason=_safe_exception(exc),
                provenance=PROVENANCE_UNAVAILABLE,
                data_class=DATA_CLASS_UNAVAILABLE,
            ),
            [],
            handle,
        )


class _LiveOverlapResult:
    def __init__(
        self,
        *,
        same_event_overlap_found: bool,
        overlap_count: int = 0,
        overlap_fixture: str | None = None,
        overlap_competition: str | None = None,
        approved_family_on_both: bool | None = None,
        catalogue_state: str | None = None,
        canonical_market_key: str | None = None,
        matched_equivalent: bool | None = None,
        comparison_economics_computed: bool | None = None,
        arb: bool | None = None,
        block_reason: str | None = None,
        replay_bundle_path: str | None = None,
        notes: list[str] | None = None,
    ) -> None:
        self.same_event_overlap_found = same_event_overlap_found
        self.overlap_count = overlap_count
        self.overlap_fixture = overlap_fixture
        self.overlap_competition = overlap_competition
        self.approved_family_on_both = approved_family_on_both
        self.catalogue_state = catalogue_state
        self.canonical_market_key = canonical_market_key
        self.matched_equivalent = matched_equivalent
        self.comparison_economics_computed = comparison_economics_computed
        self.arb = arb
        self.block_reason = block_reason
        self.replay_bundle_path = replay_bundle_path
        self.notes = list(notes or [])


class RecordingMatchbook:
    """Read-only Matchbook proxy that records list_markets payloads for replay."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.markets_by_event: dict[str, list[dict[str, Any]]] = {}

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        return await self.inner.list_events(**filters)

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        payload = await self.inner.list_markets(event_id, **filters)
        markets: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            markets = [item for item in payload.get("markets") or [] if isinstance(item, dict)]
        self.markets_by_event[str(event_id)] = [sanitize_payload(item) for item in markets]
        return payload


class RecordingKalshi:
    """Read-only Kalshi proxy that records series, markets, books and terms."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.series_by_ticker: dict[str, dict[str, Any]] = {}
        self.markets_by_event: dict[str, list[dict[str, Any]]] = {}
        self.order_books: dict[str, dict[str, Any]] = {}
        self.contract_terms: dict[str, dict[str, Any]] = {}
        self.get_market_payloads: dict[str, dict[str, Any]] = {}
        if getattr(inner, "get_contract_terms_document", None) is None:
            self.get_contract_terms_document = None

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        return await self.inner.list_events(**filters)

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        payload = await self.inner.list_markets(event_id, **filters)
        markets: list[dict[str, Any]] = []
        if isinstance(payload, dict):
            markets = [item for item in payload.get("markets") or [] if isinstance(item, dict)]
        self.markets_by_event[str(event_id)] = [sanitize_payload(item) for item in markets]
        return payload

    async def get_market(self, ticker: str) -> dict[str, Any]:
        payload = await self.inner.get_market(ticker)
        if isinstance(payload, dict):
            self.get_market_payloads[str(ticker)] = sanitize_payload(payload)
        return payload

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        payload = await self.inner.get_order_book(
            event_id, market_id, outcome_id, **filters
        )
        if isinstance(payload, dict):
            self.order_books[str(market_id)] = sanitize_payload(payload)
        return payload

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        payload = await self.inner.get_series(series_ticker)
        if isinstance(payload, dict):
            self.series_by_ticker[str(series_ticker)] = sanitize_payload(payload)
        return payload

    async def get_contract_terms_document(self, url: str) -> dict[str, Any]:
        getter = getattr(self.inner, "get_contract_terms_document", None)
        if getter is None:
            raise LookupError("inner Kalshi client has no get_contract_terms_document")
        payload = await getter(url)
        if isinstance(payload, dict):
            self.contract_terms[str(url)] = sanitize_payload(payload)
        return payload


async def _capture_genuine_overlap(
    *,
    matchbook_client: Any,
    kalshi_client: Any,
    matchbook_events: list[dict[str, Any]],
    kalshi_events: list[dict[str, Any]],
    captured_at: datetime,
    settings: Settings,
    venue_costs: list[Any] | None,
    fx_snapshots: list[FxRateSnapshot] | None,
    bundle_out: Path | None,
) -> _LiveOverlapResult:
    clusters = cluster_live_matchbook_kalshi_events(matchbook_events, kalshi_events)
    overlaps = [
        cluster
        for cluster in clusters
        if cluster.matchbook is not None and cluster.kalshi is not None
    ]
    if not overlaps:
        return _LiveOverlapResult(
            same_event_overlap_found=False,
            overlap_count=0,
            block_reason="no_same_event_overlap",
            notes=[
                "Both venues listed football events, but production EventMatcher "
                "clustering found no Matchbook↔Kalshi same-event pair. No overlap "
                "was fabricated from titles."
            ],
        )
    selected = select_overlap_cluster(overlaps)
    canonical_id = cluster_canonical_event_id(selected)
    recording_matchbook = RecordingMatchbook(matchbook_client)
    recording_kalshi = RecordingKalshi(kalshi_client)
    known = {
        canonical_id: [
            {
                "venue": item.venue.value,
                "source_event_id": item.source_event_id,
                "raw": item.raw,
            }
            for item in cluster_member_events(selected)
            if item.venue in {VenueName.MATCHBOOK, VenueName.KALSHI}
        ]
    }
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=recording_matchbook,
        polymarket=DisabledPolymarket(),
        kalshi=recording_kalshi,
        paper_scan=PaperScanService(
            MarketIntelligenceService(repository),
            settings=settings,
            fx_service=None if fx_snapshots is not None else _try_fx_service(settings),
            cost_resolver=VenueCostResolver(),
        ),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=venue_costs,
            fx_snapshots=fx_snapshots,
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            unbounded_cycle=True,
            reuse_discovery=True,
            known_source_events=known,
            identity_scope=[canonical_id],
        )
    finally:
        repository.close()

    bundle = build_live_overlap_bundle(
        cluster=selected,
        captured_at=captured_at,
        recording_matchbook=recording_matchbook,
        recording_kalshi=recording_kalshi,
        report=report,
        fx_snapshots=fx_snapshots,
    )
    replay_summary = summarize_replay(bundle, report)
    bundle_path: str | None = None
    if bundle_out is not None:
        dump_json(bundle_out, bundle.model_dump(mode="json"))
        bundle_path = str(bundle_out)
    fixture_label = replay_summary.fixture or _cluster_fixture_label(selected)
    return _LiveOverlapResult(
        same_event_overlap_found=True,
        overlap_count=len(overlaps),
        overlap_fixture=fixture_label,
        overlap_competition=replay_summary.competition or _cluster_competition(selected),
        approved_family_on_both=replay_summary.catalogue_admission_allowed,
        catalogue_state=replay_summary.catalogue_state,
        canonical_market_key=replay_summary.canonical_market_key,
        matched_equivalent=replay_summary.matched_equivalent,
        comparison_economics_computed=replay_summary.comparison_economics_computed,
        arb=replay_summary.arb,
        block_reason=replay_summary.block_reason,
        replay_bundle_path=bundle_path,
        notes=[
            f"Production identity clustering found {len(overlaps)} Matchbook↔Kalshi overlap(s).",
            f"Selected overlap {fixture_label} for approved-catalogue market/book fetch.",
            "Kalshi discovery used the bounded catalogue-relevant football series set, not GAME-only.",
            "Collector/catalogue/inventory/solver ran on the selected pair only.",
            *(
                [f"Sanitized ReplayBundle saved to {bundle_path}."]
                if bundle_path
                else []
            ),
        ],
    )


def cluster_live_matchbook_kalshi_events(
    matchbook_events: list[dict[str, Any]],
    kalshi_events: list[dict[str, Any]],
    *,
    matcher: EventMatcher | None = None,
) -> list[FixtureCluster]:
    """Cluster live venue events using the production identity path."""

    mb_scope = filter_in_scope_events(matchbook_events, venue=VenueName.MATCHBOOK)
    kalshi_scope = filter_in_scope_events(kalshi_events, venue=VenueName.KALSHI)
    matchbook_items = _normalize_live_events(mb_scope.allowed, venue=VenueName.MATCHBOOK)
    kalshi_items = _normalize_live_events(kalshi_scope.allowed, venue=VenueName.KALSHI)
    clusters, _counts = cluster_venue_events(
        matchbook=matchbook_items,
        polymarket=[],
        kalshi=kalshi_items,
        matcher=matcher or EventMatcher(),
        max_event_pairs=DEFAULT_MAX_EVENT_PAIRS,
    )
    return clusters


def catalogue_relevant_kalshi_series(settings: Settings | None = None) -> list[str]:
    """Bounded configured football series for owner-live attempt-live discovery.

    Uses Settings.kalshi_series_tickers (GAME/BTTS/TOTAL/FTTS for target
    competitions), not GAME-only. Tickers outside the target-competition
    prefixes are dropped so arbitrary Kalshi sports are not listed.
    """

    configured = [
        str(item).strip()
        for item in list((settings or Settings()).kalshi_series_tickers or [])
        if str(item).strip()
    ]
    bounded = [
        ticker
        for ticker in configured
        if resolve_target_competition_from_kalshi_ticker(ticker) is not None
    ]
    if bounded:
        return bounded
    return [
        ticker
        for ticker in Settings().kalshi_series_tickers
        if resolve_target_competition_from_kalshi_ticker(str(ticker).strip()) is not None
    ]


def select_overlap_cluster(overlaps: list[FixtureCluster]) -> FixtureCluster:
    """Prefer an overlap that exposes an approved catalogue family.

    GAME-series 1X2 without regulation evidence ranks below BTTS/totals/FTTS
    and below 1X2 that already carries 90-minute wording. Title equality is
    not used.
    """

    ranked = sorted(
        overlaps,
        key=lambda cluster: (
            _overlap_family_rank(cluster),
            cluster_canonical_event_id(cluster),
        ),
    )
    return ranked[0]


def build_live_overlap_bundle(
    *,
    cluster: FixtureCluster,
    captured_at: datetime,
    recording_matchbook: RecordingMatchbook,
    recording_kalshi: RecordingKalshi,
    report: CollectionReport,
    fx_snapshots: list[FxRateSnapshot] | None,
) -> ReplayBundle:
    focus_rows = _catalogue_fixture_rows(report)
    kalshi_items = _select_captured_kalshi_events(cluster, focus_rows)
    matchbook_item = cluster.matchbook
    mb_event = sanitize_payload(matchbook_item.raw) if matchbook_item else None
    kalshi_events = [
        sanitize_payload(item.raw)
        for item in kalshi_items
        if isinstance(getattr(item, "raw", None), dict)
    ]
    kalshi_event = kalshi_events[0] if kalshi_events else None
    mb_event_id = str(matchbook_item.source_event_id) if matchbook_item else ""
    mb_markets = list(recording_matchbook.markets_by_event.get(mb_event_id) or [])
    if not mb_markets and isinstance(mb_event, dict):
        nested = mb_event.get("markets")
        if isinstance(nested, list):
            mb_markets = [sanitize_payload(item) for item in nested if isinstance(item, dict)]
    mb_wanted = {
        str(row.matchbook.source_market_id)
        for row in focus_rows
        if getattr(row, "matchbook", None) is not None
    }
    mb_markets = (
        _filter_markets_by_ids(mb_markets, mb_wanted, id_fields=("id", "source_market_id"))
        if mb_wanted
        else mb_markets
    )
    markets_by_event: dict[str, list[dict[str, Any]]] = {}
    all_kalshi_markets: list[dict[str, Any]] = []
    kalshi_wanted = {
        str(row.kalshi.source_market_id)
        for row in focus_rows
        if getattr(row, "kalshi", None) is not None
    }
    equivalent_kalshi_ids = {
        str(row.kalshi.source_market_id)
        for row in focus_rows
        if getattr(row, "kalshi", None) is not None
        and inventory_is_comparable_opportunity(getattr(row, "comparison_status", None))
    }
    for item in kalshi_items:
        event_id = str(item.source_event_id)
        markets = list(recording_kalshi.markets_by_event.get(event_id) or [])
        raw = item.raw if isinstance(getattr(item, "raw", None), dict) else {}
        if not markets:
            nested = raw.get("markets")
            if isinstance(nested, list):
                markets = [sanitize_payload(market) for market in nested if isinstance(market, dict)]
        markets = _merge_kalshi_get_market(markets, recording_kalshi.get_market_payloads)
        if kalshi_wanted:
            markets = _filter_markets_by_ids(markets, kalshi_wanted, id_fields=("ticker", "id"))
        markets_by_event[event_id] = markets
        all_kalshi_markets.extend(markets)
    series_by_ticker = {
        str(ticker): sanitize_payload(payload)
        for ticker, payload in dict(recording_kalshi.series_by_ticker).items()
    }
    series = _series_for_event(kalshi_event, recording_kalshi)
    order_books = dict(recording_kalshi.order_books)
    book_ids = equivalent_kalshi_ids or kalshi_wanted
    if book_ids:
        filtered_books = {
            key: value for key, value in order_books.items() if str(key) in book_ids
        }
        if filtered_books:
            order_books = filtered_books
    fixture = _cluster_fixture_label(cluster)
    return ReplayBundle(
        bundle_id=f"issue293-live-overlap-{_kalshi_cluster_id(kalshi_items) or mb_event_id or 'pair'}",
        captured_at=captured_at,
        data_class=DATA_CLASS_LIVE_CAPTURE,
        identified_as=(
            "LIVE read-only same-event Matchbook↔Kalshi capture. "
            f"Fixture {fixture}. Not a paper fill. Not an executable live order. "
            "Replay uses these sanitized captured payloads with network unused. "
            "Sibling GAME/BTTS/TOTAL/FTTS containers are preserved on one fixture."
        ),
        matchbook=ReplayVenueSide(
            present=mb_event is not None,
            provenance=PROVENANCE_LIVE,
            data_class=DATA_CLASS_LIVE_CAPTURE,
            event=mb_event if isinstance(mb_event, dict) else None,
            markets=mb_markets,
            identified_as="Live read-only Matchbook event/markets. Not a venue write.",
        ),
        kalshi=ReplayVenueSide(
            present=bool(kalshi_events),
            provenance=PROVENANCE_LIVE,
            data_class=DATA_CLASS_LIVE_CAPTURE,
            event=kalshi_event if isinstance(kalshi_event, dict) else None,
            events=kalshi_events,
            markets=all_kalshi_markets,
            markets_by_event=markets_by_event,
            series=series,
            series_by_ticker=series_by_ticker,
            order_books=order_books,
            contract_terms=dict(recording_kalshi.contract_terms),
            identified_as="Live read-only Kalshi sibling events/markets/books. Not a venue write.",
        ),
        fx_snapshots=_fx_payloads_from_report(report, fx_snapshots),
        notes=[
            "Captured through production identity clustering and the collector gate.",
            "Do not treat a MATCHED_EQUIVALENT or PAPER_ASSUMED_EQUIVALENT row as a live executable fill.",
        ],
    )


def default_replay_bundle_path(attempt_out: Path) -> Path:
    return attempt_out.with_name(f"{attempt_out.stem}.replay-bundle.json")


def _normalize_live_events(
    payloads: list[dict[str, Any]],
    *,
    venue: VenueName,
) -> list[Any]:
    normalizer: MatchbookNormalizer | KalshiNormalizer = (
        MatchbookNormalizer() if venue is VenueName.MATCHBOOK else KalshiNormalizer()
    )
    items: list[Any] = []
    for payload in payloads:
        try:
            canonical = normalizer.normalize_event(payload)
        except (VenueNormalizationError, ValueError):
            continue
        items.append(to_venue_event(_NormalizedEvent(payload, canonical), venue))
    return items


def _overlap_family_rank(cluster: FixtureCluster) -> int:
    """Lower is better. Approved BTTS/totals/90m-1X2 beat GAME-only blocked 1X2."""

    if not cluster.kalshi_events:
        return 3
    return min(_kalshi_source_rank(item) for item in cluster.kalshi_events)


def _kalshi_source_rank(item: Any) -> int:
    raw = item.raw if isinstance(getattr(item, "raw", None), dict) else {}
    series_ticker = str(raw.get("series_ticker") or "")
    hint = _series_family_hint(series_ticker)
    if hint in {"btts", "total", "ftts"}:
        return 0
    markets = [market for market in raw.get("markets") or [] if isinstance(market, dict)]
    assembled: list[Any] = []
    if markets:
        try:
            normalizer = KalshiNormalizer()
            event = normalizer.normalize_event(raw)
            assembled = normalizer.assemble_canonical_markets(
                event, markets, event_payload=raw
            )
        except (VenueNormalizationError, ValueError):
            assembled = []
    families = {getattr(market, "family", None) for market in assembled}
    if families & _APPROVED_HINT_FAMILIES:
        return 0
    has_1x2 = any(
        getattr(market, "family", None) is MarketFamily.MATCH_RESULT
        or is_ordinary_full_time_1x2(market)
        for market in assembled
    )
    if has_1x2 or hint == "game":
        if _markets_have_regulation_wording(markets):
            return 0
        return 2
    if assembled:
        return 2
    return 3


def _series_family_hint(series_ticker: str) -> str:
    ticker = str(series_ticker or "").strip().upper()
    if "BTTS" in ticker:
        return "btts"
    if "FTTS" in ticker:
        return "ftts"
    if "TOTAL" in ticker:
        return "total"
    if ticker.endswith("GAME"):
        return "game"
    return "other"


def _markets_have_regulation_wording(markets: list[dict[str, Any]]) -> bool:
    text = " ".join(
        str(item.get("rules_primary") or "") + " " + str(item.get("rules_secondary") or "")
        for item in markets
    ).casefold()
    return "90 minute" in text or "90 min" in text


def _catalogue_fixture_rows(report: CollectionReport) -> list[Any]:
    """Keep every Phase-1 family inventory row on the clustered fixture.

    Capture must preserve GAME/BTTS/TOTAL/FTTS siblings, including
    PAPER_ASSUMED 1X2, not only the first APPROVED_EQUIVALENT family.
    """

    from sports_hedge.catalogue.registry import family_to_registry_archetype, phase1_expensive_work_families

    rows: list[Any] = []
    for group in report.fixture_markets.values():
        rows.extend(group)
    families = phase1_expensive_work_families()
    kept: list[Any] = []
    for row in rows:
        family = getattr(row, "family", None)
        if family is None:
            continue
        try:
            market_family = family if isinstance(family, MarketFamily) else MarketFamily(str(family))
        except ValueError:
            continue
        if market_family not in families:
            continue
        if family_to_registry_archetype(market_family) is None:
            continue
        kept.append(row)
    if kept:
        return kept
    return [
        row
        for row in rows
        if inventory_is_comparable_opportunity(getattr(row, "comparison_status", None))
    ]


def _select_captured_kalshi_events(cluster: FixtureCluster, focus_rows: list[Any]) -> list[Any]:
    event_ids = {
        str(row.kalshi.source_event_id)
        for row in focus_rows
        if getattr(row, "kalshi", None) is not None
    }
    if event_ids:
        matches = [
            item for item in cluster.kalshi_events if str(item.source_event_id) in event_ids
        ]
        if matches:
            return sorted(matches, key=_kalshi_source_rank)
    if cluster.kalshi_events:
        return sorted(cluster.kalshi_events, key=_kalshi_source_rank)
    if cluster.kalshi is not None:
        return [cluster.kalshi]
    return []


def _kalshi_cluster_id(items: list[Any]) -> str:
    return "|".join(sorted(str(item.source_event_id) for item in items if item is not None))


def _preferred_equivalent_rows(report: CollectionReport) -> list[Any]:
    return _catalogue_fixture_rows(report)


def _select_captured_kalshi_event(cluster: FixtureCluster, focus_rows: list[Any]) -> Any | None:
    items = _select_captured_kalshi_events(cluster, focus_rows)
    return items[0] if items else None


def _filter_markets_by_ids(
    markets: list[dict[str, Any]],
    wanted: set[str],
    *,
    id_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    cleaned = {item.strip() for item in wanted if str(item).strip()}
    if not cleaned or not markets:
        return markets
    filtered: list[dict[str, Any]] = []
    for market in markets:
        if any(str(market.get(field) or "").strip() in cleaned for field in id_fields):
            filtered.append(market)
    return filtered or markets


def _series_for_event(
    kalshi_event: dict[str, Any] | None,
    recording_kalshi: RecordingKalshi,
) -> dict[str, Any] | None:
    ticker = ""
    if isinstance(kalshi_event, dict):
        ticker = str(kalshi_event.get("series_ticker") or "").strip()
    recorded = recording_kalshi.series_by_ticker
    if ticker and ticker in recorded:
        return recorded[ticker]
    if ticker:
        return {"ticker": ticker}
    if recorded:
        return next(iter(recorded.values()))
    return None


def _cluster_fixture_label(cluster: FixtureCluster) -> str:
    anchor = cluster.anchor.canonical
    return f"{anchor.home_team} vs {anchor.away_team}"


def _cluster_competition(cluster: FixtureCluster) -> str | None:
    competition = str(getattr(cluster.anchor.canonical, "competition", "") or "").strip()
    return competition or None


def _merge_kalshi_get_market(
    markets: list[dict[str, Any]],
    get_market_payloads: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    if not get_market_payloads:
        return markets
    merged: list[dict[str, Any]] = []
    for market in markets:
        ticker = str(market.get("ticker") or "")
        extra = get_market_payloads.get(ticker)
        if extra:
            merged.append({**market, **extra})
        else:
            merged.append(market)
    return merged


def _fx_payloads_from_report(
    report: CollectionReport,
    provided: list[FxRateSnapshot] | None,
) -> list[dict[str, Any]]:
    for decision in report.paper_decisions:
        snapshots = getattr(decision, "fx_snapshots", None) or []
        if snapshots:
            return [item.model_dump(mode="json") for item in snapshots]
    if provided:
        return [item.model_dump(mode="json") for item in provided]
    return []


def _try_fx_service(settings: Settings) -> Any | None:
    try:
        from sports_hedge.fx.repository import SqliteFxRateRepository
        from sports_hedge.fx.service import FxRateService

        return FxRateService(
            SqliteFxRateRepository(settings.fx_db_path),
            check_tolerance_bps=Decimal(str(settings.fx_check_tolerance_bps)),
            stale_after_days=settings.fx_stale_after_days,
        )
    except Exception:
        return None


async def _aclose_client(client: Any | None) -> None:
    if client is None:
        return
    closer = getattr(client, "aclose", None)
    if closer is not None:
        await closer()


def _public_kalshi_summary(event: dict[str, Any]) -> dict[str, Any]:
    markets = [item for item in event.get("markets") or [] if isinstance(item, dict)]
    rules = " ".join(
        str(item.get("rules_primary") or "") + " " + str(item.get("rules_secondary") or "")
        for item in markets[:6]
    ).casefold()
    metadata = event.get("product_metadata") if isinstance(event.get("product_metadata"), dict) else {}
    return {
        "event_ticker": event.get("event_ticker"),
        "series_ticker": event.get("series_ticker"),
        "title": event.get("title"),
        "competition": metadata.get("competition"),
        "market_count": len(markets),
        "has_90_minute_clause": "90 minutes" in rules,
        "has_fair_price_clause": "fair price" in rules,
        "tickers": [str(item.get("ticker") or "") for item in markets[:6] if item.get("ticker")],
    }


def _public_matchbook_summary(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": event.get("id"),
        "name": event.get("name"),
        "competition": event.get("competition-name") or event.get("competition_name"),
        "start": event.get("start"),
        "status": event.get("status"),
    }


def _replay_kalshi_events(side: ReplayVenueSide) -> list[dict[str, Any]]:
    events = [item for item in (side.events or []) if isinstance(item, dict)]
    if events:
        return events
    if side.present and isinstance(side.event, dict):
        return [side.event]
    return []


def _kalshi_markets(side: ReplayVenueSide) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group in (side.markets_by_event or {}).values():
        for item in group:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or item.get("id") or "")
            if ticker and ticker in seen:
                continue
            if ticker:
                seen.add(ticker)
            collected.append(item)
    if collected:
        return collected
    if side.markets:
        return list(side.markets)
    for event in _replay_kalshi_events(side):
        nested = event.get("markets")
        if isinstance(nested, list):
            collected.extend(item for item in nested if isinstance(item, dict))
    return collected


def _kalshi_books_for_event(event: dict[str, Any]) -> dict[str, dict[str, Any]]:
    books: dict[str, dict[str, Any]] = {}
    for market in event.get("markets") or []:
        if not isinstance(market, dict):
            continue
        ticker = str(market.get("ticker") or "").strip()
        if ticker:
            books[ticker] = dict(DEFAULT_KALSHI_BOOK)
    return books


def _catalogue_fields(
    bundle: ReplayBundle, focus: Any | None = None
) -> tuple[str | None, bool | None, str | None]:
    if focus is not None and getattr(focus, "comparison_status", None) is not None:
        allowed = inventory_is_comparable_opportunity(focus.comparison_status)
        settlement = getattr(focus, "reason", None)
        return str(focus.comparison_status.value), allowed, settlement
    if not bundle.matchbook.present or not bundle.kalshi.present:
        return None, None, None
    mb_markets = bundle.matchbook.markets or (
        list(bundle.matchbook.event.get("markets") or []) if bundle.matchbook.event else []
    )
    kalshi_markets = _kalshi_markets(bundle.kalshi)
    if not bundle.matchbook.event or not bundle.kalshi.event or not mb_markets or not kalshi_markets:
        return None, None, None
    left = PayloadSide(
        venue=VenueName.MATCHBOOK,
        event=bundle.matchbook.event,
        markets=mb_markets[:1],
    )
    right = PayloadSide(
        venue=VenueName.KALSHI,
        event=bundle.kalshi.event,
        markets=kalshi_markets[:1] if len(kalshi_markets) == 1 else kalshi_markets[:3],
        series=bundle.kalshi.series,
    )
    try:
        assessment = classify_payload_pair(left, right)
    except Exception:
        return None, None, None
    return assessment.state.value, assessment.paper_mode_admitted, assessment.reason


def _row_summary(
    row: Any,
    catalogue_allowed: bool | None,
    settlement_status: str | None,
) -> ReplayRowSummary:
    mb = row.matchbook
    kalshi = row.kalshi
    return ReplayRowSummary(
        display_name=row.display_name,
        family=row.family,
        period=row.period,
        line=row.line,
        comparison_status=row.comparison_status.value,
        catalogue_admission_allowed=catalogue_allowed,
        settlement_status=settlement_status,
        entered_solver=row.entered_solver,
        solver_model=row.solver_model,
        matchbook_event_id=mb.source_event_id if mb else None,
        matchbook_market_id=mb.source_market_id if mb else None,
        kalshi_event_ticker=kalshi.source_event_id if kalshi else None,
        kalshi_market_id=kalshi.source_market_id if kalshi else None,
        matchbook_prices=_quotes(mb),
        kalshi_prices=_quotes(kalshi),
        matchbook_depth=mb.usable_depth_at_touch if mb else None,
        kalshi_depth=kalshi.usable_depth_at_touch if kalshi else None,
        net_edge=row.current_net_edge,
        arb=bool(row.solver_is_arbitrage),
        block_reason=None
        if inventory_is_comparable_opportunity(row.comparison_status)
        else row.reason,
        rejection_reasons=list(row.rejection_reasons or []),
    )


def _quotes(facts: Any) -> list[QuoteSummary]:
    if facts is None:
        return []
    return [
        QuoteSummary(
            outcome=item.outcome,
            decimal_odds=item.decimal_odds,
            size_at_touch=item.size_at_touch,
        )
        for item in facts.best_backs or []
    ]


def _canonical_market_key(row: Any) -> str | None:
    if row is None or not row.family:
        return None
    parts = [str(row.family)]
    if row.period:
        parts.append(str(row.period))
    if row.line is not None:
        parts.append(format(row.line, "f"))
    return "|".join(parts)


def _fixture_label(fixture: Any, bundle: ReplayBundle) -> str | None:
    if fixture is not None:
        return f"{fixture.home_team} vs {fixture.away_team}"
    event = bundle.matchbook.event or bundle.kalshi.event or {}
    return str(event.get("name") or event.get("title") or "") or None


def _competition(bundle: ReplayBundle) -> str | None:
    event = bundle.matchbook.event or {}
    if event.get("competition-name"):
        return str(event["competition-name"])
    kalshi = bundle.kalshi.event or {}
    metadata = kalshi.get("product_metadata") if isinstance(kalshi.get("product_metadata"), dict) else {}
    return str(metadata.get("competition") or "") or None


def _matchbook_event_id(bundle: ReplayBundle) -> str | None:
    if not bundle.matchbook.event:
        return None
    value = bundle.matchbook.event.get("id")
    return str(value) if value is not None else None


def _matchbook_market_ids(bundle: ReplayBundle) -> list[str]:
    markets = bundle.matchbook.markets or (
        list(bundle.matchbook.event.get("markets") or []) if bundle.matchbook.event else []
    )
    ids: list[str] = []
    for market in markets:
        if isinstance(market, dict) and market.get("id") is not None:
            ids.append(str(market["id"]))
    return ids


def _kalshi_event_ticker(bundle: ReplayBundle) -> str | None:
    if not bundle.kalshi.event:
        return None
    value = bundle.kalshi.event.get("event_ticker")
    return str(value) if value else None


def _kalshi_tickers(bundle: ReplayBundle) -> list[str]:
    return [
        str(market.get("ticker"))
        for market in _kalshi_markets(bundle.kalshi)
        if isinstance(market, dict) and market.get("ticker")
    ]


def _key_is_secret(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    if normalized in _SECRET_KEY_ALLOW:
        return False
    if normalized in _SECRET_KEY_EXACT:
        return True
    return any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS)


def _safe_exception(exc: BaseException) -> str:
    text = str(exc)
    lowered = text.casefold()
    for token in ("password", "username", "token", "session", "mfa", "authorization", "secret"):
        if token in lowered:
            return f"{type(exc).__name__}: [redacted]"
    return f"{type(exc).__name__}: {text[:200]}"


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    raise TypeError(f"Cannot serialize {type(value)!r}")


def render_summary(summary: ReplaySummary) -> str:
    payload = summary.model_dump(mode="json")
    return json.dumps(payload, indent=2)


def render_attempt(report: CaptureAttemptReport) -> str:
    return json.dumps(report.model_dump(mode="json"), indent=2)


def load_bundle(path: Path) -> ReplayBundle:
    return ReplayBundle.model_validate(load_json(path))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Sports Hedge Issue #291 PAPER/read-only capture + replay harness"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    replay_parser = sub.add_parser("replay", help="Replay a sanitized bundle with network unused")
    replay_parser.add_argument("--bundle", type=Path, help="ReplayBundle JSON path")
    replay_parser.add_argument(
        "--scenario",
        choices=("2", "3"),
        help="Built-in fixture/demo scenario (2=fair-price negative, 3=safe 90m positive)",
    )
    attempt_parser = sub.add_parser(
        "attempt-live",
        help="Bounded live read-only Matchbook+Kalshi discovery; does not fabricate overlap",
    )
    attempt_parser.add_argument("--out", type=Path, required=True)
    attempt_parser.add_argument(
        "--bundle-out",
        type=Path,
        help="Optional ReplayBundle JSON path. Defaults to <out-stem>.replay-bundle.json",
    )
    args = parser.parse_args(argv)
    if args.command == "attempt-live":
        bundle_out = args.bundle_out or default_replay_bundle_path(args.out)
        report = asyncio.run(attempt_live_read_only_capture(bundle_out=bundle_out))
        dump_json(args.out, report.model_dump(mode="json"))
        print(render_attempt(report))
        if report.replay_bundle_path:
            print(
                "\nReplay bundle saved:\n"
                f"  {report.replay_bundle_path}\n"
                "Offline replay (network unused):\n"
                "  python -m sports_hedge.application.capture_replay "
                f"replay --bundle {report.replay_bundle_path}"
            )
        return 0
    if args.scenario == "2":
        bundle = scenario2_bayern_fair_price_bundle()
    elif args.scenario == "3":
        bundle = scenario3_safe_90m_bundle()
    elif args.bundle:
        bundle = load_bundle(args.bundle)
    else:
        parser.error("replay requires --bundle or --scenario")
        return 2
    _report, summary = asyncio.run(replay_bundle(bundle))
    print(render_summary(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

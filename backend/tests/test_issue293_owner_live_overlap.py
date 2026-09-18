"""Issue #293: attempt-live must compute genuine Matchbook↔Kalshi overlap.

Uses the production identity/clustering path, not title equality.
PAPER / execution off. Polymarket off. No fabricated venue side.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sports_hedge.application.capture_replay import (
    DATA_CLASS_LIVE_CAPTURE,
    DEFAULT_KALSHI_BOOK,
    FORBIDDEN_WRITE_METHODS,
    CaptureAttemptReport,
    ReplayBundle,
    assert_no_write_methods,
    attempt_live_read_only_capture,
    catalogue_relevant_kalshi_series,
    cluster_live_matchbook_kalshi_events,
    replay_bundle,
    replay_venue_costs,
    sanitize_payload,
    scenario3_safe_90m_bundle,
)
from sports_hedge.catalogue.corpus import GAMEWIN_TEMPLATE, KALSHI_GAMEWIN_SERIES, REGULATION
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueHealth, VenueName
from sports_hedge.matching.events import EventMatcher
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient

CHELSEA_HULL = Path(__file__).resolve().parent / "fixtures" / "matchbook_event_chelsea_hull.json"


def _fx() -> list[FxRateSnapshot]:
    captured = datetime.now(UTC)
    return [
        FxRateSnapshot(
            currency="USD",
            gbp_per_unit=Decimal("0.75"),
            source="test_fx",
            captured_at=captured,
        ),
        FxRateSnapshot(
            currency="GBP",
            gbp_per_unit=Decimal("1"),
            source="functional_currency",
            captured_at=captured,
        ),
    ]


class OverlapMatchbook:
    def __init__(self, events: list[dict[str, Any]], markets_by_id: dict[str, list[dict[str, Any]]]) -> None:
        self.events = events
        self.markets_by_id = markets_by_id
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {"events": deepcopy(self.events)}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": deepcopy(self.markets_by_id.get(str(event_id), []))}


class OverlapKalshi:
    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        series: dict[str, Any] | None = None,
        series_by_ticker: dict[str, dict[str, Any]] | None = None,
        books: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.events = events
        self.series = series or {}
        self.series_by_ticker = dict(series_by_ticker or {})
        if self.series.get("ticker"):
            self.series_by_ticker.setdefault(str(self.series["ticker"]), dict(self.series))
        self.books = books or {}
        self.list_events_calls = 0
        self.list_events_filters: list[dict[str, Any]] = []
        self.list_markets_calls: list[str] = []
        self.book_calls: list[str] = []

    async def health(self) -> VenueHealth:
        return VenueHealth(
            venue=VenueName.KALSHI,
            ok=True,
            authenticated=False,
            checked_at=datetime.now(UTC),
        )

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        self.list_events_calls += 1
        self.list_events_filters.append(dict(filters))
        events = deepcopy(self.events)
        series_tickers = filters.get("series_tickers")
        if series_tickers:
            allowed = {str(item).strip() for item in series_tickers if str(item).strip()}
            events = [
                item
                for item in events
                if str(item.get("series_ticker") or "").strip() in allowed
            ]
        return {"events": events}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        for event in self.events:
            ticker = str(event.get("event_ticker") or "")
            if ticker == str(event_id):
                return {"markets": deepcopy(event.get("markets") or [])}
        return {"markets": []}

    async def get_market(self, ticker: str) -> dict[str, Any]:
        for event in self.events:
            for market in event.get("markets") or []:
                if isinstance(market, dict) and str(market.get("ticker") or "") == str(ticker):
                    return deepcopy(market)
        raise LookupError(ticker)

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        self.book_calls.append(str(market_id))
        book = self.books.get(str(market_id))
        if book is None:
            raise LookupError(str(market_id))
        return deepcopy(book)

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        if series_ticker in self.series_by_ticker:
            return deepcopy(self.series_by_ticker[series_ticker])
        series = dict(self.series)
        series.setdefault("ticker", series_ticker)
        return deepcopy(series)


def _scenario3_clients() -> tuple[OverlapMatchbook, OverlapKalshi, Any]:
    bundle = scenario3_safe_90m_bundle()
    mb_event = deepcopy(bundle.matchbook.event or {})
    mb_markets = deepcopy(bundle.matchbook.markets)
    kalshi_event = deepcopy(bundle.kalshi.event or {})
    matchbook = OverlapMatchbook([mb_event], {str(mb_event["id"]): mb_markets})
    kalshi = OverlapKalshi(
        [kalshi_event],
        series=deepcopy(bundle.kalshi.series or {}),
        books=deepcopy(bundle.kalshi.order_books),
    )
    return matchbook, kalshi, bundle


def _chelsea_event() -> dict[str, Any]:
    from sports_hedge.application.capture_replay import load_json

    return deepcopy(load_json(CHELSEA_HULL)["payload"])


def _gamewin_series(ticker: str, title: str) -> dict[str, Any]:
    series = deepcopy(KALSHI_GAMEWIN_SERIES)
    series["ticker"] = ticker
    series["title"] = title
    return series


def _btts_series(ticker: str, title: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "title": title,
        "fee_type": "quadratic",
        "fee_multiplier": 1,
    }


def _kalshi_books_for(event: dict[str, Any]) -> dict[str, dict[str, Any]]:
    books: dict[str, dict[str, Any]] = {}
    for market in event.get("markets") or []:
        if isinstance(market, dict) and market.get("ticker"):
            books[str(market["ticker"])] = deepcopy(DEFAULT_KALSHI_BOOK)
    return books


def _gamewin_blocked_kalshi_event(
    *,
    ticker: str,
    series_ticker: str,
    title: str,
    home: str,
    away: str,
    kickoff: str,
    competition: str,
) -> dict[str, Any]:
    return {
        "event_ticker": ticker,
        "series_ticker": series_ticker,
        "title": title,
        "category": "Sports",
        "strike_date": kickoff,
        "milestone": {"start_date": kickoff},
        "product_metadata": {"competition": competition, "competition_scope": "Game"},
        "markets": [
            {
                "ticker": f"{ticker}-HOME",
                "event_ticker": ticker,
                "title": title,
                "yes_sub_title": home,
                "rules_primary": GAMEWIN_TEMPLATE,
            },
            {
                "ticker": f"{ticker}-DRAW",
                "event_ticker": ticker,
                "title": title,
                "yes_sub_title": "Draw",
                "rules_primary": GAMEWIN_TEMPLATE,
            },
            {
                "ticker": f"{ticker}-AWAY",
                "event_ticker": ticker,
                "title": title,
                "yes_sub_title": away,
                "rules_primary": GAMEWIN_TEMPLATE,
            },
        ],
    }


def _approved_btts_kalshi_event(
    *,
    ticker: str,
    series_ticker: str,
    title: str,
    kickoff: str,
    competition: str,
) -> dict[str, Any]:
    return {
        "event_ticker": ticker,
        "series_ticker": series_ticker,
        "title": title,
        "category": "Sports",
        "strike_date": kickoff,
        "milestone": {"start_date": kickoff},
        "product_metadata": {"competition": competition, "competition_scope": "Game"},
        "markets": [
            {
                "ticker": f"{ticker}-BTTS",
                "event_ticker": ticker,
                "title": "Both Teams To Score",
                "yes_sub_title": "Yes",
                "rules_primary": REGULATION,
            }
        ],
    }


def _matchbook_btts_market(market_id: int) -> dict[str, Any]:
    return {
        "id": market_id,
        "name": "Both Teams To Score",
        "runners": [
            {
                "id": 11,
                "name": "Yes",
                "prices": [{"side": "back", "odds": "1.90", "available-amount": "80"}],
            },
            {
                "id": 12,
                "name": "No",
                "prices": [{"side": "back", "odds": "1.90", "available-amount": "80"}],
            },
        ],
    }


@pytest.mark.asyncio
async def test_attempt_live_without_matchbook_credentials_does_not_fabricate() -> None:
    bundle = scenario3_safe_90m_bundle()
    kalshi = OverlapKalshi(
        [deepcopy(bundle.kalshi.event or {})],
        series=deepcopy(bundle.kalshi.series or {}),
        books=deepcopy(bundle.kalshi.order_books),
    )
    report = await attempt_live_read_only_capture(
        settings=Settings(
            kalshi_event_page_limit=1,
            kalshi_event_max_pages=1,
            matchbook_username=None,
            matchbook_password=None,
        ),
        kalshi_client=kalshi,
    )
    assert report.paper_mode == "paper"
    assert report.execution_enabled is False
    assert report.same_event_overlap_found is False
    assert report.matched_equivalent is None
    assert report.matchbook.credentials_present is False
    assert report.matchbook.events_listed == 0
    assert "MATCHBOOK_USERNAME" in (report.matchbook.unavailable_reason or "")
    assert report.replay_bundle_path is None
    assert report.kalshi.reachable is True
    assert report.kalshi.events_listed == 1
    assert kalshi.book_calls == []


@pytest.mark.asyncio
async def test_both_venues_reachable_without_identity_overlap_is_reported_truthfully() -> None:
    chelsea = _chelsea_event()
    bundle = scenario3_safe_90m_bundle()
    kalshi_event = deepcopy(bundle.kalshi.event or {})
    # Bayern/Union captured shape rewritten to Monza is still a different match
    # than Chelsea vs Hull. Titles do not match either.
    matchbook = OverlapMatchbook([chelsea], {str(chelsea["id"]): list(chelsea.get("markets") or [])})
    kalshi = OverlapKalshi(
        [kalshi_event],
        series=deepcopy(bundle.kalshi.series or {}),
        books=deepcopy(bundle.kalshi.order_books),
    )
    report = await attempt_live_read_only_capture(
        matchbook_client=matchbook,
        kalshi_client=kalshi,
        fx_snapshots=_fx(),
        venue_costs=replay_venue_costs(bundle),
    )
    assert report.same_event_overlap_found is False
    assert report.overlap_count == 0
    assert report.matched_equivalent is None
    assert report.block_reason == "no_same_event_overlap"
    assert report.replay_bundle_path is None
    assert matchbook.list_markets_calls == []
    assert kalshi.book_calls == []
    assert any("production EventMatcher" in note for note in report.notes)


@pytest.mark.asyncio
async def test_title_equality_is_not_used_for_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    bundle = scenario3_safe_90m_bundle()
    mb_event = deepcopy(bundle.matchbook.event or {})
    kalshi_event = deepcopy(bundle.kalshi.event or {})
    kalshi_event["milestone"] = {"start_date": "2026-09-21T18:30:00Z"}
    matchbook = OverlapMatchbook(
        [mb_event], {str(mb_event["id"]): deepcopy(bundle.matchbook.markets)}
    )
    kalshi = OverlapKalshi(
        [kalshi_event],
        series=deepcopy(bundle.kalshi.series or {}),
        books=deepcopy(bundle.kalshi.order_books),
    )
    original_could = EventMatcher.could_match
    prefilter_calls = {"n": 0}

    def _counting_could(self: EventMatcher, left: Any, right: Any, **kwargs: Any) -> Any:
        prefilter_calls["n"] += 1
        return original_could(self, left, right, **kwargs)

    monkeypatch.setattr(EventMatcher, "could_match", _counting_could)
    report = await attempt_live_read_only_capture(
        matchbook_client=matchbook,
        kalshi_client=kalshi,
        fx_snapshots=_fx(),
        venue_costs=replay_venue_costs(bundle),
    )
    assert prefilter_calls["n"] > 0
    assert report.same_event_overlap_found is False
    assert report.block_reason == "no_same_event_overlap"
    assert matchbook.list_markets_calls == []


@pytest.mark.asyncio
async def test_genuine_overlap_runs_production_gate_and_saves_replay_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    matchbook, kalshi, bundle = _scenario3_clients()
    extra_mb = _chelsea_event()
    matchbook.events.append(extra_mb)
    matchbook.markets_by_id[str(extra_mb["id"])] = list(extra_mb.get("markets") or [])
    original = EventMatcher.match
    matcher_calls = {"n": 0}

    def _counting_match(self: EventMatcher, left: Any, right: Any, **kwargs: Any) -> Any:
        matcher_calls["n"] += 1
        return original(self, left, right, **kwargs)

    monkeypatch.setattr(EventMatcher, "match", _counting_match)
    bundle_path = tmp_path / "owner-live-capture.replay-bundle.json"
    report = await attempt_live_read_only_capture(
        matchbook_client=matchbook,
        kalshi_client=kalshi,
        fx_snapshots=_fx(),
        venue_costs=replay_venue_costs(bundle),
        bundle_out=bundle_path,
    )
    assert matcher_calls["n"] > 0
    assert report.data_class == DATA_CLASS_LIVE_CAPTURE
    assert report.paper_mode == "paper"
    assert report.execution_enabled is False
    assert report.polymarket_included is False
    assert report.same_event_overlap_found is True
    assert report.overlap_count == 1
    assert report.overlap_fixture is not None
    assert "Monza" in report.overlap_fixture
    assert "Sassuolo" in report.overlap_fixture
    assert report.matched_equivalent is True
    assert report.approved_family_on_both is True
    assert report.comparison_economics_computed is True
    assert report.arb is False
    assert report.block_reason is None
    assert report.replay_bundle_path == str(bundle_path)
    assert bundle_path.is_file()
    assert matchbook.list_markets_calls == ["28901"]
    assert extra_mb["id"] not in {int(item) for item in matchbook.list_markets_calls}
    assert kalshi.book_calls
    saved = ReplayBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    assert saved.data_class == DATA_CLASS_LIVE_CAPTURE
    assert saved.paper_mode == "paper"
    assert saved.execution_enabled is False
    dumped = bundle_path.read_text(encoding="utf-8").casefold()
    assert "password" not in dumped
    assert "authorization" not in dumped
    replay_report, summary = await replay_bundle(saved)
    clustered = [
        item
        for item in replay_report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    ]
    assert len(clustered) == 1
    assert summary.matched_equivalent is True
    assert summary.network_used is False
    assert summary.arb is False
    rows = replay_report.fixture_markets[clustered[0].canonical_event_id]
    assert any(
        row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
        and row.entered_solver
        for row in rows
    )


@pytest.mark.asyncio
async def test_cluster_helper_rejects_title_only_pairs() -> None:
    bundle = scenario3_safe_90m_bundle()
    mb_event = deepcopy(bundle.matchbook.event or {})
    kalshi_event = deepcopy(bundle.kalshi.event or {})
    kalshi_event["milestone"] = {"start_date": "2026-09-21T18:30:00Z"}
    clusters = cluster_live_matchbook_kalshi_events([mb_event], [kalshi_event])
    overlaps = [
        cluster
        for cluster in clusters
        if cluster.matchbook is not None and cluster.kalshi is not None
    ]
    assert overlaps == []


def test_paper_write_boundary_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert_no_write_methods()
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)


def test_sanitize_still_strips_live_payload_secrets() -> None:
    cleaned = sanitize_payload(
        {
            "id": 1,
            "name": "Monza vs Sassuolo",
            "session-token": "keep-out",
            "password": "keep-out",
            "markets": [{"id": 2, "token": "nope", "ticker": "KXSERIEAGAME-SAFE"}],
        }
    )
    dumped = str(cleaned)
    assert "keep-out" not in dumped
    assert "token" not in cleaned["markets"][0]
    assert cleaned["markets"][0]["ticker"] == "KXSERIEAGAME-SAFE"


def test_capture_attempt_report_keeps_overlap_fields() -> None:
    report = CaptureAttemptReport(
        captured_at=datetime.now(UTC),
        identified_as="test",
        matchbook={
            "venue": "matchbook",
            "credentials_present": True,
            "reachable": True,
            "events_listed": 1,
            "provenance": "live_read_only_capture",
            "data_class": "live_read_only_capture",
        },
        kalshi={
            "venue": "kalshi",
            "credentials_present": False,
            "reachable": True,
            "events_listed": 1,
            "provenance": "live_read_only_capture",
            "data_class": "live_read_only_capture",
        },
        same_event_overlap_found=True,
        overlap_count=1,
        overlap_fixture="Monza vs Sassuolo",
        matched_equivalent=True,
        comparison_economics_computed=True,
        arb=False,
        replay_bundle_path="/tmp/example.replay-bundle.json",
    )
    dumped = report.model_dump(mode="json")
    assert dumped["same_event_overlap_found"] is True
    assert dumped["replay_bundle_path"].endswith("replay-bundle.json")


def test_catalogue_relevant_series_is_not_game_only() -> None:
    series = catalogue_relevant_kalshi_series()
    game_only = {"KXEPLGAME", "KXBUNDESLIGAGAME", "KXSERIEAGAME", "KXLALIGAGAME"}
    assert "KXEPLGAME" in series
    assert "KXEPLBTTS" in series
    assert "KXEPLTOTAL" in series
    assert "KXSERIEABTTS" in series
    assert "KXLALIGABTTS" in series
    assert "KXEFLCHAMPIONSHIPBTTS" in series
    assert not set(series) <= game_only
    assert game_only < set(series)
    nfl_only = Settings(kalshi_series_tickers=["KXNFLGAME", "KXEPLBTTS"])
    bounded = catalogue_relevant_kalshi_series(nfl_only)
    assert bounded == ["KXEPLBTTS"]


@pytest.mark.asyncio
async def test_game_blocked_same_fixture_btts_is_selected_and_captured(
    tmp_path: Path,
) -> None:
    """GAMEWIN 1X2 is unapproved; BTTS on the same fixture must be captured."""

    bundle = scenario3_safe_90m_bundle()
    mb_event = deepcopy(bundle.matchbook.event or {})
    mb_markets = deepcopy(bundle.matchbook.markets) + [_matchbook_btts_market(28920)]
    kickoff = str(mb_event.get("start") or "")
    title = "AC Monza vs Sassuolo Calcio"
    game_event = _gamewin_blocked_kalshi_event(
        ticker="KXSERIEAGAME-MONSAS",
        series_ticker="KXSERIEAGAME",
        title=title,
        home="AC Monza",
        away="Sassuolo Calcio",
        kickoff=kickoff,
        competition="Serie A",
    )
    btts_event = _approved_btts_kalshi_event(
        ticker="KXSERIEABTTS-MONSAS",
        series_ticker="KXSERIEABTTS",
        title=title,
        kickoff=kickoff,
        competition="Serie A",
    )
    chelsea = _chelsea_event()
    chelsea_kickoff = str(chelsea.get("start") or "")
    chelsea_game = _gamewin_blocked_kalshi_event(
        ticker="KXEPLGAME-CHELHUL",
        series_ticker="KXEPLGAME",
        title="Chelsea FC vs Hull City AFC",
        home="Chelsea FC",
        away="Hull City AFC",
        kickoff=chelsea_kickoff,
        competition="Premier League",
    )
    matchbook = OverlapMatchbook(
        [mb_event, chelsea],
        {
            str(mb_event["id"]): mb_markets,
            str(chelsea["id"]): list(chelsea.get("markets") or []),
        },
    )
    kalshi = OverlapKalshi(
        [chelsea_game, game_event, btts_event],
        series=_gamewin_series("KXSERIEAGAME", "Serie A"),
        series_by_ticker={
            "KXSERIEAGAME": _gamewin_series("KXSERIEAGAME", "Serie A"),
            "KXSERIEABTTS": _btts_series("KXSERIEABTTS", "Serie A"),
            "KXEPLGAME": _gamewin_series("KXEPLGAME", "Premier League"),
        },
        books={
            **_kalshi_books_for(game_event),
            **_kalshi_books_for(btts_event),
            **_kalshi_books_for(chelsea_game),
        },
    )
    bundle_path = tmp_path / "owner-live-btts.replay-bundle.json"
    report = await attempt_live_read_only_capture(
        matchbook_client=matchbook,
        kalshi_client=kalshi,
        fx_snapshots=_fx(),
        venue_costs=replay_venue_costs(bundle),
        bundle_out=bundle_path,
    )
    requested = kalshi.list_events_filters[0].get("series_tickers") or []
    assert "KXSERIEABTTS" in requested
    assert "KXEPLBTTS" in requested
    assert "KXEPLTOTAL" in requested
    assert set(requested) != {"KXEPLGAME", "KXBUNDESLIGAGAME", "KXSERIEAGAME", "KXLALIGAGAME"}
    assert report.same_event_overlap_found is True
    assert report.paper_mode == "paper"
    assert report.execution_enabled is False
    assert "Monza" in (report.overlap_fixture or "")
    assert "Sassuolo" in (report.overlap_fixture or "")
    assert report.matched_equivalent is True
    assert report.approved_family_on_both is True
    assert report.canonical_market_key is not None
    assert "both_teams_to_score" in report.canonical_market_key
    assert report.comparison_economics_computed is True
    assert report.replay_bundle_path == str(bundle_path)
    assert str(chelsea["id"]) not in matchbook.list_markets_calls
    assert any("BTTS" in ticker for ticker in kalshi.book_calls)
    saved = ReplayBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    assert saved.kalshi.event is not None
    assert saved.kalshi.event.get("series_ticker") == "KXSERIEABTTS"
    assert any(
        "BTTS" in str(market.get("ticker") or "")
        for market in saved.kalshi.markets
    )
    dumped = bundle_path.read_text(encoding="utf-8").casefold()
    assert "password" not in dumped
    replay_report, summary = await replay_bundle(saved)
    assert summary.matched_equivalent is True
    assert summary.network_used is False
    clustered = [
        item
        for item in replay_report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    ]
    assert clustered
    rows = replay_report.fixture_markets[clustered[0].canonical_event_id]
    assert any(
        row.family == "both_teams_to_score"
        and row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
        and row.entered_solver
        for row in rows
    )

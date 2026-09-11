from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.market_intelligence import get_market_intelligence_service
from sports_hedge.api.paper import get_paper_audit_repository
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.persistence.paper import SqlitePaperScanRepository


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def test_paper_pair_scan_api_is_research_only_and_returns_auditable_decision() -> None:
    repository = SqliteMarketIntelligenceRepository()
    audit = SqlitePaperScanRepository()
    watchlist_store = SqliteWatchlistRepository()
    intelligence = MarketIntelligenceService(repository)
    app.dependency_overrides[get_market_intelligence_service] = lambda: intelligence
    app.dependency_overrides[get_paper_audit_repository] = lambda: audit
    app.dependency_overrides[get_watchlist_service] = lambda: WatchlistService(watchlist_store)
    client = TestClient(app)

    payload = {
        "left": {
            "venue": "matchbook",
            "event_payload": {
                "id": 1001,
                "name": "Newcastle United vs Chelsea",
                "start": KICKOFF.isoformat(),
                "competition-name": "Premier League",
            },
            "market_payload": {
                "id": 2001,
                "name": "Both Teams To Score",
                "runners": [
                    {
                        "id": 301,
                        "name": "Yes",
                        "prices": [
                            {"side": "back", "odds": "2.20", "available-amount": "100"},
                            {"side": "lay", "odds": "2.22", "available-amount": "100"},
                        ],
                    },
                    {
                        "id": 302,
                        "name": "No",
                        "prices": [
                            {"side": "back", "odds": "1.80", "available-amount": "100"},
                            {"side": "lay", "odds": "1.82", "available-amount": "100"},
                        ],
                    },
                ],
            },
            "observed_at": OBSERVED.isoformat(),
            "native_currency": "GBP",
            "quote_age_ms": 100,
        },
        "right": {
            "venue": "polymarket",
            "event_payload": {
                "id": "pm-event-1",
                "title": "Newcastle United vs Chelsea",
                "startDate": KICKOFF.isoformat(),
                "competition": "Premier League",
            },
            "market_payload": {
                "id": "pm-market-1",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "description": "Resolves based on 90 minutes of regulation time.",
            },
            "books_by_token": {
                "yes-token": {
                    "bids": [{"price": "0.49", "size": "250"}],
                    "asks": [{"price": "0.51", "size": "250"}],
                },
                "no-token": {
                    "bids": [{"price": "0.41", "size": "300"}],
                    "asks": [{"price": "0.43", "size": "300"}],
                },
            },
            "observed_at": OBSERVED.isoformat(),
            "quote_age_ms": 150,
        },
        "fee_snapshots": [
            {"venue": "matchbook", "profit_haircut_rate": "0.02", "source": "test"},
            {"venue": "polymarket", "profit_haircut_rate": "0", "source": "test"},
        ],
        "fx_snapshots": [
            {"currency": "USD", "gbp_per_unit": "0.75", "source": "test"}
        ],
        "capital_limit_gbp": "100",
        "maximum_execution_risk": 100,
    }

    try:
        response = client.post("/paper/scan/pair", json=payload)
        assert response.status_code == 200
        body = response.json()
        assert body["market_match"]["matched"] is True
        assert body["depth_scan"]["solution"]["is_arbitrage"] is True
        assert body["eligible_for_paper_simulation"] is True
        assert body["snapshots_recorded"] == 4

        scans = client.get("/paper/scans")
        assert scans.status_code == 200
        scan_rows = scans.json()
        assert len(scan_rows) == 1
        assert scan_rows[0]["home_team"] == "Newcastle United"
        assert scan_rows[0]["away_team"] == "Chelsea"
        assert scan_rows[0]["eligible_for_paper_simulation"] is True
        assert float(scan_rows[0]["net_edge"]) > 0
        assert float(scan_rows[0]["guaranteed_profit_gbp"]) > 0
        assert set(scan_rows[0]["venues"]) == {"matchbook", "polymarket"}

        watchlist = client.get("/paper/watchlist/triggered")
        assert watchlist.status_code == 200
        assert watchlist.json()[0]["status"] == "TRIGGERED"

        summary = client.get("/paper/scans/summary", params={"since": "2020-01-01T00:00:00Z"})
        assert summary.status_code == 200
        summary_payload = summary.json()
        assert summary_payload["scan_count"] == 1
        assert summary_payload["arbitrage_count"] == 1
        assert summary_payload["eligible_count"] == 1
        assert float(summary_payload["top_net_edge"]) > 0

        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["execution_enabled"] is False
    finally:
        app.dependency_overrides.clear()
        audit.close()
        watchlist_store.close()
        repository.close()

"""Named Manchester derby live logical acceptance (2026-09-13 16:30 UK).

Uses the normal read-only venue-union collector, event matcher and pairwise
solver. Live provider payloads are never replaced with demo fixtures.
Matchbook is attempted only when credentials exist; otherwise the public
Polymarket↔Kalshi path is still proven and Matchbook is reported as an
owner-Windows smoke-test remainder.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sports_hedge.application.collector import CollectionReport, ReadOnlyCrossVenueCollector
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher
from sports_hedge.normalization.text import normalize_text
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
)
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient

DERBY_DATE = date(2026, 9, 13)
DERBY_KICKOFF_UTC = datetime(2026, 9, 13, 15, 30, tzinfo=UTC)
DERBY_KICKOFF_UK = "16:30 UK / 15:30 UTC"
HOME_TEAM = "Manchester United"
AWAY_TEAM = "Manchester City"
LIVE_LOGICAL_ENV = "SPORTS_HEDGE_LIVE_LOGICAL"


class _FailingListEvents:
    """Preserve the live Matchbook failure so collector health stays unavailable."""

    def __init__(self, inner: Any, detail: str) -> None:
        self._inner = inner
        self._detail = detail

    async def list_events(self, **filters: Any) -> Any:
        del filters
        raise RuntimeError(self._detail)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


class _CachedListEvents:
    """Replay an already-fetched live list_events payload; other calls hit the client."""

    def __init__(self, inner: Any, payload: Any) -> None:
        self._inner = inner
        self._payload = payload

    async def list_events(self, **filters: Any) -> Any:
        del filters
        return self._payload

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def is_named_derby_title(title: str) -> bool:
    text = normalize_text(title)
    united = any(token in text for token in ("manchester united", "man utd", "man united"))
    city = any(token in text for token in ("manchester city", "man city"))
    return united and city


def is_named_derby_payload(payload: dict[str, Any]) -> bool:
    if not is_named_derby_title(_payload_title(payload)):
        return False
    dates = _payload_calendar_dates(payload)
    if dates and DERBY_DATE not in dates:
        return False
    return True


def _payload_title(payload: dict[str, Any]) -> str:
    return str(
        payload.get("title")
        or payload.get("name")
        or payload.get("question")
        or payload.get("sub_title")
        or ""
    )


def _payload_calendar_dates(payload: dict[str, Any]) -> set[date]:
    found: set[date] = set()
    blob = " ".join(
        str(payload.get(key) or "")
        for key in (
            "event_ticker",
            "ticker",
            "startTime",
            "startDate",
            "start",
            "start-time",
            "start_time",
            "start_date",
            "strike_date",
            "occurrence_datetime",
            "expected_expiration_time",
            "title",
            "name",
        )
    )
    if "26SEP13" in blob.upper() or "2026-09-13" in blob:
        found.add(DERBY_DATE)
    milestone = payload.get("milestone")
    if isinstance(milestone, dict):
        start = str(milestone.get("start_date") or "")
        if "2026-09-13" in start:
            found.add(DERBY_DATE)
    for market in payload.get("markets") or []:
        if not isinstance(market, dict):
            continue
        nested = " ".join(
            str(market.get(key) or "")
            for key in ("occurrence_datetime", "expected_expiration_time", "ticker", "title")
        )
        if "26SEP13" in nested.upper() or "2026-09-13" in nested:
            found.add(DERBY_DATE)
    return found


def _extract_events(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        items = payload.get("events")
        if isinstance(items, list):
            return [item for item in items if isinstance(item, dict)]
    return []


def _filter_payload(payload: Any, *, as_list: bool) -> Any:
    events = [item for item in _extract_events(payload) if is_named_derby_payload(item)]
    if as_list:
        return events
    if isinstance(payload, dict):
        return {**payload, "events": events, "total": len(events)}
    return {"events": events, "total": len(events)}


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (list, dict)):
        try:
            return value.value
        except Exception:
            return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return str(value)


def _summarize_event(payload: dict[str, Any], venue: VenueName) -> dict[str, Any]:
    normalizer = {
        VenueName.MATCHBOOK: MatchbookNormalizer(),
        VenueName.POLYMARKET: PolymarketNormalizer(),
        VenueName.KALSHI: KalshiNormalizer(),
    }[venue]
    summary: dict[str, Any] = {
        "source_id": str(
            payload.get("id")
            or payload.get("event_ticker")
            or payload.get("ticker")
            or ""
        ),
        "title": _payload_title(payload),
        "normalized": False,
        "home_team": None,
        "away_team": None,
        "kickoff_utc": None,
        "competition": None,
        "normalize_error": None,
    }
    try:
        canonical = normalizer.normalize_event(payload)
    except (VenueNormalizationError, ValueError) as exc:
        summary["normalize_error"] = str(exc)
        return summary
    summary.update(
        {
            "normalized": True,
            "home_team": canonical.home_team,
            "away_team": canonical.away_team,
            "kickoff_utc": canonical.kickoff_utc.isoformat(),
            "competition": canonical.competition,
        }
    )
    return summary


def _pairwise_identity(
    left_events: list[dict[str, Any]],
    right_events: list[dict[str, Any]],
    *,
    left_venue: VenueName,
    right_venue: VenueName,
) -> list[dict[str, Any]]:
    matcher = EventMatcher()
    left_norm = [
        (item, _summarize_event(item, left_venue))
        for item in left_events
    ]
    right_norm = [
        (item, _summarize_event(item, right_venue))
        for item in right_events
    ]
    rows: list[dict[str, Any]] = []
    left_normalizer = {
        VenueName.MATCHBOOK: MatchbookNormalizer(),
        VenueName.POLYMARKET: PolymarketNormalizer(),
        VenueName.KALSHI: KalshiNormalizer(),
    }[left_venue]
    right_normalizer = {
        VenueName.MATCHBOOK: MatchbookNormalizer(),
        VenueName.POLYMARKET: PolymarketNormalizer(),
        VenueName.KALSHI: KalshiNormalizer(),
    }[right_venue]
    for left_payload, left_summary in left_norm:
        for right_payload, right_summary in right_norm:
            if not left_summary["normalized"] or not right_summary["normalized"]:
                rows.append(
                    {
                        "left_id": left_summary["source_id"],
                        "right_id": right_summary["source_id"],
                        "matched": False,
                        "confidence": 0.0,
                        "reasons": [
                            left_summary.get("normalize_error")
                            or right_summary.get("normalize_error")
                            or "normalize_failed"
                        ],
                    }
                )
                continue
            match = matcher.match(
                left_normalizer.normalize_event(left_payload),
                right_normalizer.normalize_event(right_payload),
            )
            rows.append(
                {
                    "left_id": left_summary["source_id"],
                    "right_id": right_summary["source_id"],
                    "left_title": left_summary["title"],
                    "right_title": right_summary["title"],
                    "left_kickoff_utc": left_summary["kickoff_utc"],
                    "right_kickoff_utc": right_summary["kickoff_utc"],
                    "matched": match.matched,
                    "confidence": match.confidence,
                    "reasons": list(match.reasons),
                }
            )
    return rows


def _pair_key(left: VenueName, right: VenueName) -> str:
    return f"{left.value}_{right.value}"


def _decision_rows(report: CollectionReport) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for decision in report.paper_decisions:
        venues = [venue.value for venue in decision.execution_modes]
        if len(venues) < 2 and decision.fill_legs:
            venues = [leg.venue.value for leg in decision.fill_legs]
        pair = "unknown"
        if len(venues) >= 2:
            ordered = tuple(sorted(venues))
            pair = {
                ("matchbook", "polymarket"): "matchbook_polymarket",
                ("kalshi", "matchbook"): "matchbook_kalshi",
                ("kalshi", "polymarket"): "polymarket_kalshi",
            }.get(ordered, "_".join(ordered))
        costs = [
            {
                "venue": snapshot.venue.value if hasattr(snapshot.venue, "value") else str(snapshot.venue),
                "fee_basis": str(snapshot.fee_basis),
                "source": snapshot.source,
                "known_status": str(snapshot.known_status),
            }
            for snapshot in decision.venue_costs
        ]
        current = None
        is_arb = False
        if decision.payoff_scan is not None:
            current = decision.payoff_scan.solution.roi
            is_arb = bool(decision.payoff_scan.solution.is_arbitrage)
        elif decision.depth_scan is not None:
            implied = decision.depth_scan.solution.implied_probability_sum
            if implied > 0:
                current = Decimal("1") - implied
            is_arb = bool(decision.depth_scan.solution.is_arbitrage)
        trigger = decision.minimum_net_edge
        distance = None
        if current is not None:
            distance = (current - trigger) * Decimal("100")
        rows.append(
            {
                "pair": pair,
                "solver_model": decision.solver_model,
                "entered_solver": bool(decision.solver_model),
                "eligible_for_paper_open": bool(decision.eligible_for_paper_simulation),
                "solver_is_arbitrage": is_arb,
                "current_net_edge": current,
                "trigger_net_edge": trigger,
                "edge_vs_trigger_pp": distance,
                "rejection_reasons": list(decision.rejection_reasons),
                "venue_costs": costs,
                "fx_sources": [snapshot.source for snapshot in decision.fx_snapshots],
                "execution_enabled_claimed": False,
            }
        )
    return rows


def render_manchester_derby_report(report: dict[str, Any]) -> str:
    venues = report.get("venues", {})
    identity = report.get("canonical_identity", {})
    lines = [
        "# Manchester derby live logical test — 2026-09-13 16:30 UK",
        "",
        f"Evaluated at: {report.get('evaluated_at')}",
        f"Head SHA: {report.get('head_sha') or 'unspecified'}",
        f"Data class: {report.get('data_class')}",
        f"Paper mode: {report.get('paper_mode')} · execution_enabled={report.get('execution_enabled')}",
        f"FX used: {report.get('fx_provenance')}",
        "",
        "## Per-venue discovery",
    ]
    for venue in ("matchbook", "polymarket", "kalshi"):
        row = venues.get(venue, {})
        lines.append(
            f"- **{venue}**: reachable={row.get('reachable')} discovered={row.get('discovered')} "
            f"raw={row.get('raw_events')} derby_events={row.get('derby_events')} "
            f"normalized_markets={row.get('normalized_markets')} "
            f"detail={row.get('detail')}"
        )
    lines.extend(
        [
            "",
            "## Canonical identity",
            f"- Matched across discovered venues: {identity.get('matched')}",
            f"- Reason: {identity.get('reason')}",
            f"- PM↔K matched event pairs: {identity.get('polymarket_kalshi_matched_pairs')}",
            f"- MB↔PM matched event pairs: {identity.get('matchbook_polymarket_matched_pairs')}",
            f"- MB↔K matched event pairs: {identity.get('matchbook_kalshi_matched_pairs')}",
            "",
            "## Equivalent markets and solver",
            f"- Proven equivalent families: {report.get('equivalent_families')}",
            f"- Equivalent market count: {report.get('equivalent_market_count')}",
            f"- Best net edge: {report.get('best_net_edge')}",
            f"- Trigger: {report.get('trigger_net_edge')}",
            f"- Edge vs trigger (pp): {report.get('edge_vs_trigger_pp')}",
            f"- Qualifying opportunities: {report.get('qualifying_count')}",
            f"- Near opportunities: {report.get('near_count')}",
            f"- OPEN paper allowed: {report.get('open_paper_allowed')}",
            "",
            "Market-level PM↔K family reasons:",
        ]
    )
    family_reasons = report.get("family_reasons") or []
    notable = [item for item in family_reasons if item.get("family")]
    if notable:
        for item in notable:
            lines.append(
                f"- **{item.get('family')}**"
                f"{'' if item.get('line') in (None, '') else ' line=' + str(item.get('line'))}: "
                f"status={item.get('comparison_status')} "
                f"entered_solver={item.get('entered_solver')} "
                f"reason={item.get('reason')}"
            )
        skipped = len(family_reasons) - len(notable)
        if skipped:
            lines.append(f"- {skipped} additional unsupported/unnormalized markets (see JSON)")
    else:
        lines.append("- none recorded")
    lines.extend(
        [
            "",
            "Pairwise solver paths:",
        ]
    )
    for pair, row in (report.get("pairwise") or {}).items():
        lines.append(
            f"- **{pair}**: events_matched={row.get('events_matched')} "
            f"solver_executed={row.get('solver_executed')} "
            f"equivalent={row.get('equivalent_count')} "
            f"best_net_edge={row.get('best_net_edge')} "
            f"reason={row.get('reason')}"
        )
    limitations = report.get("provider_limitations") or []
    if limitations:
        lines.extend(["", "## Provider / matching limitations"])
        lines.extend(f"- {item}" for item in limitations)
    lines.extend(["", "Matchbook owner-Windows remainder:", report.get("matchbook_owner_requirement", "")])
    return "\n".join(lines) + "\n"


async def run_manchester_derby_logical(
    *,
    settings: Settings | None = None,
    head_sha: str | None = None,
) -> dict[str, Any]:
    settings = settings or get_settings()
    evaluated_at = datetime.now(UTC)
    matchbook = MatchbookClient(settings)
    polymarket = PolymarketClient(settings)
    kalshi = KalshiClient(settings)
    intelligence_store = SqliteMarketIntelligenceRepository()
    fx_store = SqliteFxRateRepository()
    liquidity = SqlitePaperLiquidityRepository()
    try:
        raw_matchbook, mb_error = await _safe_list(matchbook)
        raw_polymarket, pm_error = await _safe_list(polymarket)
        raw_kalshi, k_error = await _safe_list(kalshi)

        mb_events = _extract_events(raw_matchbook)
        pm_events = _extract_events(raw_polymarket)
        k_events = _extract_events(raw_kalshi)
        mb_derby = [item for item in mb_events if is_named_derby_payload(item)]
        pm_derby = [item for item in pm_events if is_named_derby_payload(item)]
        k_derby = [item for item in k_events if is_named_derby_payload(item)]

        credentials = bool(settings.matchbook_username and settings.matchbook_password)
        fx_snapshots = [
            FxRateSnapshot(
                currency="USD",
                gbp_per_unit=Decimal(str(settings.paper_treasury_demo_usd_gbp_per_unit)),
                source=settings.paper_treasury_demo_fx_source,
                captured_at=evaluated_at,
            )
        ]
        paper_scan = PaperScanService(
            MarketIntelligenceService(intelligence_store),
            fx_service=FxRateService(fx_store),
            cost_resolver=VenueCostResolver(),
            liquidity=liquidity,
        )
        matchbook_adapter: Any
        if mb_error:
            matchbook_adapter = _FailingListEvents(matchbook, mb_error)
        else:
            matchbook_adapter = _CachedListEvents(
                matchbook, _filter_payload(raw_matchbook, as_list=False)
            )
        collector = ReadOnlyCrossVenueCollector(
            matchbook=matchbook_adapter,
            polymarket=_CachedListEvents(polymarket, _filter_payload(raw_polymarket, as_list=True)),
            kalshi=_CachedListEvents(kalshi, _filter_payload(raw_kalshi, as_list=False)),
            paper_scan=paper_scan,
        )
        collection = await collector.collect_and_scan(
            fx_snapshots=fx_snapshots,
            minimum_net_edge=Decimal(str(settings.min_net_edge)),
            maximum_execution_risk=settings.max_execution_risk,
            minimum_mapping_confidence=settings.min_mapping_confidence,
            assumed_latency_ms=settings.simulated_latency_ms,
            max_event_pairs=100,
            max_market_pairs_per_event=50,
            polymarket_queried_series_ids=settings.resolved_polymarket_series_ids(),
            config_warnings=settings.polymarket_series_config_warnings(),
        )

        identity_pairs = {
            "matchbook_polymarket": _pairwise_identity(
                mb_derby, pm_derby, left_venue=VenueName.MATCHBOOK, right_venue=VenueName.POLYMARKET
            ),
            "matchbook_kalshi": _pairwise_identity(
                mb_derby, k_derby, left_venue=VenueName.MATCHBOOK, right_venue=VenueName.KALSHI
            ),
            "polymarket_kalshi": _pairwise_identity(
                pm_derby, k_derby, left_venue=VenueName.POLYMARKET, right_venue=VenueName.KALSHI
            ),
        }
        decisions = _decision_rows(collection)
        pairwise = _pairwise_solver_summary(identity_pairs, decisions, collection)
        families = sorted(
            {
                row.family
                for rows in collection.fixture_markets.values()
                for row in rows
                if row.family and row.comparison_status.value == "matched_equivalent"
            }
        )
        fixture_edges = [
            item.current_net_edge
            for item in collection.discovered_fixtures
            if item.current_net_edge is not None
        ]
        best_edge = max(fixture_edges) if fixture_edges else None
        trigger = Decimal(str(settings.min_net_edge))
        qualifying = sum(
            1
            for item in collection.discovered_fixtures
            if item.solver_is_arbitrage
        )
        near = sum(
            1
            for item in collection.discovered_fixtures
            if item.opportunity_state == "near"
        )
        inventory_near = sum(
            1
            for rows in collection.fixture_markets.values()
            for row in rows
            if row.current_net_edge is not None
            and row.trigger_net_edge is not None
            and not row.solver_is_arbitrage
            and row.current_net_edge < row.trigger_net_edge
        )
        open_allowed = any(item["eligible_for_paper_open"] for item in decisions)
        limitations = _limitations(
            credentials=credentials,
            mb_error=mb_error,
            pm_error=pm_error,
            k_error=k_error,
            identity_pairs=identity_pairs,
            pm_derby=pm_derby,
            k_derby=k_derby,
            collection=collection,
        )
        pm_k_matched = sum(1 for row in identity_pairs["polymarket_kalshi"] if row["matched"])
        identity_reason = _identity_reason(
            credentials=credentials,
            mb_error=mb_error,
            pm_derby=pm_derby,
            k_derby=k_derby,
            identity_pairs=identity_pairs,
        )
        report = {
            "named_fixture": f"{HOME_TEAM} v {AWAY_TEAM}",
            "scheduled": DERBY_KICKOFF_UK,
            "derby_date": DERBY_DATE.isoformat(),
            "evaluated_at": evaluated_at.isoformat(),
            "head_sha": head_sha,
            "data_class": "live_provider_read_only",
            "paper_mode": settings.sports_hedge_mode,
            "execution_enabled": settings.sports_hedge_execution_enabled,
            "fx_provenance": (
                f"modelled/seed {settings.paper_treasury_demo_fx_source} "
                f"{settings.paper_treasury_demo_usd_gbp_per_unit} GBP/USD"
            ),
            "venues": {
                "matchbook": {
                    "reachable": mb_error is None,
                    "discovered": bool(mb_derby),
                    "raw_events": len(mb_events),
                    "derby_events": len(mb_derby),
                    "normalized_markets": collection.normalized_matchbook_markets,
                    "health": collection.venue_health.get("matchbook"),
                    "detail": mb_error
                    or (
                        "MATCHBOOK_USERNAME/PASSWORD unset; owner-Windows smoke-test required"
                        if not credentials
                        else f"{len(mb_derby)} named derby events"
                    ),
                    "events": [_summarize_event(item, VenueName.MATCHBOOK) for item in mb_derby],
                },
                "polymarket": {
                    "reachable": pm_error is None,
                    "discovered": bool(pm_derby),
                    "raw_events": len(pm_events),
                    "derby_events": len(pm_derby),
                    "normalized_markets": collection.normalized_polymarket_markets,
                    "health": collection.venue_health.get("polymarket"),
                    "detail": pm_error or f"{len(pm_derby)} named derby events",
                    "events": [_summarize_event(item, VenueName.POLYMARKET) for item in pm_derby],
                },
                "kalshi": {
                    "reachable": k_error is None,
                    "discovered": bool(k_derby),
                    "raw_events": len(k_events),
                    "derby_events": len(k_derby),
                    "normalized_markets": collection.normalized_kalshi_markets,
                    "health": collection.venue_health.get("kalshi"),
                    "detail": k_error or f"{len(k_derby)} named derby events",
                    "events": [_summarize_event(item, VenueName.KALSHI) for item in k_derby],
                },
            },
            "canonical_identity": {
                "matched": pm_k_matched > 0 or any(
                    item.matchbook_matched and item.polymarket_matched and item.kalshi_matched
                    for item in collection.discovered_fixtures
                ),
                "reason": identity_reason,
                "polymarket_kalshi_matched_pairs": pm_k_matched,
                "matchbook_polymarket_matched_pairs": sum(
                    1 for row in identity_pairs["matchbook_polymarket"] if row["matched"]
                ),
                "matchbook_kalshi_matched_pairs": sum(
                    1 for row in identity_pairs["matchbook_kalshi"] if row["matched"]
                ),
                "event_match_attempts": _jsonable(identity_pairs),
            },
            "collector_summary": collection.operator_summary,
            "pair_counts": collection.pair_counts,
            "equivalent_families": families,
            "equivalent_market_count": sum(
                item.matched_equivalent_count for item in collection.discovered_fixtures
            ),
            "family_reasons": _family_reasons(collection),
            "best_net_edge": best_edge,
            "trigger_net_edge": trigger,
            "edge_vs_trigger_pp": None
            if best_edge is None
            else (best_edge - trigger) * Decimal("100"),
            "qualifying_count": qualifying,
            "near_count": max(near, inventory_near),
            "open_paper_allowed": open_allowed,
            "pairwise": pairwise,
            "paper_decisions": decisions,
            "discovered_fixtures": [
                item.model_dump(mode="json") for item in collection.discovered_fixtures
            ],
            "inventory": {
                key: [row.model_dump(mode="json") for row in rows]
                for key, rows in collection.fixture_markets.items()
            },
            "issues": [item.model_dump(mode="json") for item in collection.issues],
            "provider_limitations": limitations,
            "matchbook_owner_requirement": (
                "Matchbook credentials were unavailable in this environment. "
                "Owner Windows smoke-test with local MATCHBOOK_USERNAME/PASSWORD is still required "
                "for the Matchbook discovery/matching/solver legs."
                if not credentials or mb_error
                else "Matchbook credentials were present and the Matchbook leg was attempted live."
            ),
        }
        return _jsonable(report)
    finally:
        intelligence_store.close()
        await matchbook.aclose()
        await polymarket.aclose()
        await kalshi.aclose()


async def _safe_list(client: Any) -> tuple[Any, str | None]:
    try:
        return await client.list_events(), None
    except Exception as exc:  # noqa: BLE001 — live provider failures must be reported, not raised
        return {"events": []}, f"{type(exc).__name__}: {exc}"


def _identity_reason(
    *,
    credentials: bool,
    mb_error: str | None,
    pm_derby: list[dict[str, Any]],
    k_derby: list[dict[str, Any]],
    identity_pairs: dict[str, list[dict[str, Any]]],
) -> str:
    if not pm_derby:
        return "polymarket_named_derby_not_discovered"
    if not k_derby:
        return "kalshi_named_derby_not_discovered"
    pm_k = identity_pairs["polymarket_kalshi"]
    matched = [row for row in pm_k if row["matched"]]
    if matched:
        if credentials and not mb_error:
            return "pm_k_matched; matchbook attempted"
        return "pm_k_matched; matchbook unavailable in this environment"
    reasons = sorted({reason for row in pm_k for reason in row.get("reasons") or []})
    if reasons:
        return "pm_k_identity_not_matched: " + ", ".join(reasons)
    return "pm_k_identity_not_matched"


def _pairwise_solver_summary(
    identity_pairs: dict[str, list[dict[str, Any]]],
    decisions: list[dict[str, Any]],
    collection: CollectionReport,
) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for pair in ("matchbook_polymarket", "matchbook_kalshi", "polymarket_kalshi"):
        identity = identity_pairs[pair]
        matched_events = sum(1 for row in identity if row["matched"])
        pair_decisions = [item for item in decisions if item["pair"] == pair]
        solver_executed = any(item["entered_solver"] for item in pair_decisions)
        equivalent = 0
        for rows in collection.fixture_markets.values():
            for row in rows:
                if pair == "polymarket_kalshi" and row.polymarket and row.kalshi:
                    if row.comparison_status.value == "matched_equivalent":
                        equivalent += 1
                elif pair == "matchbook_polymarket" and row.matchbook and row.polymarket:
                    if row.comparison_status.value == "matched_equivalent":
                        equivalent += 1
                elif pair == "matchbook_kalshi" and row.matchbook and row.kalshi:
                    if row.comparison_status.value == "matched_equivalent":
                        equivalent += 1
        edges = [
            Decimal(item["current_net_edge"])
            for item in pair_decisions
            if item["current_net_edge"] is not None
        ]
        if not pair_decisions:
            if matched_events == 0:
                reasons = sorted({reason for row in identity for reason in row.get("reasons") or []})
                reason = (
                    "event_identity_not_matched: " + ", ".join(reasons)
                    if reasons
                    else "event_identity_not_matched"
                )
            else:
                reason = "events_matched_but_no_solver_decision"
        elif solver_executed:
            reason = "solver_executed"
        else:
            reason = ", ".join(
                sorted({rej for item in pair_decisions for rej in item["rejection_reasons"]})
            ) or "solver_not_entered"
        summary[pair] = {
            "events_matched": matched_events,
            "solver_executed": solver_executed,
            "equivalent_count": equivalent,
            "decision_count": len(pair_decisions),
            "best_net_edge": max(edges) if edges else None,
            "qualifying": sum(1 for item in pair_decisions if item["solver_is_arbitrage"]),
            "open_paper_allowed": any(item["eligible_for_paper_open"] for item in pair_decisions),
            "reason": reason,
        }
    return summary


def _limitations(
    *,
    credentials: bool,
    mb_error: str | None,
    pm_error: str | None,
    k_error: str | None,
    identity_pairs: dict[str, list[dict[str, Any]]],
    pm_derby: list[dict[str, Any]],
    k_derby: list[dict[str, Any]],
    collection: CollectionReport,
) -> list[str]:
    notes: list[str] = []
    if not credentials or mb_error:
        notes.append(
            "Matchbook was not authenticated here. Do not treat this run as a three-venue pass."
        )
    if pm_error:
        notes.append(f"Polymarket list_events failed: {pm_error}")
    if k_error:
        notes.append(f"Kalshi list_events failed: {k_error}")
    pm_k = identity_pairs["polymarket_kalshi"]
    kickoff_fail = any("kickoff_outside_tolerance" in (row.get("reasons") or []) for row in pm_k)
    if kickoff_fail and pm_derby and k_derby:
        notes.append(
            "PM↔K identity failed kickoff matching. Kalshi scheduled kickoff is the soccer "
            "milestone start_date, not market occurrence_datetime. occurrence_datetime that "
            "equals expected_expiration_time is an expiration clock and is refused. "
            "If the provider omitted milestones, identity fails closed rather than guessing."
        )
    notes.append(
        "Polymarket and Kalshi often split a fixture into multiple source events "
        "(moneyline / BTTS / totals / FTTS). Clustering unions those source events "
        "by canonical fixture identity so settlement-equivalent families can meet. "
        "Market pairing remains one-to-one and fail-closed."
    )
    notes.append(
        "Polymarket match-result Yes/No binaries are assembled into a 3-way HOME/"
        "DRAW/AWAY market only when all three complementary contracts share a "
        "settlement fingerprint. A lone binary moneyline is not equivalent to a "
        "Kalshi three-way GAME and remains fail-closed (outcome_space_mismatch / "
        "incomplete_outcome_set)."
    )
    if collection.config_warnings:
        notes.extend(collection.config_warnings)
    return notes


def _family_reasons(collection: CollectionReport) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for inventory in collection.fixture_markets.values():
        for row in inventory:
            pair_bits = []
            if row.polymarket is not None and row.kalshi is not None:
                pair_bits.append("polymarket_kalshi")
            if row.matchbook is not None and row.polymarket is not None:
                pair_bits.append("matchbook_polymarket")
            if row.matchbook is not None and row.kalshi is not None:
                pair_bits.append("matchbook_kalshi")
            if not pair_bits and (row.polymarket is not None or row.kalshi is not None):
                pair_bits.append("unpaired")
            reason = (
                row.reason
                or (", ".join(row.rejection_reasons) if row.rejection_reasons else None)
                or (", ".join(row.match_reasons) if row.match_reasons else None)
                or row.comparison_status.value
            )
            key = (
                str(row.family or "unknown"),
                "" if row.line is None else str(row.line),
                reason,
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "family": row.family,
                    "line": row.line,
                    "comparison_status": row.comparison_status.value,
                    "entered_solver": row.entered_solver,
                    "solver_model": row.solver_model,
                    "reason": reason,
                    "match_reasons": list(row.match_reasons),
                    "rejection_reasons": list(row.rejection_reasons),
                    "pairs": pair_bits,
                    "has_polymarket": row.polymarket is not None,
                    "has_kalshi": row.kalshi is not None,
                    "has_matchbook": row.matchbook is not None,
                }
            )
    return rows


async def _amain() -> None:
    import json
    from subprocess import check_output

    try:
        head = check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    except Exception:
        head = None
    report = await run_manchester_derby_logical(head_sha=head)
    print(render_manchester_derby_report(report))
    print(json.dumps(report, indent=2, default=str)[:20000])


if __name__ == "__main__":
    import asyncio

    asyncio.run(_amain())

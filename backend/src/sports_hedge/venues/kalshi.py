from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueCapabilities, VenueHealth, VenueName
from sports_hedge.venues.base import ReadOnlyVenue, market_data_http_timeout
from sports_hedge.venues.rate_limit import ProviderCooldown, ProviderRateLimitedError

_FOOTBALL_SERIES_TOKENS = frozenset(
    {"soccer", "football", "association football", "epl", "premier league", "la liga", "championship"}
)


class KalshiDiscoveryError(RuntimeError):
    """Raised when Kalshi market-data discovery cannot proceed without guessing."""


class KalshiClient(ReadOnlyVenue):
    """Public Kalshi Trade API v2 market-data client.

    Phase 1 uses unauthenticated market-data endpoints only. There is no order
    placement, cancellation, signing, or portfolio mutation here. Capability
    flags match Matchbook: data and paper enabled, execution disabled because
    Sports Hedge globally has real execution off in Phase 1.
    """

    name = VenueName.KALSHI
    capabilities = VenueCapabilities(
        data_enabled=True,
        paper_enabled=True,
        execution_enabled=False,
    )

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
        cooldown: ProviderCooldown | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._cooldown = cooldown
        self.last_series_report: list[dict[str, Any]] = []
        self._clock = clock or (lambda: datetime.now(UTC))
        self._base_url = settings.resolved_kalshi_base_url().rstrip("/")
        self._market_cache: dict[str, dict[str, Any]] = {}
        self._market_inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._series_cache: dict[str, dict[str, Any]] = {}
        self._series_inflight: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._contract_terms_cache: dict[str, dict[str, Any]] = {}
        self._client = client or httpx.AsyncClient(
            timeout=market_data_http_timeout(),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "sports-hedge/0.1 paper-research",
            },
        )

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._cooldown is not None:
            self._cooldown.raise_if_active()
        url = path if str(path).startswith("http") else f"{self._base_url}{path}"
        response = await self._client.get(url, params=params)
        if self._cooldown is not None and self._cooldown.observe_status(
            response.status_code, response.headers
        ):
            raise ProviderRateLimitedError(
                self._cooldown.remaining_seconds(),
                provider="kalshi",
            )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise KalshiDiscoveryError(
                f"Kalshi GET {path} failed with HTTP {response.status_code}"
            ) from exc
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def list_series(self, **filters: Any) -> dict[str, Any]:
        limit = int(filters.get("limit") or self.settings.kalshi_event_page_limit)
        params = {**filters, "limit": limit}
        if "cursor" in filters:
            page = await self._get("/series", params=params)
            return {**page, "series": _extract_items(page, "series"), "truncated": False}
        return await self._paginate(
            "/series",
            params=params,
            item_key="series",
            limit=limit,
            max_pages=self.settings.kalshi_event_max_pages,
        )

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        """Read-only Get Series with a successful per-client cache and single-flight.

        Concurrent callers for the same ticker share one HTTP call. Transient
        failures are not stored as successful metadata; a later retry may fetch
        again. Series names are not settlement proof.
        """

        key = str(series_ticker or "").strip()
        if not key:
            raise KalshiDiscoveryError("Kalshi get_series requires a series ticker")

        async def _fetch() -> dict[str, Any]:
            payload = await self._get(f"/series/{key}")
            series = payload.get("series")
            resolved = series if isinstance(series, dict) else payload
            if not isinstance(resolved, dict) or not resolved:
                raise KalshiDiscoveryError(f"Kalshi get_series {key} returned no series object")
            return resolved

        return await _single_flight_cached(self._series_cache, self._series_inflight, key, _fetch)

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        """List open Kalshi events, optionally restricted to configured series."""

        limit = int(filters.get("limit") or self.settings.kalshi_event_page_limit)
        series_tickers = _requested_series(filters, self.settings.kalshi_series_tickers)
        nested = filters.get("with_nested_markets", "true")
        with_milestones = filters.get("with_milestones", "true")
        if "cursor" in filters or filters.get("series_ticker"):
            params = {
                "status": filters.get("status", "open"),
                "limit": limit,
                "with_nested_markets": nested,
                "with_milestones": with_milestones,
                **{
                    key: value
                    for key, value in filters.items()
                    if key not in {"series_tickers", "with_nested_markets", "with_milestones"}
                },
            }
            page = await self._get("/events", params=params)
            events = _extract_items(page_payload(page), "events")
            milestones = _extract_items(page, "milestones")
            milestones = await self._ensure_milestones(events, milestones)
            return {**page, "events": events, "milestones": milestones, "truncated": False}

        if series_tickers:
            events: list[dict[str, Any]] = []
            milestones: list[dict[str, Any]] = []
            seen: set[str] = set()
            truncated = False
            series_results: list[dict[str, Any]] = []
            first_total_error: Exception | None = None
            for ticker in series_tickers:
                try:
                    page = await self._paginate(
                        "/events",
                        params={
                            "status": filters.get("status", "open"),
                            "limit": limit,
                            "series_ticker": ticker,
                            "with_nested_markets": nested,
                            "with_milestones": with_milestones,
                        },
                        item_key="events",
                        extra_keys=("milestones",),
                        limit=limit,
                        max_pages=self.settings.kalshi_event_max_pages,
                    )
                except Exception as exc:
                    status, retryable = _series_failure_kind(exc)
                    series_results.append(
                        {
                            "series": ticker,
                            "status": status,
                            "retryable": retryable,
                            "event_count": 0,
                            "reason": str(exc),
                        }
                    )
                    if first_total_error is None:
                        first_total_error = exc
                    continue
                truncated = truncated or bool(page.get("truncated"))
                milestones.extend(page.get("milestones") or [])
                retained = 0
                for item in page.get("events", []):
                    event_id = str(item.get("event_ticker") or item.get("ticker") or "").strip()
                    if event_id and event_id in seen:
                        continue
                    if event_id:
                        seen.add(event_id)
                    events.append(item)
                    retained += 1
                series_results.append(
                    {
                        "series": ticker,
                        "status": "ok",
                        "retryable": False,
                        "event_count": retained,
                        "reason": None,
                    }
                )
            self.last_series_report = series_results
            if not events and series_results and all(item["status"] != "ok" for item in series_results):
                if first_total_error is not None:
                    raise first_total_error
            milestones = _dedupe_by_id(milestones)
            milestones = await self._ensure_milestones(events, milestones)
            return {
                "events": events,
                "milestones": milestones,
                "truncated": truncated,
                "total": len(events),
                "series_results": series_results,
                "partial": any(item["status"] != "ok" for item in series_results) and bool(events),
            }

        page = await self._paginate(
            "/events",
            params={
                "status": filters.get("status", "open"),
                "limit": limit,
                "with_nested_markets": nested,
                "with_milestones": with_milestones,
            },
            item_key="events",
            extra_keys=("milestones",),
            limit=limit,
            max_pages=self.settings.kalshi_event_max_pages,
        )
        events = list(page.get("events") or [])
        milestones = _dedupe_by_id(page.get("milestones") or [])
        milestones = await self._ensure_milestones(events, milestones)
        return {**page, "events": events, "milestones": milestones}

    async def list_milestones(self, **filters: Any) -> list[dict[str, Any]]:
        """List Kalshi milestones. Soccer `start_date` is scheduled kickoff."""

        limit = int(filters.get("limit") or self.settings.kalshi_event_page_limit)
        params = {key: value for key, value in filters.items() if key not in {"limit"}}
        page = await self._paginate(
            "/milestones",
            params=params,
            item_key="milestones",
            limit=limit,
            max_pages=self.settings.kalshi_event_max_pages,
        )
        return _dedupe_by_id(list(page.get("milestones") or []))

    async def _ensure_milestones(
        self,
        events: list[dict[str, Any]],
        milestones: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Attach event-list milestones, then fetch Sports milestones if kickoff is still missing.

        Kalshi `occurrence_datetime` is not scheduled kickoff. Soccer milestone
        `start_date` is the authoritative clock. A missing `/milestones` response
        fails closed rather than inventing a kickoff from expiration.
        """

        attach_milestones_to_events(events, milestones)
        if not events or all(_event_has_kickoff_clock(event) for event in events):
            return _dedupe_by_id(milestones)
        try:
            extra = await self.list_milestones(category="Sports")
        except KalshiDiscoveryError:
            return _dedupe_by_id(milestones)
        combined = _dedupe_by_id([*milestones, *extra])
        attach_milestones_to_events(events, combined)
        return combined

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        limit = int(filters.get("limit") or self.settings.kalshi_event_page_limit)
        params = {
            "event_ticker": str(event_id),
            "limit": limit,
            "status": filters.get("status", "open"),
            **{key: value for key, value in filters.items() if key not in {"event_ticker", "limit"}},
        }
        if "cursor" in filters:
            page = await self._get("/markets", params=params)
            return {**page, "markets": _extract_items(page, "markets"), "truncated": False}
        return await self._paginate(
            "/markets",
            params=params,
            item_key="markets",
            limit=limit,
            max_pages=self.settings.kalshi_event_max_pages,
        )

    async def get_market(self, ticker: str) -> dict[str, Any]:
        """Documented read-only Get Market. Source of `rules_primary` / `rules_secondary`.

        Nested `/events?with_nested_markets=true` and `/markets` list pages may
        omit contract-rule text. Do not infer settlement from GAME/Opta names.
        Cached per ticker for the life of this client instance.
        """

        key = str(ticker or "").strip()
        if not key:
            raise KalshiDiscoveryError("Kalshi get_market requires a ticker")

        async def _fetch() -> dict[str, Any]:
            payload = await self._get(f"/markets/{key}")
            market = payload.get("market")
            resolved = market if isinstance(market, dict) else payload
            if not isinstance(resolved, dict) or not resolved:
                raise KalshiDiscoveryError(f"Kalshi get_market {key} returned no market object")
            return resolved

        return await _single_flight_cached(self._market_cache, self._market_inflight, key, _fetch)

    async def get_contract_terms_document(self, url: str) -> dict[str, Any]:
        """Bounded read-only GET of an allowlisted public contract_terms_url.

        Uses a dedicated unauthenticated client: no venue credentials, no
        redirects off the allowlisted URL, no PDF body returned. Cached once
        per URL for the life of this client.
        """

        from sports_hedge.normalization.kalshi_contract_terms import (
            KALSHI_CONTRACT_TERMS_MAX_BYTES,
            kalshi_contract_terms_url_is_allowlisted,
            sha256_hex,
        )

        key = str(url or "").strip()
        if not kalshi_contract_terms_url_is_allowlisted(key):
            raise KalshiDiscoveryError("Kalshi contract_terms_url host/path is not allowlisted")
        cached = self._contract_terms_cache.get(key)
        if cached is not None:
            return cached
        async with httpx.AsyncClient(
            timeout=market_data_http_timeout(),
            follow_redirects=False,
            headers={
                "Accept": "application/pdf,application/octet-stream,*/*",
                "Accept-Encoding": "gzip",
                "User-Agent": "sports-hedge/0.1 paper-research",
            },
        ) as public:
            response = await public.get(key)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise KalshiDiscoveryError(
                f"Kalshi contract_terms_url fetch failed with HTTP {response.status_code}"
            ) from exc
        payload = bytes(response.content or b"")
        if not payload:
            raise KalshiDiscoveryError("Kalshi contract_terms_url returned an empty document")
        if len(payload) > KALSHI_CONTRACT_TERMS_MAX_BYTES:
            raise KalshiDiscoveryError("Kalshi contract_terms_url exceeded bounded size")
        resolved = {
            "url": key,
            "sha256": sha256_hex(payload),
            "byte_length": len(payload),
            "content_type": str(response.headers.get("content-type") or "")[:80],
        }
        self._contract_terms_cache[key] = resolved
        return resolved

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id
        ticker = str(market_id)
        params = {**filters}
        payload = await self._get(f"/markets/{ticker}/orderbook", params=params)
        if "orderbook_fp" not in payload and "orderbook" in payload:
            raise KalshiDiscoveryError(
                f"Kalshi orderbook for {ticker} lacked orderbook_fp dollar/fixed-point fields"
            )
        return payload

    async def health(self) -> VenueHealth:
        try:
            await self._get("/exchange/status")
            return VenueHealth(
                venue=self.name,
                ok=True,
                authenticated=False,
                checked_at=self._clock(),
                detail="Public Kalshi market data reachable; execution disabled (Phase 1)",
            )
        except Exception as exc:  # health reports, does not raise
            try:
                await self._get("/events", params={"limit": 1, "status": "open"})
                return VenueHealth(
                    venue=self.name,
                    ok=True,
                    authenticated=False,
                    checked_at=self._clock(),
                    detail="Public Kalshi events reachable; execution disabled (Phase 1)",
                )
            except Exception:
                return VenueHealth(
                    venue=self.name,
                    ok=False,
                    authenticated=False,
                    checked_at=self._clock(),
                    detail=str(exc),
                )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _paginate(
        self,
        path: str,
        *,
        params: dict[str, Any],
        item_key: str,
        limit: int,
        max_pages: int,
        extra_keys: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        extras: dict[str, list[dict[str, Any]]] = {key: [] for key in extra_keys}
        cursor: str | None = None
        truncated = False
        last_page: dict[str, Any] = {}
        for page_index in range(max_pages):
            page_params = {**params, "limit": limit}
            if cursor:
                page_params["cursor"] = cursor
            last_page = await self._get(path, params=page_params)
            page_items = _extract_items(last_page, item_key)
            items.extend(page_items)
            for key in extra_keys:
                extras[key].extend(_extract_items(last_page, key))
            cursor = str(last_page.get("cursor") or "").strip() or None
            if not page_items or cursor is None:
                break
            if page_index == max_pages - 1 and cursor:
                truncated = True
        else:
            truncated = True
        return {
            **last_page,
            item_key: items,
            **extras,
            "truncated": truncated,
            "total": len(items),
        }


async def _single_flight_cached(
    cache: dict[str, dict[str, Any]],
    inflight: dict[str, asyncio.Task[dict[str, Any]]],
    key: str,
    fetch: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Share one in-flight fetch per key. Cache successful payloads only."""

    cached = cache.get(key)
    if cached is not None:
        return cached
    existing = inflight.get(key)
    if existing is not None:
        return await asyncio.shield(existing)

    async def _run() -> dict[str, Any]:
        try:
            resolved = await fetch()
            cache[key] = resolved
            return resolved
        finally:
            inflight.pop(key, None)

    task = asyncio.create_task(_run())
    inflight[key] = task
    return await asyncio.shield(task)


def football_series_ticker(series: dict[str, Any]) -> bool:
    """True when series metadata is association-football relevant. Not NFL."""

    tokens = " ".join(
        [
            str(series.get("ticker") or ""),
            str(series.get("title") or ""),
            str(series.get("category") or ""),
            " ".join(str(tag) for tag in (series.get("tags") or []) if tag),
        ]
    ).casefold()
    if "nfl" in tokens or "american football" in tokens:
        return False
    return any(token in tokens for token in _FOOTBALL_SERIES_TOKENS)


def page_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


def _requested_series(filters: dict[str, Any], configured: list[str]) -> list[str]:
    explicit = filters.get("series_tickers")
    if explicit is not None:
        if isinstance(explicit, str):
            return [part.strip() for part in explicit.split(",") if part.strip()]
        return [str(item).strip() for item in explicit if str(item).strip()]
    ticker = filters.get("series_ticker")
    if ticker:
        return [str(ticker).strip()]
    return [item.strip() for item in configured if str(item).strip()]


def _series_failure_kind(exc: BaseException) -> tuple[str, bool]:
    text = str(exc).casefold()
    if "401" in text or "403" in text or "auth" in text:
        return "auth_failure", False
    if "unsupported" in text or "404" in text:
        return "unsupported", False
    if "429" in text or "rate-limited" in text:
        return "rate_limited", True
    if "timeout" in text or "timed out" in text:
        return "discovery_timeout", True
    return "unavailable", True


def _event_has_kickoff_clock(event: dict[str, Any]) -> bool:
    """True when the event already carries a scheduled-start clock, not expiration."""

    if event.get("start_date"):
        return True
    milestone = event.get("milestone")
    if isinstance(milestone, dict) and milestone.get("start_date"):
        return True
    for key in ("target_datetime", "game_start_time", "scheduled_start", "strike_date"):
        raw = event.get(key)
        if raw and "T" in str(raw):
            return True
    return False


def attach_milestones_to_events(
    events: list[dict[str, Any]],
    milestones: list[dict[str, Any]],
) -> None:
    """Copy the matching soccer milestone onto each event for kickoff parsing.

    Kalshi documents milestones as the real-world occurrence. Soccer
    ``start_date`` is scheduled kickoff. Market ``occurrence_datetime`` is not.
    """

    by_ticker: dict[str, dict[str, Any]] = {}
    for milestone in milestones:
        if not isinstance(milestone, dict):
            continue
        for ticker in _milestone_event_tickers(milestone):
            current = by_ticker.get(ticker)
            if current is None or _milestone_kickoff_rank(milestone) < _milestone_kickoff_rank(current):
                by_ticker[ticker] = milestone
    for event in events:
        ticker = str(event.get("event_ticker") or event.get("ticker") or "").strip()
        milestone = by_ticker.get(ticker)
        if milestone is None:
            continue
        event["milestone"] = milestone
        start = milestone.get("start_date")
        if start and not event.get("start_date"):
            event["start_date"] = start


def _milestone_event_tickers(milestone: dict[str, Any]) -> list[str]:
    tickers: list[str] = []
    details = milestone.get("details")
    if isinstance(details, dict):
        main = str(details.get("main_game_event_ticker") or "").strip()
        if main:
            tickers.append(main)
    for key in ("primary_event_tickers", "related_event_tickers"):
        values = milestone.get(key) or []
        if isinstance(values, list):
            tickers.extend(str(item).strip() for item in values if str(item).strip())
    seen: set[str] = set()
    unique: list[str] = []
    for ticker in tickers:
        if ticker in seen:
            continue
        seen.add(ticker)
        unique.append(ticker)
    return unique


def _milestone_kickoff_rank(milestone: dict[str, Any]) -> tuple[int, int]:
    milestone_type = str(milestone.get("type") or "").casefold()
    soccer = 0 if "soccer" in milestone_type else 1
    has_start = 0 if milestone.get("start_date") else 1
    return soccer, has_start


def _dedupe_by_id(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in items:
        item_id = str(item.get("id") or "").strip()
        if item_id and item_id in seen:
            continue
        if item_id:
            seen.add(item_id)
        unique.append(item)
    return unique


def _extract_items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    nested = payload.get("data")
    if isinstance(nested, dict) and isinstance(nested.get(key), list):
        return [item for item in nested[key] if isinstance(item, dict)]
    return []

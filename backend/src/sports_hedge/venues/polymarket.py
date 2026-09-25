from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueCapabilities, VenueHealth, VenueName
from sports_hedge.venues.base import ReadOnlyVenue, market_data_http_timeout
from sports_hedge.venues.rate_limit import ProviderCooldown, ProviderRateLimitedError

_SERIES_IDS_UNSET = object()


class PolymarketClient(ReadOnlyVenue):
    """Public-data-only Polymarket adapter.

    Phase 1 deliberately contains no authentication, wallet, signing, or order
    submission code. Only public Gamma/CLOB market-data endpoints are used.
    """

    name = VenueName.POLYMARKET
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
        cooldown: ProviderCooldown | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._cooldown = cooldown
        self.last_series_report: list[dict[str, Any]] = []
        self.last_pages_attempted: int = 0
        self._discovery_page_index = 0
        self._discovery_page_budget: int | None = None
        self._client = client or httpx.AsyncClient(
            timeout=market_data_http_timeout(),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "sports-hedge/0.1 paper-research",
            },
        )

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        """List public Gamma events for configured or caller-selected series.

        ``series_id`` selects exactly one Gamma series. Plural ``series_ids``
        is a client-side discovery control: it is removed before any provider
        request and, when supplied without ``series_id``, selects those series
        in order. Settings defaults apply only when neither control is supplied.
        Pagination stays per-series, bounded, and read-only. Empty series
        results stay empty rather than inventing markets.
        """

        page_limit = self.settings.polymarket_gamma_page_limit
        params: dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": page_limit,
            **filters,
        }
        # Never a Gamma query parameter, including on the singular series_id path.
        requested_series_ids = params.pop("series_ids", _SERIES_IDS_UNSET)
        page_index, max_pages = _discovery_page_window(params)
        caller_series = params.get("series_id")
        if caller_series is not None and str(caller_series).strip() == "":
            params.pop("series_id", None)
            return await self._get_event_page(params)

        if caller_series is not None:
            self._discovery_page_index = page_index
            self._discovery_page_budget = max_pages
            return await self._list_series_events(str(caller_series), params)

        if requested_series_ids is _SERIES_IDS_UNSET:
            series_ids = self.settings.resolved_polymarket_series_ids()
        else:
            series_ids = _ordered_series_ids(requested_series_ids)
            if not series_ids:
                self.last_series_report = []
                return []

        if not series_ids:
            return await self._get_event_page(params)

        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        series_results: list[dict[str, Any]] = []
        first_total_error: Exception | None = None
        for series_id in series_ids:
            try:
                self._discovery_page_index = page_index
                self._discovery_page_budget = max_pages
                page_items = await self._list_series_events(series_id, params)
                pages_attempted = self.last_pages_attempted
            except Exception as exc:
                status, retryable = _series_failure_kind(exc)
                series_results.append(
                    {
                        "series": series_id,
                        "status": status,
                        "retryable": retryable,
                        "event_count": 0,
                        "pages_attempted": self.last_pages_attempted,
                        "http_attempted": True,
                        "reason": str(exc),
                    }
                )
                if first_total_error is None:
                    first_total_error = exc
                continue
            retained = 0
            for item in page_items:
                event_id = str(item.get("id", "")).strip()
                if event_id and event_id in seen:
                    continue
                if event_id:
                    seen.add(event_id)
                events.append(item)
                retained += 1
            series_results.append(
                {
                    "series": series_id,
                    "status": "ok",
                    "retryable": False,
                    "event_count": retained,
                    "pages_attempted": pages_attempted,
                    "http_attempted": True,
                    "empty": retained == 0,
                    "reason": None,
                }
            )
        self.last_series_report = series_results
        if not events and series_results and all(item["status"] != "ok" for item in series_results):
            if first_total_error is not None:
                raise first_total_error
        return events

    async def _list_series_events(
        self,
        series_id: str,
        base_params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        page_limit = int(base_params.get("limit") or self.settings.polymarket_gamma_page_limit)
        page_index = int(getattr(self, "_discovery_page_index", 0) or 0)
        max_pages = getattr(self, "_discovery_page_budget", None)
        page_budget = (
            self.settings.polymarket_gamma_max_pages_per_series if max_pages is None else max_pages
        )
        self.last_pages_attempted = 0
        for step in range(max(0, int(page_budget))):
            page = page_index + step
            self.last_pages_attempted = step + 1
            params = {
                **base_params,
                "series_id": series_id,
                "limit": page_limit,
                "offset": page * page_limit,
            }
            page_items = await self._get_event_page(params)
            events.extend(page_items)
            if len(page_items) < page_limit:
                break
        return events

    async def _get_event_page(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        if self._cooldown is not None:
            self._cooldown.raise_if_active()
        response = await self._client.get(
            f"{self.settings.polymarket_gamma_base_url.rstrip('/')}/events",
            params=params,
        )
        if self._cooldown is not None and self._cooldown.observe_status(
            response.status_code, response.headers
        ):
            raise ProviderRateLimitedError(
                self._cooldown.remaining_seconds(),
                provider="polymarket",
            )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        if self._cooldown is not None:
            self._cooldown.raise_if_active()
        response = await self._client.get(
            f"{self.settings.polymarket_gamma_base_url.rstrip('/')}/events/{event_id}",
            params=filters,
        )
        if self._cooldown is not None and self._cooldown.observe_status(
            response.status_code, response.headers
        ):
            raise ProviderRateLimitedError(
                self._cooldown.remaining_seconds(),
                provider="polymarket",
            )
        response.raise_for_status()
        payload = response.json()
        markets = payload.get("markets", []) if isinstance(payload, dict) else []
        return markets

    async def get_market(self, market_id: int | str) -> dict[str, Any]:
        response = await self._client.get(
            f"{self.settings.polymarket_gamma_base_url.rstrip('/')}/markets/{market_id}"
        )
        response.raise_for_status()
        return response.json()

    async def get_event(self, event_id: int | str) -> dict[str, Any]:
        """Exact-ID Gamma event refresh. Read-only; used for settlement lifecycle."""

        response = await self._client.get(
            f"{self.settings.polymarket_gamma_base_url.rstrip('/')}/events/{event_id}"
        )
        response.raise_for_status()
        return response.json()

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id  # CLOB book lookup is token/outcome-id based.
        if outcome_id is None:
            raise ValueError("Polymarket order books require a CLOB token id")
        params = {"token_id": str(outcome_id), **filters}
        response = await self._client.get(
            f"{self.settings.polymarket_clob_base_url.rstrip('/')}/book",
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def health(self) -> VenueHealth:
        try:
            response = await self._client.get(
                f"{self.settings.polymarket_gamma_base_url.rstrip('/')}/events",
                params={"limit": 1},
            )
            response.raise_for_status()
            return VenueHealth(
                venue=self.name,
                ok=True,
                authenticated=False,
                checked_at=datetime.now(UTC),
                detail="Public Gamma/CLOB market data reachable; execution disabled",
            )
        except Exception as exc:  # health endpoint should report, not raise
            return VenueHealth(
                venue=self.name,
                ok=False,
                authenticated=False,
                checked_at=datetime.now(UTC),
                detail=str(exc),
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _discovery_page_window(params: dict[str, Any]) -> tuple[int, int | None]:
    """Client-side page window. Popped before any Gamma request.

    ``discovery_max_pages`` is the number of pages to fetch from
    ``discovery_page_index``. Omitted, the series uses the configured
    per-series page cap from the start.
    """

    raw_index = params.pop("discovery_page_index", 0)
    raw_max = params.pop("discovery_max_pages", None)
    try:
        page_index = max(0, int(raw_index or 0))
    except (TypeError, ValueError):
        page_index = 0
    if raw_max is None:
        return page_index, None
    try:
        return page_index, max(1, int(raw_max))
    except (TypeError, ValueError):
        return page_index, None


def _ordered_series_ids(value: Any) -> list[str]:
    """Deduplicate a client-side series selection, preserving first-seen order."""

    if isinstance(value, str):
        raw_items: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        raw_items = list(value)
    else:
        raw_items = [value]
    selected: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        selected.append(text)
    return selected


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

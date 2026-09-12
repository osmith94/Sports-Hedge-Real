from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueHealth, VenueName
from sports_hedge.normalization.text import normalize_text
from sports_hedge.venues.base import ReadOnlyVenue

# Official lookups/sports names that mean association football only.
# NCAA / American / Gaelic football are not in this set and must not match.
_ASSOCIATION_FOOTBALL_SPORT_NAMES = frozenset(
    {"football", "soccer", "association football"}
)


class MatchbookAuthError(RuntimeError):
    pass


class MatchbookDiscoveryError(RuntimeError):
    """Raised when football discovery cannot proceed without guessing."""


class MatchbookClient(ReadOnlyVenue):
    """Read-only Matchbook market-data client for Sports Hedge Phase 1."""

    name = VenueName.MATCHBOOK

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client = client or httpx.AsyncClient(
            base_url=settings.matchbook_base_url.rstrip("/"),
            timeout=httpx.Timeout(10.0),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "sports-hedge/0.1 paper-research",
            },
        )
        self._session_token: str | None = None
        self._football_sport_id: int | None = None

    async def login(self) -> str:
        username = self.settings.matchbook_username
        password = self.settings.matchbook_password
        if not username or not password:
            raise MatchbookAuthError(
                "MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD are required for Matchbook API access"
            )

        payload: dict[str, str] = {"username": username, "password": password}
        if self.settings.matchbook_mfa_code:
            payload["mfa-code"] = self.settings.matchbook_mfa_code

        response = await self._client.post(
            "/bpapi/rest/security/session",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()

        body = response.json()
        token = (
            body.get("session-token")
            or body.get("session_token")
            or response.cookies.get("session-token")
        )
        if not token:
            raise MatchbookAuthError("Matchbook login succeeded but returned no session token")

        self._session_token = str(token)
        self._client.headers["session-token"] = self._session_token
        return self._session_token

    async def _ensure_session(self) -> None:
        if not self._session_token:
            await self.login()

    async def _get(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_session()
        response = await self._client.get(path, params=params)
        if response.status_code == 401:
            self._session_token = None
            self._client.headers.pop("session-token", None)
            await self.login()
            response = await self._client.get(path, params=params)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    def _market_data_params(self) -> dict[str, Any]:
        return {
            "exchange-type": "back-lay",
            "odds-type": "DECIMAL",
            "currency": self.settings.matchbook_currency,
            "minimum-liquidity": self.settings.matchbook_minimum_liquidity,
            "price-mode": "expanded",
        }

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        """List Matchbook events for football within a bounded live/near-future window.

        Provider ``sport-ids`` plus ``after``/``before`` are an efficiency layer.
        Pagination uses ``offset`` until the page is short, ``total`` is reached,
        or the configured page cap is hit. Callers that pass ``offset`` receive
        a single page. The collector allowlist remains the competition gate.
        """

        per_page = int(filters.get("per-page") or self.settings.matchbook_event_per_page)
        params: dict[str, Any] = {
            **self._market_data_params(),
            "states": "open,suspended",
            "include-prices": "false",
            "per-page": per_page,
            **filters,
        }
        sport_ids = str(params.get("sport-ids") or "").strip()
        if not sport_ids:
            params["sport-ids"] = str(await self.resolve_football_sport_id())
        after_window, before_window = self._fixture_window_epochs()
        if "after" not in filters:
            params["after"] = after_window
        if "before" not in filters:
            params["before"] = before_window
        params["per-page"] = per_page

        if "offset" in filters:
            page = await self._get("/edge/rest/events", params=params)
            events = _extract_items(page, "events")
            return {
                **page,
                "events": events,
                "truncated": False,
            }

        return await self._paginate_events(params, per_page=per_page)

    async def resolve_football_sport_id(self) -> int:
        if self._football_sport_id is not None:
            return self._football_sport_id
        sports = await self._list_sports()
        sport_id = select_football_sport_id(sports)
        self._football_sport_id = sport_id
        return sport_id

    async def _list_sports(self) -> list[dict[str, Any]]:
        sports: list[dict[str, Any]] = []
        per_page = 100
        max_pages = 5
        offset = 0
        for _ in range(max_pages):
            payload = await self._get(
                "/edge/rest/lookups/sports",
                params={
                    "offset": offset,
                    "per-page": per_page,
                    "status": "active",
                    "order": "name asc",
                },
            )
            page_items = _extract_items(payload, "sports")
            sports.extend(page_items)
            total = _optional_int(payload.get("total"))
            if not page_items:
                break
            offset += len(page_items)
            if total is not None and offset >= total:
                break
            reported_per_page = _optional_int(payload.get("per-page")) or per_page
            if len(page_items) < reported_per_page and (total is None or offset >= total):
                break
        else:
            raise MatchbookDiscoveryError(
                "Matchbook lookups/sports exceeded the safety page cap before completing"
            )
        return sports

    async def _paginate_events(self, params: dict[str, Any], *, per_page: int) -> dict[str, Any]:
        events: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        max_pages = self.settings.matchbook_event_max_pages
        max_events = max_pages * per_page
        reported_total: int | None = None
        offset = 0
        pages_fetched = 0
        last_page_len = 0
        while pages_fetched < max_pages and len(events) < max_events:
            page_params = {**params, "offset": offset, "per-page": per_page}
            payload = await self._get("/edge/rest/events", params=page_params)
            reported_total = _optional_int(payload.get("total")) or reported_total
            page_events = _extract_items(payload, "events")
            last_page_len = len(page_events)
            pages_fetched += 1
            for item in page_events:
                event_id = str(item.get("id", "")).strip()
                if event_id and event_id in seen_ids:
                    continue
                if event_id:
                    seen_ids.add(event_id)
                events.append(item)
            if last_page_len == 0:
                break
            offset += last_page_len
            if reported_total is not None and offset >= reported_total:
                break
            if last_page_len < per_page:
                break

        truncated = len(events) >= max_events and (
            reported_total is None or len(events) < reported_total or last_page_len >= per_page
        )
        if reported_total is not None and len(events) < reported_total and pages_fetched >= max_pages:
            truncated = True
        if last_page_len < per_page and (reported_total is None or len(events) >= reported_total):
            truncated = False

        truncation_detail = None
        if truncated:
            truncation_detail = (
                f"Matchbook event list truncated after {len(events)} events "
                f"({max_pages} pages of {per_page}); provider total="
                f"{reported_total if reported_total is not None else 'unknown'}"
            )
        return {
            "offset": 0,
            "per-page": per_page,
            "total": reported_total if reported_total is not None else len(events),
            "events": events,
            "truncated": truncated,
            "truncation-detail": truncation_detail,
        }

    def _fixture_window_epochs(self) -> tuple[int, int]:
        now = self._clock()
        if now.tzinfo is None:
            raise MatchbookDiscoveryError("Matchbook fixture window clock must be timezone-aware")
        after = now - timedelta(hours=self.settings.matchbook_fixture_lookback_hours)
        before = now + timedelta(hours=self.settings.matchbook_fixture_lookahead_hours)
        return int(after.timestamp()), int(before.timestamp())

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        params = {
            **self._market_data_params(),
            "states": "open,suspended",
            "include-prices": "true",
            "price-depth": self.settings.matchbook_price_depth,
            "per-page": 100,
            **filters,
        }
        return await self._get(f"/edge/rest/events/{event_id}/markets", params=params)

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        if outcome_id is None:
            raise ValueError("Matchbook order books require a runner/outcome id")
        params = {
            **self._market_data_params(),
            "depth": self.settings.matchbook_price_depth,
            **filters,
        }
        return await self._get(
            f"/edge/rest/events/{event_id}/markets/{market_id}/runners/{outcome_id}/prices",
            params=params,
        )

    async def health(self) -> VenueHealth:
        try:
            await self._ensure_session()
            return VenueHealth(
                venue=self.name,
                ok=True,
                authenticated=True,
                checked_at=datetime.now(UTC),
                detail="Matchbook session authenticated",
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


def select_football_sport_id(sports: list[dict[str, Any]]) -> int:
    """Pick the active association-football sport id. Unknown/ambiguous fail closed."""

    matched_ids: set[int] = set()
    matched_names: list[str] = []
    for item in sports:
        if not isinstance(item, dict):
            continue
        name = normalize_text(str(item.get("name", "")))
        if name not in _ASSOCIATION_FOOTBALL_SPORT_NAMES:
            continue
        sport_id = _optional_int(item.get("id"))
        if sport_id is None:
            raise MatchbookDiscoveryError(
                f"Matchbook football sport {name!r} has no numeric id"
            )
        matched_ids.add(sport_id)
        matched_names.append(name)
    if not matched_ids:
        raise MatchbookDiscoveryError(
            "Matchbook lookups/sports did not include an active association-football sport"
        )
    if len(matched_ids) > 1:
        raise MatchbookDiscoveryError(
            "Matchbook lookups/sports returned multiple association-football sport ids: "
            f"{sorted(matched_ids)} ({matched_names})"
        )
    return next(iter(matched_ids))


def _extract_items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

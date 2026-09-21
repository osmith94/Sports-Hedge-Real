from __future__ import annotations

import asyncio
import math
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueHealth, VenueName
from sports_hedge.normalization.text import normalize_text
from sports_hedge.venues.base import ReadOnlyVenue, market_data_http_timeout
from sports_hedge.venues.rate_limit import ProviderCooldown, RateLimitPolicy

# Official lookups/sports names that mean association football only.
# NCAA / American / Gaelic football are not in this set and must not match.
_ASSOCIATION_FOOTBALL_SPORT_NAMES = frozenset(
    {"football", "soccer", "association football"}
)
_AMERICAN_FOOTBALL_SPORT_NAMES = frozenset({"american football"})

MATCHBOOK_SESSION_PATH = "/bpapi/rest/security/session"
DEFAULT_LOGIN_COOLDOWN_SECONDS = 30.0
MAX_LOGIN_COOLDOWN_SECONDS = 300.0
LOGIN_ACCOUNT_LOCKED_CODE = "LOGIN_ACCOUNT_LOCKED_2"
_MAX_SAFE_ERROR_ITEMS = 5
_MAX_SAFE_MESSAGE_LEN = 200

_shared_client_lock = threading.Lock()
_shared_matchbook_client: MatchbookClient | None = None


def parse_matchbook_login_error_metadata(
    response: httpx.Response,
    *,
    secrets: tuple[str, ...] = (),
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract operator-safe login error codes/messages. Never request payloads."""

    try:
        body = response.json()
    except Exception:
        return (), ()
    if not isinstance(body, dict):
        return (), ()
    errors = body.get("errors")
    if not isinstance(errors, list):
        return (), ()
    codes: list[str] = []
    messages: list[str] = []
    seen_codes: set[str] = set()
    seen_messages: set[str] = set()
    for item in errors:
        if not isinstance(item, dict):
            continue
        raw_codes = item.get("codes")
        if isinstance(raw_codes, list):
            for raw in raw_codes:
                code = _safe_error_code(raw)
                if code and code not in seen_codes and len(codes) < _MAX_SAFE_ERROR_ITEMS:
                    seen_codes.add(code)
                    codes.append(code)
        raw_messages = item.get("messages")
        if isinstance(raw_messages, list):
            for raw in raw_messages:
                message = _safe_error_message(raw, secrets)
                if (
                    message
                    and message not in seen_messages
                    and len(messages) < _MAX_SAFE_ERROR_ITEMS
                ):
                    seen_messages.add(message)
                    messages.append(message)
    return tuple(codes), tuple(messages)


def _safe_error_code(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or len(text) > 80:
        return None
    if not all(ch.isalnum() or ch in "._-" for ch in text):
        return None
    return text


def _safe_error_message(value: Any, secrets: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    if not text:
        return None
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[redacted]")
    lowered = text.lower()
    if "session-token" in lowered or "session_token" in lowered:
        return None
    if len(text) > _MAX_SAFE_MESSAGE_LEN:
        text = text[:_MAX_SAFE_MESSAGE_LEN].rstrip() + "…"
    return text


class MatchbookAuthError(RuntimeError):
    pass


class MatchbookRateLimitedError(MatchbookAuthError):
    """Login is cooling down after HTTP 429 from POST /security/session."""

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = max(0.0, float(retry_after_seconds))
        wait_s = max(1, int(math.ceil(self.retry_after_seconds)))
        super().__init__(
            f"Matchbook authentication rate-limited (HTTP 429); retry after {wait_s}s"
        )


class MatchbookAuthFaultError(MatchbookAuthError):
    """Deterministic login rejection (HTTP 400). Fail fast until explicit reset."""

    def __init__(
        self,
        *,
        status_code: int = 400,
        codes: tuple[str, ...] = (),
        messages: tuple[str, ...] = (),
    ) -> None:
        self.status_code = int(status_code)
        self.codes = tuple(code for code in codes if code)
        self.messages = tuple(message for message in messages if message)
        super().__init__(self._operator_detail())

    def _operator_detail(self) -> str:
        parts = [f"Matchbook authentication rejected (HTTP {self.status_code})"]
        if LOGIN_ACCOUNT_LOCKED_CODE in self.codes:
            parts.append(f"account locked ({LOGIN_ACCOUNT_LOCKED_CODE})")
        elif self.codes:
            parts.append("codes " + ", ".join(self.codes))
        if self.messages:
            parts.append(self.messages[0])
        parts.append(
            "auth fault latched; correct credentials/MFA and restart the process to retry"
        )
        return "; ".join(parts)

    @classmethod
    def from_response(
        cls,
        response: httpx.Response,
        *,
        secrets: tuple[str, ...] = (),
    ) -> MatchbookAuthFaultError:
        codes, messages = parse_matchbook_login_error_metadata(response, secrets=secrets)
        return cls(status_code=response.status_code, codes=codes, messages=messages)


class MatchbookDiscoveryError(RuntimeError):
    """Raised when football discovery cannot proceed without guessing."""


class MatchbookMarketGoneError(MatchbookDiscoveryError):
    """Known-market refresh returned HTTP 404/410. HOT must fail closed."""

    def __init__(self, event_id: int | str, market_id: int | str, status_code: int) -> None:
        self.event_id = str(event_id)
        self.market_id = str(market_id)
        self.status_code = int(status_code)
        super().__init__(
            f"Matchbook market {self.market_id} for event {self.event_id} "
            f"unavailable (HTTP {self.status_code})"
        )


class MatchbookClient(ReadOnlyVenue):
    """Read-only Matchbook market-data client for Sports Hedge Phase 1."""

    name = VenueName.MATCHBOOK

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
        monotonic_clock: Callable[[], float] | None = None,
        login_cooldown_seconds: float | None = None,
        login_cooldown_max_seconds: float | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._login_cooldown = ProviderCooldown(
            RateLimitPolicy(
                fallback_seconds=(
                    DEFAULT_LOGIN_COOLDOWN_SECONDS
                    if login_cooldown_seconds is None
                    else float(login_cooldown_seconds)
                ),
                max_seconds=(
                    MAX_LOGIN_COOLDOWN_SECONDS
                    if login_cooldown_max_seconds is None
                    else float(login_cooldown_max_seconds)
                ),
                provider="Matchbook",
            ),
            monotonic_clock=monotonic_clock,
            wall_clock=self._clock,
        )
        self._client = client or httpx.AsyncClient(
            base_url=settings.matchbook_base_url.rstrip("/"),
            timeout=market_data_http_timeout(),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "sports-hedge/0.1 paper-research",
            },
        )
        self._session_token: str | None = None
        self._auth_fault: MatchbookAuthFaultError | None = None
        self._football_sport_id: int | None = None
        self._american_football_sport_id: int | None = None
        self._login_lock = asyncio.Lock()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def login(self) -> str:
        """Authenticate once under a lock. Existing tokens are reused."""

        self._raise_if_auth_fault()
        self._raise_if_cooling_down()
        async with self._login_lock:
            if self._session_token:
                return self._session_token
            self._raise_if_auth_fault()
            self._raise_if_cooling_down()
            return await self._authenticate()

    async def _authenticate(self) -> str:
        username = self.settings.matchbook_username
        password = self.settings.matchbook_password
        if not username or not password:
            raise MatchbookAuthError(
                "MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD are required for Matchbook API access"
            )

        # Credential payload is never logged. Only status/retry metadata may be reported.
        payload: dict[str, str] = {"username": username, "password": password}
        if self.settings.matchbook_mfa_code:
            payload["mfa-code"] = self.settings.matchbook_mfa_code

        response = await self._client.post(
            MATCHBOOK_SESSION_PATH,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        retry_after = self._login_cooldown.observe_status(
            response.status_code,
            response.headers,
            now=self._clock(),
        )
        if retry_after is not None:
            self._clear_session_token()
            raise MatchbookRateLimitedError(retry_after)
        if response.status_code == 400:
            self._clear_session_token()
            fault = MatchbookAuthFaultError.from_response(
                response,
                secrets=self._login_secrets(),
            )
            self._auth_fault = fault
            raise fault
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

    def _login_secrets(self) -> tuple[str, ...]:
        return tuple(
            value
            for value in (
                self.settings.matchbook_username,
                self.settings.matchbook_password,
                self.settings.matchbook_mfa_code,
                self._session_token,
            )
            if value
        )

    def _raise_if_cooling_down(self) -> None:
        remaining = self._login_cooldown.remaining_seconds()
        if remaining > 0:
            raise MatchbookRateLimitedError(remaining)

    def _raise_if_auth_fault(self) -> None:
        if self._auth_fault is not None:
            raise self._auth_fault

    def clear_auth_fault(self) -> None:
        """Clear the process-local 400 latch after credentials/MFA are corrected."""

        self._auth_fault = None

    def _clear_session_token(self) -> None:
        self._session_token = None
        self._client.headers.pop("session-token", None)

    async def _ensure_session(self) -> None:
        if self._session_token:
            return
        await self.login()

    async def _reauthenticate(self, rejected_token: str | None) -> None:
        async with self._login_lock:
            if self._session_token and self._session_token != rejected_token:
                return
            self._raise_if_auth_fault()
            self._clear_session_token()
            self._raise_if_cooling_down()
            await self._authenticate()

    async def _get(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_session()
        rejected_token = self._session_token
        response = await self._client.get(path, params=params)
        if response.status_code == 401:
            await self._reauthenticate(rejected_token)
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

    async def resolve_american_football_sport_id(self) -> int:
        if self._american_football_sport_id is not None:
            return self._american_football_sport_id
        sports = await self._list_sports()
        sport_id = select_american_football_sport_id(sports)
        self._american_football_sport_id = sport_id
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
        """List Matchbook markets for one event, paging until complete or capped.

        Official GET /edge/rest/events/{id}/markets defaults to 20 rows. Callers
        that pass ``offset`` receive a single page. The collector still makes one
        list_markets call; extra pages are sequential inside this client.
        """

        per_page = int(filters.get("per-page") or self.settings.matchbook_market_per_page)
        params = {
            **self._market_data_params(),
            "states": "open,suspended",
            "include-prices": "true",
            "price-depth": self.settings.matchbook_price_depth,
            "per-page": per_page,
            **filters,
        }
        params["per-page"] = per_page
        if "offset" in filters:
            page = await self._get(f"/edge/rest/events/{event_id}/markets", params=params)
            return {
                **page,
                "markets": _extract_items(page, "markets"),
                "truncated": False,
            }
        return await self._paginate_markets(event_id, params, per_page=per_page)

    async def _paginate_markets(
        self,
        event_id: int | str,
        params: dict[str, Any],
        *,
        per_page: int,
    ) -> dict[str, Any]:
        markets: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        max_pages = self.settings.matchbook_market_max_pages
        max_markets = max_pages * per_page
        reported_total: int | None = None
        offset = 0
        pages_fetched = 0
        last_page_len = 0
        last_page: dict[str, Any] = {}
        path = f"/edge/rest/events/{event_id}/markets"
        while pages_fetched < max_pages and len(markets) < max_markets:
            page_params = {**params, "offset": offset, "per-page": per_page}
            last_page = await self._get(path, params=page_params)
            reported_total = _optional_int(last_page.get("total")) or reported_total
            page_markets = _extract_items(last_page, "markets")
            last_page_len = len(page_markets)
            pages_fetched += 1
            for item in page_markets:
                market_id = str(item.get("id", "")).strip()
                if market_id and market_id in seen_ids:
                    continue
                if market_id:
                    seen_ids.add(market_id)
                markets.append(item)
            if last_page_len == 0:
                break
            offset += last_page_len
            if reported_total is not None and offset >= reported_total:
                break
            if last_page_len < per_page:
                break

        truncated = len(markets) >= max_markets and (
            reported_total is None or len(markets) < reported_total or last_page_len >= per_page
        )
        if reported_total is not None and len(markets) < reported_total and pages_fetched >= max_pages:
            truncated = True
        if last_page_len < per_page and (reported_total is None or len(markets) >= reported_total):
            truncated = False

        truncation_detail = None
        if truncated:
            truncation_detail = (
                f"Matchbook market list truncated after {len(markets)} markets "
                f"({pages_fetched} pages of {per_page}) for event {event_id}; "
                f"provider total="
                f"{reported_total if reported_total is not None else 'unknown'}"
            )
        return {
            **last_page,
            "offset": 0,
            "per-page": per_page,
            "total": reported_total if reported_total is not None else len(markets),
            "markets": markets,
            "truncated": truncated,
            "truncation-detail": truncation_detail,
        }

    async def get_market(
        self,
        event_id: int | str,
        market_id: int | str,
        **filters: Any,
    ) -> dict[str, Any]:
        """Read-only GET of one known Matchbook market with prices.

        Official path: ``GET /edge/rest/events/{event_id}/markets/{market_id}``.
        HOT uses this for persisted ApprovedEquivalent markets instead of
        ``list_markets(event_id)``. Phase 1 remains market-data only.
        """

        params = {
            **self._market_data_params(),
            "include-prices": "true",
            "price-depth": self.settings.matchbook_price_depth,
            **filters,
        }
        path = f"/edge/rest/events/{event_id}/markets/{market_id}"
        try:
            return await self._get(path, params=params)
        except httpx.HTTPStatusError as exc:
            status_code = int(exc.response.status_code)
            if status_code in {404, 410}:
                raise MatchbookMarketGoneError(event_id, market_id, status_code) from exc
            raise

    async def get_event(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        """Read-only GET of one known Matchbook event, including scores/status.

        Official path: ``GET /edge/rest/events/{event_id}``. Used by PAPER
        settlement to read graded/finished results. Never infers completion
        from elapsed kickoff time. Phase 1 remains market-data only.
        """

        params = {
            **self._market_data_params(),
            "include-prices": "false",
            **filters,
        }
        path = f"/edge/rest/events/{event_id}"
        try:
            return await self._get(path, params=params)
        except httpx.HTTPStatusError as exc:
            status_code = int(exc.response.status_code)
            if status_code in {404, 410}:
                raise MatchbookMarketGoneError(event_id, event_id, status_code) from exc
            raise

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
        if self._closed:
            return
        self._closed = True
        self._clear_session_token()
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


def select_american_football_sport_id(sports: list[dict[str, Any]]) -> int:
    """Pick American Football. Association football / generic 'football' fail closed."""

    matched_ids: set[int] = set()
    matched_names: list[str] = []
    for item in sports:
        if not isinstance(item, dict):
            continue
        name = normalize_text(str(item.get("name", "")))
        if name not in _AMERICAN_FOOTBALL_SPORT_NAMES:
            continue
        sport_id = _optional_int(item.get("id"))
        if sport_id is None:
            raise MatchbookDiscoveryError(
                f"Matchbook American Football sport {name!r} has no numeric id"
            )
        matched_ids.add(sport_id)
        matched_names.append(name)
    if not matched_ids:
        raise MatchbookDiscoveryError(
            "Matchbook lookups/sports did not include an active American Football sport"
        )
    if len(matched_ids) > 1:
        raise MatchbookDiscoveryError(
            "Matchbook lookups/sports returned multiple American Football sport ids: "
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


def get_shared_matchbook_client(settings: Settings | None = None) -> MatchbookClient:
    """Return the process-local Matchbook market-data client, creating it once."""

    global _shared_matchbook_client
    with _shared_client_lock:
        current = _shared_matchbook_client
        if current is None or current.closed:
            _shared_matchbook_client = MatchbookClient(settings or get_settings())
        return _shared_matchbook_client


def set_shared_matchbook_client(client: MatchbookClient | None) -> None:
    """Test helper: install or clear the process-local Matchbook client."""

    global _shared_matchbook_client
    with _shared_client_lock:
        _shared_matchbook_client = client


async def aclose_shared_matchbook_client() -> None:
    """Close the shared client exactly once. Safe to call when none exists."""

    global _shared_matchbook_client
    with _shared_client_lock:
        client = _shared_matchbook_client
        _shared_matchbook_client = None
    if client is not None:
        await client.aclose()


async def reset_shared_matchbook_client() -> None:
    """Drop singleton state so later tests/process recreation start clean.

    A new shared client has no session token, no 429 cooldown, and no 400 latch.
    """

    await aclose_shared_matchbook_client()

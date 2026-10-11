"""Current-IP Polymarket eligibility. Fail closed. Do not cache or spoof it.

Official check: GET https://polymarket.com/api/geoblock
https://docs.polymarket.com/api-reference/geoblock

The response ``blocked`` flag is the decision. This module does not keep a
country allowlist and does not honor proxy environment variables.

A transport failure is UNKNOWN, not proof of ``geoblock_blocked``. Unknown
still denies. Never infer permitted from timeout, connect error, or non-200.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=3.0)
_MAX_ATTEMPTS = 2
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PolymarketEligibility:
    reachable: bool
    blocked: bool | None
    country: str | None
    region: str | None
    permitted: bool
    reason: str
    transport: str | None = None

    def audit(self) -> dict[str, bool | str | None]:
        """Minimal decision. The detected IP is not stored."""

        payload: dict[str, bool | str | None] = {
            "reachable": self.reachable,
            "blocked": self.blocked,
            "country": self.country,
            "region": self.region,
            "reason": self.reason,
        }
        if self.transport:
            payload["transport"] = self.transport
        return payload


def parse_geoblock(payload: Any) -> PolymarketEligibility:
    """Accept only an explicit boolean ``blocked`` flag."""

    if not isinstance(payload, dict):
        return _closed("geoblock_malformed")
    blocked = payload.get("blocked")
    if not isinstance(blocked, bool):
        return _closed("geoblock_ambiguous")
    country = payload.get("country")
    region = payload.get("region")
    if country is not None and not _code(country):
        return _closed("geoblock_ambiguous")
    if region is not None and not isinstance(region, str):
        return _closed("geoblock_ambiguous")
    country_text = None if country is None else str(country).upper()
    region_text = None if region in (None, "") else str(region)
    if blocked:
        return PolymarketEligibility(
            reachable=True,
            blocked=True,
            country=country_text,
            region=region_text,
            permitted=False,
            reason="geoblock_blocked",
        )
    return PolymarketEligibility(
        reachable=True,
        blocked=False,
        country=country_text,
        region=region_text,
        permitted=True,
        reason="geoblock_permitted",
    )


async def fetch_geoblock(url: str, *, client: httpx.AsyncClient | None = None) -> PolymarketEligibility:
    """Ask Polymarket about this request's current IP. Any failure is not permission.

    One bounded retry is used only for timeout/connect failures. HTTP non-200
    is not retried. ``trust_env=False`` so proxy settings cannot change the IP.
    """

    owns = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT, trust_env=False)
    last = _closed("geoblock_unreachable")
    try:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            last = await _probe_once(http, url, attempt=attempt)
            if last.reachable or last.reason == "geoblock_unreachable_http":
                return last
            if attempt >= _MAX_ATTEMPTS:
                return last
        return last
    finally:
        if owns:
            await http.aclose()


async def _probe_once(http: httpx.AsyncClient, url: str, *, attempt: int) -> PolymarketEligibility:
    try:
        response = await http.get(url)
    except httpx.TimeoutException:
        _log_transport("timeout", attempt=attempt)
        return _closed("geoblock_unreachable_timeout", transport="timeout")
    except httpx.NetworkError:
        _log_transport("network", attempt=attempt)
        return _closed("geoblock_unreachable_network", transport="network")
    except httpx.HTTPError:
        _log_transport("http_error", attempt=attempt)
        return _closed("geoblock_unreachable_network", transport="http_error")
    if response.status_code != 200:
        _log_transport("http", attempt=attempt, status=response.status_code)
        return _closed(
            "geoblock_unreachable_http",
            transport=f"http {response.status_code}",
        )
    try:
        body = response.json()
    except ValueError:
        return _closed("geoblock_malformed", transport="malformed_json")
    return parse_geoblock(body)


def _log_transport(stage: str, *, attempt: int, status: int | None = None) -> None:
    LOGGER.warning(
        "polymarket geoblock transport failed stage=%s attempt=%s status=%s",
        stage,
        attempt,
        status if status is not None else "-",
    )


def _closed(reason: str, *, transport: str | None = None) -> PolymarketEligibility:
    return PolymarketEligibility(
        reachable=False,
        blocked=None,
        country=None,
        region=None,
        permitted=False,
        reason=reason,
        transport=transport,
    )


def _code(value: Any) -> bool:
    text = str(value).strip()
    return len(text) == 2 and text.isalpha()

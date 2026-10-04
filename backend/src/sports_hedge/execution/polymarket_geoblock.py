"""Current-IP Polymarket eligibility. Fail closed. Do not cache or spoof it.

Official check: GET https://polymarket.com/api/geoblock
https://docs.polymarket.com/api-reference/geoblock

The response ``blocked`` flag is the decision. This module does not keep a
country allowlist and does not honor proxy environment variables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
_TIMEOUT = httpx.Timeout(3.0)


@dataclass(frozen=True)
class PolymarketEligibility:
    reachable: bool
    blocked: bool | None
    country: str | None
    region: str | None
    permitted: bool
    reason: str

    def audit(self) -> dict[str, bool | str | None]:
        """Minimal decision. The detected IP is not stored."""

        return {
            "reachable": self.reachable,
            "blocked": self.blocked,
            "country": self.country,
            "region": self.region,
            "reason": self.reason,
        }


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
    """Ask Polymarket about this request's current IP. Any failure is not permission."""

    owns = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT, trust_env=False)
    try:
        response = await http.get(url)
    except httpx.HTTPError:
        return _closed("geoblock_unreachable")
    finally:
        if owns:
            await http.aclose()
    if response.status_code != 200:
        return _closed("geoblock_unreachable")
    try:
        body = response.json()
    except ValueError:
        return _closed("geoblock_malformed")
    return parse_geoblock(body)


def _closed(reason: str) -> PolymarketEligibility:
    return PolymarketEligibility(
        reachable=False,
        blocked=None,
        country=None,
        region=None,
        permitted=False,
        reason=reason,
    )


def _code(value: Any) -> bool:
    text = str(value).strip()
    return len(text) == 2 and text.isalpha()

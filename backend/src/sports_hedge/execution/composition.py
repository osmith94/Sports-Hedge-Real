"""Install Wave 2 HTTP transports on the one process execution runtime.

Constructing a transport opens an HTTP client. It does not log in and it does
not submit an order. PAPER mode and disabled execution leave the runtime
without those clients. An explicit ``set_execution_runtime`` installation is
left in place so tests and health keep the same object.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from sports_hedge.config import Settings
from sports_hedge.execution.clients import (
    KalshiExecutionClient,
    MatchbookExecutionClient,
    PolymarketExecutionClient,
)
from sports_hedge.execution.kalshi_http import KalshiHttpExecutionTransport
from sports_hedge.execution.matchbook_http import MatchbookHttpExecutionTransport
from sports_hedge.execution.polymarket_http import PolymarketHttpExecutionTransport
from sports_hedge.execution.polymarket_sdk import wallet_config
from sports_hedge.execution.package import execution_armed
from sports_hedge.execution.runtime import ExecutionRuntime, get_execution_runtime


def bind_process_execution_runtime(settings: Settings) -> ExecutionRuntime:
    """Configure the process runtime from settings when it is not explicit."""

    runtime = get_execution_runtime()
    if runtime.explicit:
        return runtime
    signature = _signature(settings)
    if runtime.configured_signature == signature:
        return runtime
    runtime.matchbook = None
    runtime.kalshi = None
    runtime.polymarket = None
    runtime.configured_signature = signature
    if not execution_armed(settings):
        return runtime
    if _matchbook_ready(settings):
        runtime.matchbook = MatchbookExecutionClient(MatchbookHttpExecutionTransport(settings))
    private_key = _kalshi_private_key(settings)
    if private_key is not None:
        runtime.kalshi = KalshiExecutionClient(
            KalshiHttpExecutionTransport(settings, private_key=private_key)
        )
    if wallet_config(settings) is not None:
        runtime.polymarket = PolymarketExecutionClient(PolymarketHttpExecutionTransport(settings))
    return runtime


def _signature(settings: Settings) -> str:
    material = "\n".join(
        (
            settings.sports_hedge_mode,
            str(settings.sports_hedge_execution_enabled is True),
            (settings.matchbook_username or "").strip(),
            settings.matchbook_password or "",
            settings.matchbook_base_url,
            (settings.kalshi_api_key_id or "").strip(),
            (settings.kalshi_private_key_path or "").strip(),
            settings.resolved_kalshi_base_url(),
            (settings.polymarket_private_key_path or "").strip(),
            (settings.polymarket_funder_address or "").strip(),
            "" if settings.polymarket_signature_type is None else str(settings.polymarket_signature_type),
            _fingerprint(settings.polymarket_api_key or ""),
            _fingerprint(settings.polymarket_api_secret or ""),
            _fingerprint(settings.polymarket_api_passphrase or ""),
            settings.polymarket_geoblock_url,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _matchbook_ready(settings: Settings) -> bool:
    return bool((settings.matchbook_username or "").strip() and (settings.matchbook_password or "").strip())


def _kalshi_private_key(settings: Settings) -> Any | None:
    """Load a PEM key. A missing or unreadable file is not an armed transport."""

    key_id = (settings.kalshi_api_key_id or "").strip()
    path = (settings.kalshi_private_key_path or "").strip()
    if not key_id or not path:
        return None
    file = Path(path)
    if not file.is_file():
        return None
    try:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        return load_pem_private_key(file.read_bytes(), password=None)
    except (OSError, ValueError, TypeError):
        return None

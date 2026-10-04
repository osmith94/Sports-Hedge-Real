"""Open the official Polymarket Python client without deploying a wallet.

``polymarket-client`` 0.12.0 is the client documented for current CLOB orders.
Token orders are signed against exchange protocol version 2.

``SecureClient.create`` calls ``_ensure_wallet_ready`` after ``_create`` and
can broadcast a relayer transaction that deploys a deposit wallet. This module
calls ``SecureClient._create`` only, and only when the installed package is
exactly 0.12.0 and ``_create`` still accepts ``private_key``, ``wallet``,
``credentials``, and ``validate_credentials``. Any other shape raises
``POLYMARKET_NOT_READY`` and does not call ``SecureClient.create``.

An undeployed smart-contract wallet is refused after construction. Credential
derivation hits the CLOB auth API once when no key is configured and no cache
file exists. It does not place an order. Preflight does not derive.
"""

from __future__ import annotations

import importlib.metadata
import inspect
import json
import os
from dataclasses import dataclass
from pathlib import Path

from sports_hedge.config import Settings

POLYMARKET_CLIENT_VERSION = "0.12.0"
NOT_READY = "POLYMARKET_NOT_READY"
_CREATE_PARAMETERS = frozenset({"private_key", "wallet", "credentials", "validate_credentials"})


@dataclass(frozen=True)
class PolymarketWalletConfig:
    private_key: str
    funder: str | None
    signature_type: int | None


class PolymarketClientError(RuntimeError):
    """The official client could not be opened. The message has no key material."""


def wallet_config(settings: Settings) -> PolymarketWalletConfig | None:
    """Read the key file. Incomplete wallet choice returns None."""

    path = (settings.polymarket_private_key_path or "").strip()
    funder = (settings.polymarket_funder_address or "").strip() or None
    signature_type = settings.polymarket_signature_type
    if not path or (funder is None and signature_type is None):
        return None
    file = Path(path)
    if not file.is_file():
        return None
    try:
        text = file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    key = _normalize_key(text)
    if key is None:
        return None
    return PolymarketWalletConfig(private_key=key, funder=funder, signature_type=signature_type)


def configured_api_creds(settings: Settings) -> tuple[str, str, str] | None:
    key = (settings.polymarket_api_key or "").strip()
    secret = (settings.polymarket_api_secret or "").strip()
    passphrase = (settings.polymarket_api_passphrase or "").strip()
    if key and secret and passphrase:
        return key, secret, passphrase
    return None


def cached_api_creds(settings: Settings) -> tuple[str, str, str] | None:
    path = _creds_path(settings)
    if path is None or not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    key = str(payload.get("api_key") or "").strip()
    secret = str(payload.get("api_secret") or "").strip()
    passphrase = str(payload.get("api_passphrase") or "").strip()
    if not key or not secret or not passphrase:
        return None
    return key, secret, passphrase


def _supported_secure_client():
    """Return SecureClient only for the pinned 0.12.0 ``_create`` shape.

    A missing or renamed ``_create``, or a signature without the parameters
    this module passes, fails closed. ``SecureClient.create`` is never called.
    """

    try:
        installed = importlib.metadata.version("polymarket-client")
    except importlib.metadata.PackageNotFoundError as exc:
        raise PolymarketClientError(f"{NOT_READY}: polymarket client is not installed") from exc
    if installed != POLYMARKET_CLIENT_VERSION:
        raise PolymarketClientError(
            f"{NOT_READY}: polymarket-client {installed} is not {POLYMARKET_CLIENT_VERSION}"
        )
    try:
        from polymarket.clients.secure import SecureClient
    except ImportError as exc:
        raise PolymarketClientError(f"{NOT_READY}: polymarket client is not installed") from exc
    factory = getattr(SecureClient, "_create", None)
    if not callable(factory):
        raise PolymarketClientError(f"{NOT_READY}: SecureClient._create is unavailable")
    try:
        parameters = set(inspect.signature(factory).parameters)
    except (TypeError, ValueError) as exc:
        raise PolymarketClientError(
            f"{NOT_READY}: SecureClient._create signature is unsupported"
        ) from exc
    if not _CREATE_PARAMETERS <= parameters:
        raise PolymarketClientError(f"{NOT_READY}: SecureClient._create signature is unsupported")
    return SecureClient


def open_polymarket_client(settings: Settings, *, derive_credentials: bool):
    """Build a SecureClient through ``_create``. ``derive_credentials`` is the only auth write."""

    wallet = wallet_config(settings)
    if wallet is None:
        raise PolymarketClientError(f"{NOT_READY}: wallet configuration is incomplete")
    try:
        from eth_account import Account
        from polymarket.models.clob.api_key import ApiKeyCreds
    except ImportError as exc:
        raise PolymarketClientError(f"{NOT_READY}: polymarket client is not installed") from exc
    secure_client = _supported_secure_client()

    supplied = configured_api_creds(settings) or cached_api_creds(settings)
    if supplied is None and not derive_credentials:
        raise PolymarketClientError(f"{NOT_READY}: L2 API credentials are not configured")
    creds = None if supplied is None else ApiKeyCreds(key=supplied[0], secret=supplied[1], passphrase=supplied[2])
    signer = Account.from_key(wallet.private_key)
    funder = wallet.funder or signer.address
    try:
        client = secure_client._create(  # noqa: SLF001 — create() can deploy a wallet
            private_key=wallet.private_key,
            wallet=funder,
            credentials=creds,
            validate_credentials=creds is not None or derive_credentials,
        )
    except PolymarketClientError:
        raise
    except Exception as exc:
        raise PolymarketClientError(f"{NOT_READY}: authentication failed") from exc
    _require_signature(client, wallet.signature_type)
    _require_deployed(client)
    if supplied is None:
        _store_derived_creds(settings, client)
    return client


def _require_signature(client: object, expected: int | None) -> None:
    from polymarket._internal.wallet import signature_type_for

    actual = signature_type_for(client._ctx.wallet_type)  # noqa: SLF001
    if expected is not None and actual != expected:
        client.close()  # type: ignore[attr-defined]
        raise PolymarketClientError(
            f"{NOT_READY}: signature type {expected} does not match the wallet"
        )


def _require_deployed(client: object) -> None:
    from polymarket._internal.actions.relayer.deployed import fetch_deployed_sync
    from polymarket.clients.secure import _relayer_transaction_type_for_wallet

    wallet_type = client._ctx.wallet_type  # noqa: SLF001
    kind = _relayer_transaction_type_for_wallet(wallet_type)
    if kind is None:
        return
    try:
        deployed = fetch_deployed_sync(
            client._ctx.relayer,  # noqa: SLF001
            address=str(client._ctx.wallet),  # noqa: SLF001
            type=kind,
        )
    except Exception as exc:
        client.close()  # type: ignore[attr-defined]
        raise PolymarketClientError(f"{NOT_READY}: wallet deployment could not be read") from exc
    if not deployed:
        client.close()  # type: ignore[attr-defined]
        raise PolymarketClientError(f"{NOT_READY}: wallet is not deployed")


def _store_derived_creds(settings: Settings, client: object) -> None:
    path = _creds_path(settings)
    creds = client._ctx.credentials  # noqa: SLF001
    if path is None:
        return
    payload = {
        "api_key": creds.key,
        "api_secret": creds.secret,
        "api_passphrase": creds.passphrase,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def _creds_path(settings: Settings) -> Path | None:
    raw = (settings.polymarket_private_key_path or "").strip()
    if not raw:
        return None
    key = Path(raw)
    return key.with_name(key.name + ".clob-creds.json")


def _normalize_key(text: str) -> str | None:
    body = text[2:] if text.lower().startswith("0x") else text
    if len(body) != 64:
        return None
    try:
        int(body, 16)
    except ValueError:
        return None
    try:
        from eth_account import Account

        Account.from_key("0x" + body)
    except Exception:
        return None
    return "0x" + body

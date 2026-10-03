"""Kalshi request signing for Ed25519 and RSA. No venue is contacted."""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sports_hedge.config import Settings
from sports_hedge.execution.composition import _kalshi_private_key, bind_process_execution_runtime
from sports_hedge.execution.kalshi_http import (
    KalshiHttpExecutionTransport,
    kalshi_auth_headers,
    kalshi_sign_path,
    sign_kalshi_message,
)
from sports_hedge.execution.runtime import get_execution_runtime, set_execution_runtime

KEY_ID = "kalshi-key-id"
TIMESTAMP = "1703123456789"
SIGN_PATH = "/trade-api/v2/portfolio/orders"
MESSAGE = f"{TIMESTAMP}GET{SIGN_PATH}".encode()
NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)


def _headers(private_key: object, *, path: str = "/portfolio/orders?limit=5") -> dict[str, str]:
    sign_path = kalshi_sign_path("https://external-api.kalshi.com/trade-api/v2", path)
    return kalshi_auth_headers(
        key_id=KEY_ID,
        private_key=private_key,
        timestamp_ms=TIMESTAMP,
        method="get",
        sign_path=sign_path,
    )


def test_ed25519_signature_verifies_and_ignores_the_query() -> None:
    key = Ed25519PrivateKey.generate()
    headers = _headers(key)
    assert kalshi_sign_path(
        "https://external-api.kalshi.com/trade-api/v2",
        "/portfolio/orders?limit=5",
    ) == SIGN_PATH
    assert headers["KALSHI-ACCESS-KEY"] == KEY_ID
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == TIMESTAMP
    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    key.public_key().verify(signature, MESSAGE)
    key.public_key().verify(sign_kalshi_message(key, MESSAGE), MESSAGE)
    assert b"?" not in MESSAGE


def test_rsa_signature_still_verifies_the_same_text() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    headers = _headers(key, path="/portfolio/orders?cursor=abc&limit=5")
    assert headers["KALSHI-ACCESS-KEY"] == KEY_ID
    assert headers["KALSHI-ACCESS-TIMESTAMP"] == TIMESTAMP
    key.public_key().verify(
        base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        MESSAGE,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_unsupported_private_key_fails_closed() -> None:
    class Unsupported:
        def sign(self, *_args: object) -> bytes:
            return b"not-used"

    with pytest.raises(TypeError, match="Unsupported Kalshi private key type"):
        sign_kalshi_message(Unsupported(), MESSAGE)


def test_ed25519_pem_loads_into_the_execution_transport(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("loading an Ed25519 key submitted an order")

    monkeypatch.setattr(httpx.AsyncClient, "request", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "post", refuse)
    key = Ed25519PrivateKey.generate()
    pem = tmp_path / "kalshi.pem"
    pem.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    settings = Settings(
        sports_hedge_mode="real",
        sports_hedge_execution_enabled=True,
        kalshi_api_key_id=KEY_ID,
        kalshi_private_key_path=str(pem),
    )
    set_execution_runtime(None)
    try:
        loaded = _kalshi_private_key(settings)
        assert isinstance(loaded, ed25519.Ed25519PrivateKey)
        loaded.public_key().verify(sign_kalshi_message(loaded, MESSAGE), MESSAGE)
        runtime = bind_process_execution_runtime(settings)
        assert runtime.kalshi is not None
        assert isinstance(runtime.kalshi._transport._private_key, ed25519.Ed25519PrivateKey)  # type: ignore[union-attr]
        assert get_execution_runtime().kalshi is runtime.kalshi
    finally:
        set_execution_runtime(None)


@pytest.mark.asyncio
async def test_execution_transport_signs_ed25519_without_a_live_call() -> None:
    key = Ed25519PrivateKey.generate()
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(400, json={"error": "mock"})

    transport = KalshiHttpExecutionTransport(
        Settings(
            sports_hedge_mode="real",
            sports_hedge_execution_enabled=True,
            kalshi_api_key_id=KEY_ID,
        ),
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            base_url="https://external-api.kalshi.com/trade-api/v2",
        ),
        clock=lambda: NOW,
        private_key=key,
    )
    response = await transport._send(
        "GET",
        "/portfolio/orders",
        params={"cursor": "next", "limit": "5"},
    )
    await transport.aclose()
    assert response.status_code == 400
    request = seen[0]
    assert request.url.params["cursor"] == "next"
    assert request.url.params["limit"] == "5"
    assert request.url.path == "/trade-api/v2/portfolio/orders"
    timestamp = request.headers["KALSHI-ACCESS-TIMESTAMP"]
    signed = f"{timestamp}GET{SIGN_PATH}".encode()
    assert b"?" not in signed
    assert b"cursor" not in signed
    assert b"limit" not in signed
    key.public_key().verify(
        base64.b64decode(request.headers["KALSHI-ACCESS-SIGNATURE"]),
        signed,
    )

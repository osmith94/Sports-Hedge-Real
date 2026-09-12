from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.config import Settings, parse_cors_allow_origins


CONSOLE = "http://localhost:3000"
DENIED = "https://evil.example"


def test_parse_cors_allowlist_rejects_wildcard() -> None:
    with pytest.raises(ValueError, match="wildcard"):
        parse_cors_allow_origins("*")
    with pytest.raises(ValueError, match="wildcard"):
        Settings(cors_allow_origins=["http://localhost:3000", "*"])


def test_parse_cors_allowlist_accepts_comma_separated_origins() -> None:
    assert parse_cors_allow_origins("http://localhost:3000, http://127.0.0.1:3000") == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


def test_settings_reads_env_example_comma_separated_cors_allow_origins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EnvSettingsSource must accept the documented .env.example value.

    pydantic-settings JSON-decodes list env fields unless NoDecode is set.
    """

    documented = "http://localhost:3000,http://127.0.0.1:3000"
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", documented)
    assert os.environ["CORS_ALLOW_ORIGINS"] == documented
    settings = Settings()
    assert settings.cors_allow_origins == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


def test_settings_reads_json_array_cors_allow_origins_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CORS_ALLOW_ORIGINS",
        '["http://localhost:3000","http://127.0.0.1:3000"]',
    )
    settings = Settings()
    assert settings.cors_allow_origins == [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]


def test_settings_rejects_wildcard_cors_allow_origins_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "*")
    with pytest.raises(ValueError, match="wildcard"):
        Settings()


def test_preflight_collect_allows_configured_console_origin() -> None:
    client = TestClient(app)
    response = client.options(
        "/paper/collect",
        headers={
            "Origin": CONSOLE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers.get("access-control-allow-origin") == CONSOLE
    allowed_methods = response.headers.get("access-control-allow-methods", "")
    assert "POST" in allowed_methods.upper()
    allowed_headers = response.headers.get("access-control-allow-headers", "").lower()
    assert "content-type" in allowed_headers


def test_preflight_collect_denies_unlisted_origin() -> None:
    client = TestClient(app)
    response = client.options(
        "/paper/collect",
        headers={
            "Origin": DENIED,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert response.status_code == 400
    assert "access-control-allow-origin" not in response.headers


def test_actual_health_response_echoes_permitted_origin_only() -> None:
    client = TestClient(app)
    permitted = client.get("/health", headers={"Origin": CONSOLE})
    assert permitted.status_code == 200
    assert permitted.json()["execution_enabled"] is False
    assert permitted.json()["mode"] == "paper"
    assert permitted.headers.get("access-control-allow-origin") == CONSOLE

    denied = client.get("/health", headers={"Origin": DENIED})
    assert denied.status_code == 200
    assert "access-control-allow-origin" not in denied.headers

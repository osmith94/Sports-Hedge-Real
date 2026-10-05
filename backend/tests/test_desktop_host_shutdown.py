"""SportsHedge.exe desktop-host shutdown contract.

The shutdown route exists only when ``sports_hedge.api.desktop_host`` mounts it,
requires desktop mode plus a per-launch secret, is loopback-only, and asks
uvicorn to exit gracefully so the FastAPI lifespan cleanup still runs.
"""

from __future__ import annotations

import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sports_hedge.api import main as api_main
from sports_hedge.api.desktop_control import (
    DESKTOP_MODE_ENV,
    DESKTOP_SESSION_ID_ENV,
    DESKTOP_SHUTDOWN_TOKEN_ENV,
    DesktopShutdownConfig,
    authorize_desktop_shutdown,
    build_desktop_control_router,
)
from sports_hedge.api.desktop_host import (
    DesktopHostConfigError,
    build_desktop_server,
    main as desktop_host_main,
    validate_bind_host,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
TOKEN = "t" * 43
ENABLED_ENV = {
    DESKTOP_MODE_ENV: "1",
    DESKTOP_SHUTDOWN_TOKEN_ENV: TOKEN,
    DESKTOP_SESSION_ID_ENV: "session-public-id",
}


def _enabled() -> DesktopShutdownConfig:
    return DesktopShutdownConfig.from_env(ENABLED_ENV)


def _app_with_router(config: DesktopShutdownConfig, calls: list[str]) -> FastAPI:
    app = FastAPI()
    app.include_router(build_desktop_control_router(config, lambda: calls.append("exit")))
    return app


def _loopback_client(app: FastAPI) -> TestClient:
    return TestClient(app, client=("127.0.0.1", 50000))


def test_desktop_shutdown_disabled_by_default() -> None:
    assert DesktopShutdownConfig.from_env({}).enabled is False
    assert DesktopShutdownConfig.from_env({DESKTOP_SHUTDOWN_TOKEN_ENV: TOKEN}).enabled is False
    assert DesktopShutdownConfig.from_env({DESKTOP_MODE_ENV: "1"}).enabled is False
    short = {DESKTOP_MODE_ENV: "1", DESKTOP_SHUTDOWN_TOKEN_ENV: "short"}
    assert DesktopShutdownConfig.from_env(short).enabled is False
    decision = authorize_desktop_shutdown(
        DesktopShutdownConfig.disabled(), f"Bearer {TOKEN}", "127.0.0.1"
    )
    assert (decision.allowed, decision.status_code) == (False, 404)


def test_normal_api_app_has_no_shutdown_route() -> None:
    paths = {getattr(route, "path", None) for route in api_main.app.routes}
    assert "/desktop/shutdown" not in paths
    assert not any("shutdown" in str(path) for path in paths)
    client = TestClient(api_main.app, client=("127.0.0.1", 50000))
    response = client.post("/desktop/shutdown", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code in (404, 405)


def test_disabled_router_rejects_even_valid_looking_token() -> None:
    calls: list[str] = []
    app = _app_with_router(DesktopShutdownConfig.disabled(), calls)
    response = _loopback_client(app).post(
        "/desktop/shutdown", headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert response.status_code == 404
    assert calls == []


def test_missing_token_rejected() -> None:
    calls: list[str] = []
    client = _loopback_client(_app_with_router(_enabled(), calls))
    assert client.post("/desktop/shutdown").status_code == 401
    assert client.post("/desktop/shutdown", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.post("/desktop/shutdown", headers={"Authorization": TOKEN}).status_code == 401
    assert calls == []


def test_invalid_token_rejected() -> None:
    calls: list[str] = []
    client = _loopback_client(_app_with_router(_enabled(), calls))
    response = client.post("/desktop/shutdown", headers={"Authorization": "Bearer " + "x" * 43})
    assert response.status_code == 403
    assert response.json()["detail"] == "invalid_token"
    assert TOKEN not in response.text
    assert calls == []


def test_valid_token_accepted_only_in_desktop_mode_and_is_idempotent() -> None:
    calls: list[str] = []
    client = _loopback_client(_app_with_router(_enabled(), calls))
    first = client.post("/desktop/shutdown", headers={"Authorization": f"Bearer {TOKEN}"})
    second = client.post("/desktop/shutdown", headers={"Authorization": f"Bearer {TOKEN}"})
    assert first.status_code == 202
    assert first.json()["already_requested"] is False
    assert second.status_code == 202
    assert second.json()["already_requested"] is True
    assert calls == ["exit"]
    assert TOKEN not in first.text


def test_shutdown_control_is_loopback_only() -> None:
    calls: list[str] = []
    app = _app_with_router(_enabled(), calls)
    remote = TestClient(app, client=("192.168.1.20", 50000))
    response = remote.post("/desktop/shutdown", headers={"Authorization": f"Bearer {TOKEN}"})
    assert response.status_code == 403
    assert response.json()["detail"] == "loopback_only"
    assert remote.get("/desktop/session").status_code == 403
    assert calls == []
    ipv6 = authorize_desktop_shutdown(_enabled(), f"Bearer {TOKEN}", "::1")
    assert ipv6.allowed is True


def test_session_route_exposes_public_session_id_but_never_the_secret() -> None:
    client = _loopback_client(_app_with_router(_enabled(), []))
    body = client.get("/desktop/session").json()
    assert body == {"desktop_mode": True, "session_id": "session-public-id"}
    assert TOKEN not in repr(_enabled())


def test_desktop_host_refuses_without_desktop_mode_or_secret(capsys) -> None:
    assert desktop_host_main(["--port", "0"], env={}) == 2
    assert "desktop_host_refused" in capsys.readouterr().err
    with pytest.raises(DesktopHostConfigError):
        build_desktop_server(FastAPI(), DesktopShutdownConfig.disabled())


def test_desktop_host_refuses_non_loopback_bind() -> None:
    for host in ("0.0.0.0", "::", "192.168.1.20", "example.com"):
        with pytest.raises(DesktopHostConfigError):
            validate_bind_host(host)
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert desktop_host_main(["--host", "0.0.0.0"], env=ENABLED_ENV) == 2


def test_fastapi_lifespan_path_remains_intact() -> None:
    assert api_main.app.router.lifespan_context is not None
    source = (REPO_ROOT / "backend/src/sports_hedge/api/main.py").read_text(encoding="utf-8")
    for step in (
        "await schedule.start()",
        "await coordinator.start_server_loop(",
        "await coordinator.stop_server_loop()",
        "await schedule.stop()",
        "await aclose_shared_provider_runtime()",
        "await aclose_shared_matchbook_client()",
    ):
        assert step in source
    assert source.index("await schedule.start()") < source.index(
        "await coordinator.start_server_loop("
    )
    assert source.index("await coordinator.stop_server_loop()") < source.index(
        "await schedule.stop()"
    )
    host_source = (REPO_ROOT / "backend/src/sports_hedge/api/desktop_host.py").read_text(
        encoding="utf-8"
    )
    assert "server.should_exit = True" in host_source
    assert 'lifespan="on"' in host_source
    assert "os._exit" not in host_source
    assert "sys.exit(" not in host_source


def test_authenticated_shutdown_runs_lifespan_cleanup_on_real_uvicorn() -> None:
    events: list[str] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        events.append("startup")
        try:
            yield
        finally:
            events.append("lifespan_cleanup")

    app = FastAPI(lifespan=lifespan)
    server = build_desktop_server(app, _enabled(), host="127.0.0.1", port=0)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    port = server.servers[0].sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    rejected = httpx.post(f"{base}/desktop/shutdown", timeout=5)
    assert rejected.status_code == 401
    wrong = httpx.post(
        f"{base}/desktop/shutdown", headers={"Authorization": "Bearer " + "y" * 43}, timeout=5
    )
    assert wrong.status_code == 403
    assert server.should_exit is False

    accepted = httpx.post(
        f"{base}/desktop/shutdown", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=5
    )
    assert accepted.status_code == 202
    thread.join(timeout=20)
    assert not thread.is_alive(), "uvicorn did not exit after authenticated shutdown"
    assert events == ["startup", "lifespan_cleanup"]

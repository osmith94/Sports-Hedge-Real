"""Programmatic uvicorn host used only by SportsHedge.exe (desktop controller).

    python -m sports_hedge.api.desktop_host --host 127.0.0.1 --port 8000

Differences from ``uvicorn sports_hedge.api.main:app``:

- refuses to start unless desktop mode and a per-launch shutdown secret are
  present in the environment;
- refuses non-loopback bind addresses;
- mounts the authenticated ``POST /desktop/shutdown`` route, which sets
  ``server.should_exit = True`` so uvicorn performs its normal graceful
  shutdown and the FastAPI lifespan cleanup in ``sports_hedge.api.main`` runs
  (accounting schedule, live-refresh coordinator, shared provider runtime,
  shared Matchbook client).

Paper-only settings are enforced by the controller environment and by
``sports_hedge.config``; this module does not change them.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Mapping, Sequence

import uvicorn
from fastapi import FastAPI

from sports_hedge.api.desktop_control import (
    DesktopShutdownConfig,
    build_desktop_control_router,
    is_loopback_host,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS = 10


class DesktopHostConfigError(RuntimeError):
    pass


def validate_bind_host(host: str) -> str:
    if host == "localhost" or is_loopback_host(host):
        return host
    raise DesktopHostConfigError(f"desktop host must bind a loopback address, got {host!r}")


def build_desktop_server(
    app: FastAPI,
    config: DesktopShutdownConfig,
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> uvicorn.Server:
    if not config.enabled:
        raise DesktopHostConfigError(
            "desktop host requires SPORTS_HEDGE_DESKTOP_MODE=1 and a per-launch "
            "SPORTS_HEDGE_DESKTOP_SHUTDOWN_TOKEN supplied by SportsHedge.exe"
        )
    validate_bind_host(host)
    uv_config = uvicorn.Config(
        app,
        host=host,
        port=port,
        lifespan="on",
        log_level="info",
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_TIMEOUT_SECONDS,
    )
    server = uvicorn.Server(uv_config)

    def request_exit() -> None:
        server.should_exit = True

    app.include_router(build_desktop_control_router(config, request_exit))
    return server


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sports_hedge.api.desktop_host")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    config = DesktopShutdownConfig.from_env(os.environ if env is None else env)
    try:
        validate_bind_host(args.host)
        if not config.enabled:
            raise DesktopHostConfigError(
                "SPORTS_HEDGE_DESKTOP_MODE=1 and a per-launch shutdown secret are required"
            )
    except DesktopHostConfigError as exc:
        print(f"desktop_host_refused reason={exc}", file=sys.stderr, flush=True)
        return 2

    from sports_hedge.api.main import app

    server = build_desktop_server(app, config, host=args.host, port=args.port)
    print(
        f"desktop_host_starting host={args.host} port={args.port} "
        f"session_id={config.session_id}",
        file=sys.stderr,
        flush=True,
    )
    server.run()
    print("desktop_host_stopped server_run_returned=true", file=sys.stderr, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

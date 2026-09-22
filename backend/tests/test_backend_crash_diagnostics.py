"""Crash diagnostics around API startup and FastAPI lifespan.

Data class: in-process event-loop phase marks and stubbed lifespan services.
Not live, historical, or modelled quotes. PAPER / read-only boundaries are
unchanged. Scanner scheduling is not started.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import sys
import time
from pathlib import Path

import pytest

from sports_hedge.application import crash_diagnostics
from sports_hedge.application.event_loop_activity import (
    close_loop_slice,
    mark_loop_phase,
    reset_loop_activity,
)
from sports_hedge.application.serving_build import ServingBuildInfo
from sports_hedge.api import main as main_api

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGGER_NAME = "sports_hedge.application.crash_diagnostics"


def _fatal_messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == LOGGER_NAME and record.getMessage().startswith("backend_fatal")
    ]


def _mark_distinct_phases() -> None:
    reset_loop_activity()
    mark_loop_phase(
        lane="universe",
        phase="partition",
        shard_id="epl",
        events=4,
        candidates=2,
    )
    time.sleep(0.01)
    close_loop_slice()
    mark_loop_phase(lane="hot", phase="scheduler", shard_id="hot-1", events=1, candidates=0)


class _Coordinator:
    def bind_universe_checkpoint_store(self, store: object) -> None:
        return None

    def configure_from_settings(self) -> None:
        return None

    async def start_server_loop(self, tick: object) -> None:
        return None

    async def stop_server_loop(self) -> None:
        return None


class _FailingCoordinator(_Coordinator):
    async def start_server_loop(self, tick: object) -> None:
        raise RuntimeError("lifespan escaped")


class _Schedule:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _patch_lifespan(monkeypatch: pytest.MonkeyPatch, coordinator: _Coordinator) -> None:
    async def _aclose() -> None:
        return None

    monkeypatch.setattr(main_api, "get_live_refresh_coordinator", lambda: coordinator)
    monkeypatch.setattr(main_api, "get_universe_checkpoint_store", lambda: object())
    monkeypatch.setattr(main_api, "get_accounting_schedule", lambda: _Schedule())
    monkeypatch.setattr(main_api, "aclose_shared_provider_runtime", _aclose)
    monkeypatch.setattr(main_api, "aclose_shared_matchbook_client", _aclose)
    monkeypatch.setattr(
        crash_diagnostics,
        "get_serving_build_info",
        lambda: ServingBuildInfo(
            git_sha="abc123def",
            git_branch="owner-live",
            repo_root="/repo",
            source="env",
        ),
    )


def test_enable_faulthandler_is_invoked_on_stderr_without_periodic_dumps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    real_enable = crash_diagnostics.faulthandler.enable

    def wrapped(*args: object, **kwargs: object) -> object:
        calls.append((args, kwargs))
        return real_enable(*args, **kwargs)

    monkeypatch.setattr(crash_diagnostics.faulthandler, "enable", wrapped)
    crash_diagnostics.enable_faulthandler()

    assert len(calls) == 1
    assert calls[0][0] == ()
    assert calls[0][1]["file"] is sys.stderr
    assert calls[0][1]["all_threads"] is True
    assert crash_diagnostics.faulthandler.is_enabled()
    module_source = inspect.getsource(crash_diagnostics)
    assert "dump_traceback_later" not in module_source
    assert "dump_traceback_later" not in inspect.getsource(main_api)


def test_api_startup_enables_faulthandler() -> None:
    source = inspect.getsource(main_api)
    assert "\nenable_faulthandler()\n" in source
    assert "enable_faulthandler()" in inspect.getsource(main_api.lifespan)


def test_windows_launcher_keeps_stderr_on_backend_err_log() -> None:
    script = (REPO_ROOT / "scripts" / "windows" / "Start-SportsHedge-Demo.ps1").read_text(
        encoding="utf-8"
    )
    assert 'Join-Path $Logs "demo-backend.err.log"' in script
    assert "-RedirectStandardError $BackendErr" in script
    runbook = (REPO_ROOT / "docs" / "DEMO_RUNBOOK.md").read_text(encoding="utf-8")
    assert "logs\\demo-backend.err.log" in runbook


@pytest.mark.asyncio
async def test_unhandled_async_exception_logs_loop_phase_and_chains(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _mark_distinct_phases()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    chained: list[dict[str, object]] = []
    logged_before_chain: list[str] = []

    def previous(active: asyncio.AbstractEventLoop, context: dict[str, object]) -> None:
        logged_before_chain.extend(
            record.getMessage()
            for record in caplog.records
            if record.getMessage().startswith("unhandled_async_exception")
        )
        chained.append({"loop_is_running": active is loop, **dict(context)})

    async def boom() -> None:
        raise RuntimeError("async-boom")

    task = asyncio.create_task(boom(), name="scanner-tick")
    with pytest.raises(RuntimeError, match="async-boom"):
        await task

    loop.set_exception_handler(previous)
    crash_diagnostics.install_asyncio_exception_handler(loop)
    context = {
        "message": "Task exception was never retrieved",
        "exception": RuntimeError("async-boom"),
        "task": task,
    }
    try:
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            loop.call_exception_handler(context)
    finally:
        loop.set_exception_handler(previous_handler)
        reset_loop_activity()

    assert len(chained) == 1
    assert chained[0]["loop_is_running"] is True
    assert chained[0]["message"] == "Task exception was never retrieved"
    assert chained[0]["task"] is task
    assert logged_before_chain
    assert "event_loop_current_phase=scheduler" in logged_before_chain[0]
    assert "event_loop_longest_sync_phase=partition" in logged_before_chain[0]
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("unhandled_async_exception")
    ]
    assert len(messages) == 1
    text = messages[0]
    assert "async-boom" in text
    assert "scanner-tick" in text
    assert "event_loop_current_lane=hot" in text
    assert "event_loop_current_phase=scheduler" in text
    assert "event_loop_longest_sync_lane=universe" in text
    assert "event_loop_longest_sync_phase=partition" in text
    assert "event_loop_longest_sync_shard=epl" in text


@pytest.mark.asyncio
async def test_unhandled_async_exception_chains_to_default_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    calls: list[dict[str, object]] = []

    def default_handler(context: dict[str, object]) -> None:
        calls.append(context)

    monkeypatch.setattr(loop, "default_exception_handler", default_handler)
    loop.set_exception_handler(None)
    try:
        crash_diagnostics.install_asyncio_exception_handler(loop)
        context = {"message": "unhandled", "exception": RuntimeError("default-chain")}
        loop.call_exception_handler(context)
    finally:
        loop.set_exception_handler(previous_handler)
        reset_loop_activity()

    assert len(calls) == 1
    assert calls[0]["message"] == "unhandled"


@pytest.mark.asyncio
async def test_normal_lifespan_shutdown_does_not_emit_backend_fatal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_lifespan(monkeypatch, _Coordinator())
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    try:
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            async with main_api.lifespan(main_api.app):
                current = loop.get_exception_handler()
                assert current is not None
                assert current is not previous_handler
        assert loop.get_exception_handler() is previous_handler
    finally:
        loop.set_exception_handler(previous_handler)
        reset_loop_activity()

    assert _fatal_messages(caplog) == []


@pytest.mark.asyncio
async def test_escaping_lifespan_exception_emits_one_backend_fatal(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _patch_lifespan(monkeypatch, _FailingCoordinator())
    _mark_distinct_phases()
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    try:
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            with pytest.raises(RuntimeError, match="lifespan escaped"):
                async with main_api.lifespan(main_api.app):
                    raise AssertionError("lifespan yielded after startup failure")
        assert loop.get_exception_handler() is previous_handler
    finally:
        loop.set_exception_handler(previous_handler)
        reset_loop_activity()

    messages = _fatal_messages(caplog)
    assert len(messages) == 1
    text = messages[0]
    assert "exception_type=RuntimeError" in text
    assert "lifespan escaped" in text
    assert "git_sha=abc123def" in text
    assert "git_branch=owner-live" in text
    assert "event_loop_current_phase=scheduler" in text
    assert "event_loop_longest_sync_phase=partition" in text


def test_backend_fatal_still_emits_when_build_info_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def unavailable() -> ServingBuildInfo:
        raise RuntimeError("git unavailable")

    monkeypatch.setattr(crash_diagnostics, "get_serving_build_info", unavailable)
    reset_loop_activity()
    try:
        with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
            crash_diagnostics.record_backend_fatal(KeyError("missing"))
    finally:
        reset_loop_activity()

    messages = _fatal_messages(caplog)
    assert len(messages) == 1
    assert "exception_type=KeyError" in messages[0]
    assert "missing" in messages[0]
    assert "git_sha=None" in messages[0]
    assert "git_branch=None" in messages[0]

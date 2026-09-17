from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.config import (
    Settings,
    canonical_dotenv_path,
    emit_dotenv_operator_diagnostics,
    inspect_dotenv_sources,
    legacy_backend_dotenv_path,
    resolve_repository_root,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_DIR = REPO_ROOT / "backend"
SRC_DIR = BACKEND_DIR / "src"
LAUNCHER = REPO_ROOT / "scripts/windows/Start-SportsHedge-Demo.ps1"

ROOT_USERNAME = "canonical-root-user"
LEGACY_USERNAME = "legacy-backend-user"
CWD_USERNAME = "cwd-dotenv-user"
PROCESS_USERNAME = "process-env-user"
ROOT_PASSWORD = "canonical-root-password-must-never-leak"
LEGACY_PASSWORD = "legacy-backend-password-must-never-leak"
ROOT_MFA = "canonical-mfa-must-never-leak"
LEGACY_MFA = "legacy-mfa-must-never-leak"
SESSION_TOKEN = "session-token-must-never-leak"
SECRET_MARKERS = (
    ROOT_PASSWORD,
    LEGACY_PASSWORD,
    ROOT_MFA,
    LEGACY_MFA,
    SESSION_TOKEN,
)


def _write_dotenv(path: Path, *, username: str, password: str, mfa: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                f"MATCHBOOK_USERNAME={username}",
                f"MATCHBOOK_PASSWORD={password}",
                f"MATCHBOOK_MFA_CODE={mfa}",
                f"MATCHBOOK_SESSION_TOKEN={SESSION_TOKEN}",
                "SPORTS_HEDGE_MODE=paper",
                "SPORTS_HEDGE_EXECUTION_ENABLED=false",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "Sports-Hedge"
    (repo / "backend").mkdir(parents=True)
    (repo / ".env.example").write_text("# template\n", encoding="utf-8")
    _write_dotenv(
        repo / ".env",
        username=ROOT_USERNAME,
        password=ROOT_PASSWORD,
        mfa=ROOT_MFA,
    )
    _write_dotenv(
        repo / "backend" / ".env",
        username=LEGACY_USERNAME,
        password=LEGACY_PASSWORD,
        mfa=LEGACY_MFA,
    )
    return repo


def _clear_matchbook_process_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "MATCHBOOK_USERNAME",
        "MATCHBOOK_PASSWORD",
        "MATCHBOOK_MFA_CODE",
        "MATCHBOOK_SESSION_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)


def _assert_no_secrets(payload: object) -> None:
    blob = json.dumps(payload, default=str) if not isinstance(payload, str) else payload
    for marker in SECRET_MARKERS:
        assert marker not in blob
        assert marker.lower() not in blob.lower()


def test_canonical_dotenv_is_repo_root_and_independent_of_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = (REPO_ROOT / ".env").resolve()
    assert canonical_dotenv_path() == expected
    assert canonical_dotenv_path().is_absolute()
    assert (canonical_dotenv_path().parent / "backend").is_dir()
    assert (canonical_dotenv_path().parent / ".env.example").is_file()
    assert legacy_backend_dotenv_path() == (BACKEND_DIR / ".env").resolve()

    monkeypatch.chdir(REPO_ROOT)
    from_root = canonical_dotenv_path()
    monkeypatch.chdir(BACKEND_DIR)
    from_backend = canonical_dotenv_path()
    monkeypatch.chdir(BACKEND_DIR / "tests")
    from_tests = canonical_dotenv_path()
    assert from_root == from_backend == from_tests == expected


def test_subprocess_repo_root_and_backend_cwd_resolve_the_same_dotenv() -> None:
    script = (
        "from sports_hedge.config import canonical_dotenv_path, inspect_dotenv_sources; "
        "d = inspect_dotenv_sources(); "
        "print(canonical_dotenv_path()); "
        "print(d.canonical_path)"
    )
    env = {**os.environ, "PYTHONPATH": str(SRC_DIR)}
    expected = str((REPO_ROOT / ".env").resolve())
    paths: list[str] = []
    for cwd in (REPO_ROOT, BACKEND_DIR):
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode == 0
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        assert lines == [expected, expected]
        paths.extend(lines)
        combined = result.stdout + result.stderr
        _assert_no_secrets(combined)
    assert set(paths) == {expected}


def test_conflicting_legacy_backend_dotenv_cannot_override_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr("sports_hedge.config.resolve_repository_root", lambda: repo)
    _clear_matchbook_process_env(monkeypatch)
    monkeypatch.chdir(repo / "backend")
    _write_dotenv(
        Path.cwd() / ".env",
        username=CWD_USERNAME,
        password=LEGACY_PASSWORD,
        mfa=LEGACY_MFA,
    )

    settings = Settings()
    assert settings.matchbook_username == ROOT_USERNAME
    assert settings.matchbook_username != LEGACY_USERNAME
    assert settings.matchbook_username != CWD_USERNAME
    assert settings.matchbook_password == ROOT_PASSWORD
    assert settings.matchbook_mfa_code == ROOT_MFA

    diagnostics = inspect_dotenv_sources()
    public = diagnostics.as_public_dict()
    assert diagnostics.canonical_path == str((repo / ".env").resolve())
    assert diagnostics.canonical_exists is True
    assert diagnostics.legacy_backend_exists is True
    assert diagnostics.legacy_backend_ignored is True
    assert diagnostics.warning is not None
    assert str((repo / "backend" / ".env").resolve()) in diagnostics.warning
    assert "scanner_configuration:" in diagnostics.warning
    assert "not a provider outage" in diagnostics.warning
    _assert_no_secrets(public)
    _assert_no_secrets(diagnostics.warning)
    assert ROOT_USERNAME not in diagnostics.warning
    assert LEGACY_USERNAME not in json.dumps(public)


def test_process_environment_overrides_canonical_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr("sports_hedge.config.resolve_repository_root", lambda: repo)
    monkeypatch.setenv("MATCHBOOK_USERNAME", PROCESS_USERNAME)
    monkeypatch.delenv("MATCHBOOK_PASSWORD", raising=False)
    monkeypatch.delenv("MATCHBOOK_MFA_CODE", raising=False)

    settings = Settings()
    assert settings.matchbook_username == PROCESS_USERNAME
    assert settings.matchbook_password == ROOT_PASSWORD


def test_emit_diagnostics_logs_paths_not_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    repo = _fake_repo(tmp_path)
    monkeypatch.setattr("sports_hedge.config.resolve_repository_root", lambda: repo)
    with caplog.at_level("INFO", logger="sports_hedge.config"):
        diagnostics = emit_dotenv_operator_diagnostics(force=True)
    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert str((repo / ".env").resolve()) in messages
    assert str((repo / "backend" / ".env").resolve()) in messages
    assert "ignoring leftover dotenv" in messages
    _assert_no_secrets(messages)
    _assert_no_secrets(diagnostics.as_public_dict())


def test_health_reports_dotenv_paths_without_secrets() -> None:
    body = TestClient(app).get("/health").json()
    assert body["mode"] == "paper"
    assert body["execution_enabled"] is False
    dotenv = body["dotenv"]
    assert dotenv["canonical_path"] == str(canonical_dotenv_path())
    assert dotenv["legacy_backend_path"] == str(legacy_backend_dotenv_path())
    assert dotenv["canonical_exists"] is Path(dotenv["canonical_path"]).is_file()
    assert dotenv["legacy_backend_exists"] is Path(dotenv["legacy_backend_path"]).is_file()
    assert dotenv["legacy_backend_ignored"] is dotenv["legacy_backend_exists"]
    if dotenv["legacy_backend_exists"]:
        assert isinstance(dotenv["warning"], str)
        assert "scanner_configuration:" in dotenv["warning"]
    else:
        assert dotenv["warning"] is None
    _assert_no_secrets(body)


def test_windows_launcher_documents_repo_root_dotenv_and_keeps_backend_cwd() -> None:
    start_ps1 = LAUNCHER.read_text(encoding="utf-8")
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    runbook = (REPO_ROOT / "docs/DEMO_RUNBOOK.md").read_text(encoding="utf-8")

    assert "-WorkingDirectory $backendDir" in start_ps1
    assert 'Join-Path $Root ".env"' in start_ps1
    assert r"backend\.env is ignored" in start_ps1
    assert "Canonical local dotenv" in start_ps1
    assert "MATCHBOOK_PASSWORD" not in start_ps1
    assert "MATCHBOOK_MFA" not in start_ps1
    assert "session-token" not in start_ps1.lower()
    assert "Get-Content $LegacyBackendDotEnv" not in start_ps1
    assert "Get-Content $CanonicalDotEnv" not in start_ps1

    assert "repository root" in readme.lower()
    assert "backend/.env" in readme
    assert "ignored" in readme.lower()
    assert "Canonical local dotenv" in example
    assert "backend/.env is not read" in example
    assert "repository-root `.env`" in runbook
    assert "`backend/.env` is ignored" in runbook
    _assert_no_secrets(start_ps1)

    # Launcher still starts uvicorn with backend cwd; Settings still targets root .env.
    assert canonical_dotenv_path() == (resolve_repository_root() / ".env").resolve()
    assert canonical_dotenv_path().parent == REPO_ROOT.resolve()


def test_config_does_not_use_cwd_relative_env_file() -> None:
    source = (SRC_DIR / "sports_hedge/config.py").read_text(encoding="utf-8")
    assert not any(line.strip() == 'env_file=".env",' for line in source.splitlines())
    assert "canonical_dotenv_path()" in source
    assert "settings_customise_sources" in source

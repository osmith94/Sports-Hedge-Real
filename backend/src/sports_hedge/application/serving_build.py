"""Frozen serving Git identity for runtime /build-info and /health.

Captured once per process. Launcher-exported env wins over a live git read so a
later checkout change cannot make a stale process claim the new SHA.
"""

from __future__ import annotations

from dataclasses import dataclass
from os import environ
from pathlib import Path
from subprocess import DEVNULL, CalledProcessError, TimeoutExpired, check_output
from typing import Literal, Mapping

from sports_hedge.config import resolve_repository_root

BuildSource = Literal["env", "git", "unavailable"]


@dataclass(frozen=True)
class ServingBuildInfo:
    git_sha: str | None
    git_branch: str | None
    repo_root: str | None
    source: BuildSource

    def as_public_dict(self) -> dict[str, object]:
        return {
            "git_sha": self.git_sha,
            "git_branch": self.git_branch,
            "source": self.source,
            "data_kind": "runtime_build_identity",
        }


_SNAPSHOT: ServingBuildInfo | None = None


def reset_serving_build_info() -> None:
    """Drop the process snapshot. Tests use this after env patches."""

    global _SNAPSHOT
    _SNAPSHOT = None


def get_serving_build_info(*, refresh: bool = False) -> ServingBuildInfo:
    """Return the frozen serving identity, capturing it on first use."""

    global _SNAPSHOT
    if _SNAPSHOT is not None and not refresh:
        return _SNAPSHOT
    _SNAPSHOT = capture_serving_build_info()
    return _SNAPSHOT


def capture_serving_build_info(
    *,
    environ_map: Mapping[str, str] | None = None,
) -> ServingBuildInfo:
    """Read serving identity from launcher env, else git, else unavailable."""

    env = environ if environ_map is None else environ_map
    env_sha = _clean(env.get("SPORTS_HEDGE_GIT_SHA"))
    env_branch = _clean(env.get("SPORTS_HEDGE_GIT_BRANCH"))
    env_root = _clean(env.get("SPORTS_HEDGE_REPO_ROOT"))
    if env_sha:
        return ServingBuildInfo(
            git_sha=env_sha,
            git_branch=env_branch,
            repo_root=env_root,
            source="env",
        )

    repo_root = Path(env_root) if env_root else _try_repository_root()
    if repo_root is None:
        return ServingBuildInfo(None, env_branch, env_root, "unavailable")
    sha = _git_text(repo_root, "rev-parse", "HEAD")
    branch = env_branch or _git_text(repo_root, "rev-parse", "--abbrev-ref", "HEAD")
    if not sha:
        return ServingBuildInfo(None, branch, str(repo_root), "unavailable")
    return ServingBuildInfo(sha, branch, str(repo_root), "git")


def _try_repository_root() -> Path | None:
    try:
        return resolve_repository_root()
    except RuntimeError:
        return None


def _git_text(repo_root: Path, *args: str) -> str | None:
    try:
        value = check_output(
            ["git", "-C", str(repo_root), *args],
            text=True,
            timeout=2,
            stderr=DEVNULL,
        )
    except (OSError, CalledProcessError, TimeoutExpired):
        return None
    return _clean(value)


def _clean(value: str | None) -> str | None:
    stripped = (value or "").strip()
    return stripped or None

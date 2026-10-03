from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from json import JSONDecodeError, loads
from logging import getLogger
from os import environ
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    DotEnvSettingsSource,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

LOGGER = getLogger(__name__)

CANONICAL_DOTENV_NAME = ".env"
_PACKAGE_FILE = Path(__file__).resolve()
_DOTENV_DIAGNOSTICS_EMITTED = False

EMPTY_LEGACY_POLYMARKET_SERIES_WARNING = (
    "scanner_configuration: POLYMARKET_GAMMA_SERIES_ID is empty, so Gamma series "
    "filtering is disabled. Championship/La Liga coverage is no longer guaranteed "
    "by the target-series allowlist. Unset the empty legacy variable to restore "
    "target-series filtering, or set POLYMARKET_GAMMA_SERIES_IDS explicitly. "
    "This is configuration, not a provider outage."
)
RETIRED_ALLOCATION_MAX_CONCURRENT_OPEN_WARNING = (
    "allocator_configuration: ALLOCATION_MAX_CONCURRENT_OPEN is retired and ignored. "
    "The standard PAPER allocator no longer rejects on open-opportunity count. "
    "Capital, reserve, venue, same-fixture, depth and risk constraints still apply."
)
_RETIRED_ALLOCATION_MAX_CONCURRENT_OPEN_ENV = "ALLOCATION_MAX_CONCURRENT_OPEN"


def legacy_extra_polymarket_series_warning(single: str, resolved: list[str]) -> str:
    return (
        "scanner_configuration: legacy POLYMARKET_GAMMA_SERIES_ID "
        f"({single}) was merged into POLYMARKET_GAMMA_SERIES_IDS "
        f"({', '.join(resolved)}) rather than replacing the target set. Unset the "
        "legacy variable and add any extra series to POLYMARKET_GAMMA_SERIES_IDS. "
        "This is configuration hygiene, not a provider outage."
    )


def _is_repository_root(candidate: Path) -> bool:
    return (candidate / ".env.example").is_file() and (candidate / "backend").is_dir()


def resolve_repository_root() -> Path:
    """Resolve the git/repository root from this package's location, not cwd.

    ``sports_hedge.config`` lives at ``backend/src/sports_hedge/config.py``.
    Walking upward from that file finds the committed layout that contains
    ``.env.example`` and ``backend/``.
    """

    for candidate in (_PACKAGE_FILE.parent, *_PACKAGE_FILE.parents):
        if _is_repository_root(candidate):
            return candidate
    try:
        return _PACKAGE_FILE.parents[3]
    except IndexError as exc:
        raise RuntimeError(
            "Unable to resolve Sports Hedge repository root from package location"
        ) from exc


def canonical_dotenv_path(*, repository_root: Path | None = None) -> Path:
    """Absolute path of the single canonical local dotenv: repository-root ``.env``."""

    root = repository_root if repository_root is not None else resolve_repository_root()
    return (root / CANONICAL_DOTENV_NAME).resolve()


def legacy_backend_dotenv_path(*, repository_root: Path | None = None) -> Path:
    """Absolute path of the leftover ``backend/.env`` that must not be read."""

    root = repository_root if repository_root is not None else resolve_repository_root()
    return (root / "backend" / CANONICAL_DOTENV_NAME).resolve()


def legacy_backend_dotenv_warning(*, canonical_path: Path, legacy_path: Path) -> str:
    return (
        "scanner_configuration: ignoring leftover dotenv at "
        f"{legacy_path}; canonical local dotenv is {canonical_path}. "
        "Remove or migrate backend/.env so it cannot be mistaken for active "
        "configuration. This is configuration hygiene, not a provider outage."
    )


@dataclass(frozen=True)
class DotenvDiagnostics:
    """Path-level dotenv diagnostic. Never includes secret values."""

    canonical_path: str
    canonical_exists: bool
    legacy_backend_path: str
    legacy_backend_exists: bool
    legacy_backend_ignored: bool
    warning: str | None

    def as_public_dict(self) -> dict[str, object]:
        return {
            "canonical_path": self.canonical_path,
            "canonical_exists": self.canonical_exists,
            "legacy_backend_path": self.legacy_backend_path,
            "legacy_backend_exists": self.legacy_backend_exists,
            "legacy_backend_ignored": self.legacy_backend_ignored,
            "warning": self.warning,
        }


def inspect_dotenv_sources(*, repository_root: Path | None = None) -> DotenvDiagnostics:
    """Report which dotenv path is configured without reading credential contents."""

    canonical = canonical_dotenv_path(repository_root=repository_root)
    legacy = legacy_backend_dotenv_path(repository_root=repository_root)
    legacy_exists = legacy.is_file()
    warning = (
        legacy_backend_dotenv_warning(canonical_path=canonical, legacy_path=legacy)
        if legacy_exists
        else None
    )
    return DotenvDiagnostics(
        canonical_path=str(canonical),
        canonical_exists=canonical.is_file(),
        legacy_backend_path=str(legacy),
        legacy_backend_exists=legacy_exists,
        legacy_backend_ignored=legacy_exists,
        warning=warning,
    )


def emit_dotenv_operator_diagnostics(*, force: bool = False) -> DotenvDiagnostics:
    """Log the canonical dotenv path and any leftover backend/.env warning once."""

    global _DOTENV_DIAGNOSTICS_EMITTED
    diagnostics = inspect_dotenv_sources()
    if force or not _DOTENV_DIAGNOSTICS_EMITTED:
        LOGGER.info(
            "canonical local dotenv path=%s exists=%s",
            diagnostics.canonical_path,
            diagnostics.canonical_exists,
        )
        if diagnostics.warning:
            LOGGER.warning("%s", diagnostics.warning)
        _DOTENV_DIAGNOSTICS_EMITTED = True
    return diagnostics


class Settings(BaseSettings):
    """Runtime configuration.

    The default remains paper with execution disabled. ``real`` names the
    live-execution seam in this repository. Arming that seam requires both
    ``sports_hedge_mode="real"`` and ``sports_hedge_execution_enabled=True``.
    Read-only Matchbook and Kalshi market-data clients stay unchanged either way.
    Paper autofill still uses simulated fills until a later wave wires the seam.
    """

    model_config = SettingsConfigDict(
        env_file_encoding="utf-8",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Load only the repository-root dotenv; process env still wins.

        The default ``env_file=".env"`` source is cwd-relative and must not be
        used. Discard it so a leftover ``backend/.env`` cannot override root.
        """

        _ = dotenv_settings
        return (
            init_settings,
            env_settings,
            DotEnvSettingsSource(
                settings_cls,
                env_file=canonical_dotenv_path(),
                env_file_encoding="utf-8",
            ),
            file_secret_settings,
        )

    sports_hedge_mode: Literal["paper", "real"] = "paper"
    sports_hedge_execution_enabled: bool = False

    matchbook_username: str | None = None
    matchbook_password: str | None = None
    matchbook_mfa_code: str | None = None
    matchbook_base_url: str = "https://api.matchbook.com"
    matchbook_currency: Literal["GBP", "USD", "EUR", "AUD", "CAD", "HKD"] = "GBP"
    matchbook_price_depth: int = Field(default=5, ge=1, le=50)
    matchbook_minimum_liquidity: float = Field(default=2.0, ge=0)
    # Provider-side event paging. sport-ids is resolved via GET /edge/rest/lookups/sports.
    matchbook_event_per_page: int = Field(default=100, ge=1, le=100)
    matchbook_event_max_pages: int = Field(default=10, ge=1, le=50)
    # GET /events/{id}/markets defaults to 20 and is independently paged.
    # Sequential pages stay inside one list_markets call (no extra concurrency).
    matchbook_market_per_page: int = Field(default=100, ge=1, le=100)
    matchbook_market_max_pages: int = Field(default=10, ge=1, le=50)
    matchbook_fixture_lookback_hours: int = Field(default=6, ge=1, le=24)
    # Sunday operator scans must still see the following weekend's PL/Championship
    # cards. 168h is the documented max and the demo-phase default.
    matchbook_fixture_lookahead_hours: int = Field(default=168, ge=1, le=168)

    polymarket_gamma_base_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_base_url: str = "https://clob.polymarket.com"
    # Public Gamma GET /sports (2026-09-16, FA Cup re-checked 2026-09-20):
    # epl=10188, elc=10355, lal=10193, efl=10329 (EFL CUP), efa=10314 (FA Cup;
    # 2026-09-16 snapshot was 10307, which is no longer listed),
    # fif=10238 (FIFA Friendlies), bun=10194 (Bundesliga), sea=10203 (Serie A).
    # Empty single-id override disables series filtering.
    polymarket_gamma_series_id: str | None = None
    polymarket_gamma_series_ids: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "10188",
            "10355",
            "10193",
            "10329",
            "10314",
            "10238",
            "10194",
            "10203",
        ]
    )
    polymarket_gamma_page_limit: int = Field(default=100, ge=1, le=100)
    polymarket_gamma_max_pages_per_series: int = Field(default=5, ge=1, le=20)

    # Public Kalshi Trade API v2 market data. Demo host is opt-in.
    kalshi_base_url: str = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_demo_base_url: str = "https://external-api.demo.kalshi.co/trade-api/v2"
    kalshi_use_demo: bool = False
    # Execution signing material. Empty means the Kalshi execution transport is
    # not configured. The private key file is never read by health.
    kalshi_api_key_id: str | None = None
    kalshi_private_key_path: str | None = None
    kalshi_event_page_limit: int = Field(default=200, ge=1, le=200)
    kalshi_event_max_pages: int = Field(default=10, ge=1, le=50)
    kalshi_series_tickers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [
            "KXEPLGAME",
            "KXEPLBTTS",
            "KXEPLTOTAL",
            "KXEPLFTTS",
            "KXEFLCHAMPIONSHIPGAME",
            "KXEFLCHAMPIONSHIPBTTS",
            "KXEFLCHAMPIONSHIPTOTAL",
            "KXLALIGAGAME",
            "KXLALIGABTTS",
            "KXLALIGATOTAL",
            "KXLALIGAFTTS",
            "KXEFLCUPGAME",
            "KXEFLCUPBTTS",
            "KXEFLCUPTOTAL",
            "KXEFLCUPFTTS",
            "KXFACUPGAME",
            "KXFACUPBTTS",
            "KXFACUPTOTAL",
            "KXFACUPFTTS",
            "KXINTLFRIENDLYGAME",
            "KXINTLFRIENDLYBTTS",
            "KXINTLFRIENDLYTOTAL",
            "KXBUNDESLIGAGAME",
            "KXBUNDESLIGABTTS",
            "KXBUNDESLIGATOTAL",
            "KXBUNDESLIGAFTTS",
            "KXSERIEAGAME",
            "KXSERIEABTTS",
            "KXSERIEATOTAL",
            "KXSERIEAFTTS",
        ]
    )

    market_intelligence_db_path: str = "./data/market_intelligence.sqlite"
    market_intelligence_minimum_sample_size: int = Field(default=8, ge=2)
    event_intelligence_db_path: str = "./data/event_intelligence.sqlite"
    paper_audit_db_path: str = "./data/paper_audit.sqlite"
    paper_liquidity_db_path: str = "./data/paper_liquidity.sqlite"
    paper_ledger_db_path: str = "./data/paper_ledger.sqlite"
    paper_account_fees_db_path: str = "./data/paper_account_fees.sqlite"
    # Application default off. Windows paper demo may enable this for the local
    # process so qualifying LIVE_PAPER decisions auto-capture via
    # persist_triggered_chain. Not a venue order path; fixture replay does not inherit.
    paper_autofill_enabled: bool = False
    # Paper-only automatic unwind after a second revalidation. Default off.
    # Windows launcher must not enable this until the Wave-B lane is reviewed.
    paper_auto_unwind_enabled: bool = False
    watchlist_db_path: str = "./data/near_arb_watchlist.sqlite"
    paper_live_refresh_enabled: bool = False
    paper_live_refresh_interval_seconds: int = Field(default=30, ge=15, le=300)
    paper_live_refresh_hot_interval_seconds: int = Field(default=30, ge=15, le=60)
    # How often the HOT worker checks for due or newly promoted rows.
    # Independent of hot reprice-after (paper_live_refresh_hot_interval_seconds).
    paper_hot_scan_interval_seconds: int = Field(default=10, ge=5, le=60)
    # Fixture radar / membership TTL only. Not BACKGROUND pricing and
    # not the UNIVERSE discovery refresh.
    paper_live_refresh_universe_interval_seconds: int = Field(default=180, ge=60, le=300)
    # How often the BACKGROUND worker checks for due rows. Independent of
    # the per-row reprice age below.
    paper_background_scan_interval_seconds: int = Field(default=10, ge=5, le=60)
    # BACKGROUND per-row reprice age. A row priced at T is due again at
    # T + this value. Operator override is background_reprice_after_seconds
    # (60–600). Legacy background_cadence_seconds migrates here.
    paper_background_price_interval_seconds: int = Field(default=600, ge=60, le=600)
    # After a terminal-complete UNIVERSE generation, wait this long before the
    # next fresh discovery generation. Incomplete chunks/retries do not use this.
    # Operator override is universe_discovery_refresh_seconds (60–3600).
    # Not radar TTL, not intra-generation worker cooldown, and not generation budget.
    paper_universe_discovery_interval_seconds: int = Field(default=3600, ge=60, le=3600)
    # Config-authoritative ACTIVE TRADE exact-ID cadence. Not an operator field
    # in this first pass. Do not discover/rematch on this lane.
    paper_active_trade_interval_seconds: int = Field(default=5, ge=1, le=15)
    # Narrow PAPER settlement/reconciliation cadence over persisted OPEN trades.
    # Sub-cycle of the ACTIVE TRADE worker: no extra create_task, no extra
    # provider concurrency. Never infers results from elapsed kickoff time.
    paper_settlement_interval_seconds: int = Field(default=30, ge=5, le=300)
    # Intra-generation pause only (incomplete chunk yield). Not UNIVERSE
    # discovery cadence and not BACKGROUND pricing.
    paper_universe_worker_cooldown_seconds: int = Field(default=8, ge=5, le=15)
    # Bounded live-scan budgets. A hung provider must not freeze the operator console.
    paper_scan_cycle_timeout_seconds: int = Field(default=45, ge=10, le=180)
    paper_scan_hot_cycle_timeout_seconds: int = Field(default=25, ge=10, le=45)
    # Manual POST /paper/collect is a bounded diagnostic one-shot, not HOT
    # pricing and not UNIVERSE discovery.
    # Keep this strictly below the frontend PAPER_COLLECTION_TIMEOUT_MS (60s) envelope.
    paper_scan_manual_diagnostic_timeout_seconds: int = Field(default=20, ge=10, le=45)
    paper_scan_universe_generation_budget_seconds: int = Field(default=150, ge=30, le=180)
    paper_universe_hot_yield_safety_margin_seconds: float = Field(default=2.0, ge=0.5, le=10)
    paper_hot_pre_kickoff_horizon_minutes: int = Field(default=60, ge=5, le=180)
    paper_hot_post_kickoff_unknown_horizon_hours: int = Field(default=3, ge=1, le=12)
    # Hard football current-radar/HOT membership ceiling after effective kickoff.
    # Scanner membership only; elapsed time must not fabricate completed/closed.
    paper_hot_post_kickoff_current_radar_ceiling_hours: int = Field(default=4, ge=1, le=24)
    paper_hot_current_state_ttl_seconds: int = Field(default=90, ge=30, le=300)
    # Quote / Tracked radar TTL and post-generation idle relationship TTL.
    # Intra-generation ApprovedEquivalent presence is generation-scoped and
    # is not extended by raising this value.
    paper_universe_current_state_ttl_seconds: int = Field(default=360, ge=60, le=900)
    # Lane-specific operator venue defaults. Empty/invalid values keep all three
    # first-class venues on. Persisted operator selections override these.
    paper_hot_venues: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["matchbook", "polymarket", "kalshi"]
    )
    paper_universe_venues: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["matchbook", "polymarket", "kalshi"]
    )
    paper_settings_db_path: str = "./data/paper_settings.sqlite"
    mapping_rules_db_path: str = "./data/mapping_rules.sqlite"
    paper_scan_venue_timeout_seconds: int = Field(default=15, ge=3, le=60)
    paper_scan_provider_timeout_seconds: int = Field(default=8, ge=2, le=30)
    paper_scan_cluster_concurrency: int = Field(default=8, ge=1, le=32)
    paper_scan_matchbook_concurrency: int = Field(default=4, ge=1, le=16)
    paper_scan_polymarket_concurrency: int = Field(default=8, ge=1, le=32)
    paper_scan_kalshi_concurrency: int = Field(default=4, ge=1, le=16)
    # After this many consecutive HOT provider grants while UNIVERSE waits,
    # the next slot goes to UNIVERSE. HOT priority must not starve discovery.
    paper_provider_hot_starvation_grants: int = Field(default=8, ge=1, le=64)
    # Historical attempt counter bound. Transient UNIVERSE provider/network/
    # rate-limit/timeout failures stay RETRY_WAIT with capped backoff; they are
    # not converted to FINAL_FAILED solely because this count is reached.
    paper_universe_work_max_attempts: int = Field(default=3, ge=1, le=8)
    cors_allow_origins: Annotated[list[str], NoDecode] = Field(
        default=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ]
    )
    notifications_db_path: str = "./data/notifications.sqlite"
    notification_cooldown_seconds: int = Field(default=900, ge=0)
    historical_db_path: str = "./data/historical_football.sqlite"
    historical_odds_db_path: str = "./data/historical_odds.sqlite"
    fx_db_path: str = "./data/fx_rates.sqlite"
    fx_check_tolerance_bps: int = Field(default=25, ge=0)
    fx_stale_after_days: int = Field(default=7, ge=1)
    accounting_schedule_enabled: bool = False
    """Default off. Windows paper-demo launcher enables this for local ECB bootstrap only."""

    paper_bankroll_gbp: float = Field(default=5000.0, gt=0)
    paper_bankroll_usd: float = Field(default=5000.0, gt=0)
    paper_bankroll_kalshi_usd: float = Field(default=5000.0, gt=0)
    paper_treasury_seed_gbp: float = Field(default=1000.0, gt=0)
    paper_treasury_demo_usd_gbp_per_unit: float = Field(default=0.80, gt=0)
    paper_treasury_demo_fx_source: str = "paper_demo_fx_snapshot"
    paper_treasury_include_kalshi: bool = True
    min_net_edge: float = Field(default=0.01, ge=0)
    # Owner-configurable outright/season Min Net Arb. None is unconfigured:
    # COMPETITION_SEASON qualification fails closed and never inherits fixture
    # min_net_edge. Do not invent a numeric default here.
    outright_min_net_edge: float | None = Field(default=None, ge=0, lt=1)
    # Deprecated. Runtime PAPER placement uses max_event_gbp, max_opportunity_gbp
    # and max_one_time_gbp. Fresh installs inherit this ceiling for all three.
    max_allocated_per_trade_gbp: float = Field(default=1000.0, gt=0)
    max_event_gbp: float | None = Field(default=None, gt=0)
    max_opportunity_gbp: float | None = Field(default=None, gt=0)
    max_one_time_gbp: float | None = Field(default=None, gt=0)
    max_slippage_bps: int = Field(default=25, ge=0)
    max_event_exposure_gbp: float = Field(default=1000.0, gt=0)
    max_total_exposure_gbp: float = Field(default=5000.0, gt=0)
    max_execution_risk: int = Field(default=60, ge=0, le=100)
    min_mapping_confidence: float = Field(default=0.98, ge=0, le=1)
    # PAPER fixture-identity EventMatcher threshold. Isolated from the class
    # default 0.92 and from deprecated ``minimum_mapping_confidence`` / this
    # mapping-confidence field. Owner-approved #413 experiment default is 0.80.
    paper_event_match_threshold: float = Field(default=0.80, ge=0, le=1)
    simulated_latency_ms: int = Field(default=500, ge=0)
    # Authoritative paper-entry freshness cap. Snapshot age at T1 plus simulated
    # latency must stay strictly below this. Backend dispatch delay is telemetry.
    paper_entry_max_quote_age_ms: int = Field(default=2000, ge=250, le=10000)
    # Maximum latest-minus-earliest retrieval skew for one Price-2 hedge.
    # Default 500 ms is the initial PAPER validation bound. It cannot be set
    # above the 2,000 ms freshness ceiling here; evaluation also clamps it to
    # the active quote-age gate. A wider skew does not relax freshness.
    paper_execution_max_snapshot_skew_ms: int = Field(default=500, ge=0, le=2000)
    simulate_partial_fills: bool = True
    fx_spread_bps: int = Field(default=10, ge=0)

    priority_min_net_edge: float = Field(default=0.03, ge=0)
    priority_min_expected_profit: float = Field(default=20.0, ge=0)
    priority_min_executable_depth: float = Field(default=200.0, ge=0)
    priority_max_quote_age_ms: int = Field(default=2000, ge=0)
    priority_max_execution_risk: int = Field(default=40, ge=0, le=100)
    priority_min_depth_coverage: float = Field(default=1.0, ge=0)
    priority_min_capital_efficiency: float = Field(default=0.03, ge=0)
    priority_safety_haircut: float = Field(default=0.05, ge=0, lt=1)
    priority_operator_manual_cap: float = Field(default=10000.0, gt=0)
    priority_risk_limit: float = Field(default=10000.0, gt=0)

    allocation_min_reserve_fraction: float = Field(default=0.30, ge=0, le=1)
    allocation_min_reserve_amount: float | None = Field(default=None, ge=0)
    allocation_max_pool_fraction_per_opportunity: float = Field(default=0.25, gt=0, le=1)
    allocation_max_open_capital_fraction: float = Field(default=0.70, gt=0, le=1)
    allocation_max_same_fixture_fraction: float = Field(default=0.40, gt=0, le=1)
    # ALLOCATION_MAX_CONCURRENT_OPEN is retired. Leftover env/dotenv values are
    # ignored via extra="ignore" and must not restore an open-count cap.
    allocation_per_opportunity_limit_gbp: float | None = Field(default=1000.0, gt=0)
    allocation_matchbook_limit_gbp: float | None = Field(default=None, gt=0)
    allocation_polymarket_limit_usd: float | None = Field(default=None, gt=0)
    allocation_external_leg_cap_native: float | None = Field(default=None, gt=0)
    allocation_football_regulation_playing_minutes: float = Field(default=90.0, gt=0)
    allocation_football_halftime_minutes: float = Field(default=15.0, ge=0)
    allocation_football_stoppage_settlement_buffer_minutes: float = Field(default=15.0, ge=0)

    @field_validator("cors_allow_origins", mode="before")
    @classmethod
    def split_cors_allow_origins(cls, value: Any) -> list[str]:
        return parse_cors_allow_origins(value)

    @field_validator("polymarket_gamma_series_ids", mode="before")
    @classmethod
    def split_polymarket_series_ids(cls, value: Any) -> list[str]:
        return parse_series_ids(value)

    @field_validator("kalshi_series_tickers", mode="before")
    @classmethod
    def split_kalshi_series_tickers(cls, value: Any) -> list[str]:
        return parse_series_ids(value)

    @field_validator("paper_hot_venues", "paper_universe_venues", mode="before")
    @classmethod
    def split_paper_lane_venues(cls, value: Any) -> list[str]:
        return parse_series_ids(value)

    def resolved_kalshi_base_url(self) -> str:
        if self.kalshi_use_demo:
            return self.kalshi_demo_base_url.rstrip("/")
        return self.kalshi_base_url.rstrip("/")

    def resolved_polymarket_series_ids(self) -> list[str]:
        """Target-series coverage with a safe legacy-ID merge.

        A stale ``POLYMARKET_GAMMA_SERIES_ID`` must not silently replace the
        current target-competition series set. The legacy value is merged into
        the current list. An explicit empty single-id still disables series
        filtering (operator-opt-in) and is warned.
        """

        targets = list(self.polymarket_gamma_series_ids)
        if self.polymarket_gamma_series_id is None:
            return targets
        single = self.polymarket_gamma_series_id.strip()
        if not single:
            return []
        merged: list[str] = []
        for item in [*targets, single]:
            if item and item not in merged:
                merged.append(item)
        return merged

    def polymarket_series_config_warnings(self) -> list[str]:
        """Operator-facing series warnings only. Absent legacy is silent.

        Supported ``POLYMARKET_GAMMA_SERIES_IDS`` is the default path and must
        not emit an obsolete single-series warning merely because it is in use.
        A leftover non-empty legacy ID that is already in the plural set is a
        no-op merge — not an operator warning. Empty or coverage-changing
        legacy values stay explicit.
        """

        if self.polymarket_gamma_series_id is None:
            return []
        single = self.polymarket_gamma_series_id.strip()
        if not single:
            return [EMPTY_LEGACY_POLYMARKET_SERIES_WARNING]
        targets = list(self.polymarket_gamma_series_ids)
        if single in targets:
            return []
        resolved = self.resolved_polymarket_series_ids()
        return [legacy_extra_polymarket_series_warning(single, resolved)]

    @model_validator(mode="after")
    def enforce_phase_one_safety(self) -> Settings:
        if self.sports_hedge_execution_enabled and self.sports_hedge_mode != "real":
            raise ValueError("Live execution is intentionally unavailable in Phase 1")
        # PAPER_LIVE_REFRESH_INTERVAL_SECONDS remains the HOT reprice-after alias.
        # It does not set the HOT scan interval.
        hot_reprice = min(60, max(15, self.paper_live_refresh_interval_seconds))
        self.paper_live_refresh_hot_interval_seconds = hot_reprice
        for warning in self.polymarket_series_config_warnings():
            LOGGER.warning("%s", warning)
        leftover_concurrent = environ.get(_RETIRED_ALLOCATION_MAX_CONCURRENT_OPEN_ENV)
        if leftover_concurrent is not None and leftover_concurrent.strip() != "":
            LOGGER.warning("%s", RETIRED_ALLOCATION_MAX_CONCURRENT_OPEN_WARNING)
        return self


def parse_cors_allow_origins(value: Any) -> list[str]:
    """Restrictive console origin allowlist. Wildcard * is rejected."""

    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = loads(text)
            except JSONDecodeError as exc:
                raise ValueError("CORS_ALLOW_ORIGINS JSON list is invalid") from exc
            return parse_cors_allow_origins(parsed)
        origins = [part.strip() for part in text.split(",") if part.strip()]
    elif isinstance(value, (list, tuple)):
        origins = [str(item).strip() for item in value if str(item).strip()]
    else:
        raise ValueError("CORS_ALLOW_ORIGINS must be a list or comma-separated string")
    if any(origin == "*" for origin in origins):
        raise ValueError("CORS allowlist must not include an unrestricted wildcard")
    return origins


def parse_series_ids(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = loads(text)
            except JSONDecodeError as exc:
                raise ValueError("POLYMARKET_GAMMA_SERIES_IDS JSON list is invalid") from exc
            return parse_series_ids(parsed)
        return [part.strip() for part in text.split(",") if part.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("POLYMARKET_GAMMA_SERIES_IDS must be a list or comma-separated string")


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    emit_dotenv_operator_diagnostics()
    return settings

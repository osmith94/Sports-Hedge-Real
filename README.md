# Sports Hedge

Sports Hedge is an internal football arbitrage research and paper-trading platform.

## Phase 1

Phase 1 is deliberately **read-only and paper-only**. It ingests market data, normalizes football markets across venues, models executable liquidity and costs, detects arbitrage, and simulates fills. It contains no real order-placement capability.

Initial venue plan:

- **Matchbook** — primary official API integration for football prices and liquidity.
- **Polymarket** — permitted public/read-only market data for research and paper simulation; execution disabled.
- **Smarkets** — adapter reserved for a later phase.

The paper-only boundary is enforced in both configuration and the venue interface. Phase 1 has no `place_order` or `cancel_order` methods.

## Repository layout

```text
backend/
  src/sports_hedge/
    api/        internal FastAPI status/control surface
    domain/     canonical market models
    venues/     read-only venue integrations
    facts/      canonical match/team/competition identity
    historical/ football match facts repository (canonical denominator)
    odds/       historical odds observations, coverage, Excel export
  tests/
docs/
  CORE_TENETS.md
  core-tenets/
.github/workflows/
```

## Backend setup

Requires Python 3.12+.

```bash
cd backend
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Copy `.env.example` to `.env` at **repository root**. That root `.env` is the only canonical local dotenv. `sports_hedge.config.Settings` resolves it from the package/repository location, so starting the API from the repo root, from `backend/`, from tests, or from the Windows launcher (which uses `backend\` as the process working directory) all read the same file. Process environment variables still override dotenv values. A leftover `backend/.env` is ignored and is not active configuration; remove or migrate it. `GET /health` reports the configured dotenv paths without credential values. Never commit credentials.

Run tests:

```bash
cd backend
python -m pytest -q
```

Run the internal API:

```bash
cd backend
uvicorn sports_hedge.api.main:app --reload
```

Then inspect:

- `GET /health` — confirms paper mode, execution-disabled state, which dotenv path is configured (path only; no secrets), and the serving Git SHA under `build`.
- `GET /build-info` — cheap runtime serving identity (`git_sha`, `git_branch`, `source`) so soak/UI can prove which checkout is actually running.
- `GET /venues` — shows current venue capability flags.

## One-click local demo (Windows)

Double-click `scripts/windows/Start-SportsHedge-Demo.bat`. It starts the Python backend and Next.js operator console hidden, waits until they are healthy, and opens `/` (Operations Console). Fixture replay remains a labelled advanced/test path at `/demo` and is not the normal operator surface. Fast/Full auto-refresh is server-owned; the browser polls status. Primary **Run scan** posts a bounded current-identity HOT refresh to `/paper/collect/hot`, while broad `/paper/collect` discovery is explicitly labelled **Run full diagnostic** under Advanced and may return partial coverage. A companion `Stop-SportsHedge-Demo.bat` stops only the launcher-started processes after verifying PID command/path identity; a reused PID is not killed, and verified Next.js descendants of a launcher-owned `npm` wrapper are stopped before the PID file is removed. Unrelated Node processes occupying port 3000 are refused rather than killed. To fast-forward the local demo onto latest `owner-live` in one command, run `powershell.exe -ExecutionPolicy Bypass -File .\scripts\windows\Refresh-SportsHedge-Demo.ps1` from the repo root: it stops the owned tree, verifies ports 3000/8000 are gone, `git fetch`/`switch`/`pull --ff-only` `owner-live`, clears only `frontend/.next`, prints the serving branch/SHA, and delegates startup to `Start-SportsHedge-Demo.ps1`. Logs are written under `logs/`. The launcher forces paper mode (`execution_enabled=false`), enables local paper autofill and the read-only live-refresh loop for that process only, and does not add venue write, wallet, or trading-auth capability. It is not a Vercel/cloud deploy.

Requires a local `backend/.venv` with the package installed and Node.js `npm` on PATH. If the backend or frontend is already healthy on ports 8000/3000, the launcher reuses them only when the PID identity belongs to this Sports Hedge launcher, the process command matches, and the recorded repo root plus Git HEAD match the current checkout. A launcher-owned process from a different HEAD is stopped and restarted. An unrelated occupant of those ports is refused rather than killed. The launcher prints the current branch, SHA, and whether each process was reused or restarted. `GET /build-info` (also nested on `GET /health`) reports the serving Git SHA. Matchbook credentials belong in the repository-root `.env` (same file as `.env.example`). The launcher starts FastAPI with working directory `backend\` but does not read `backend/.env`.

## Operator console (two processes)

The documented localhost walkthrough runs FastAPI and Next.js as separate origins. Browser `fetch` from `http://localhost:3000` to `http://localhost:8000` is cross-origin.

```bash
# terminal 1
cd backend
uvicorn sports_hedge.api.main:app --reload --port 8000

# terminal 2
cd frontend
npm run dev
```

CORS is a **restrictive allowlist** (`CORS_ALLOW_ORIGINS`), defaulting to `http://localhost:3000` and `http://127.0.0.1:3000`. It is not `*` and does not grant trading permissions. Server-rendered GETs do not exercise this path; clicking **Run scan** does (`POST /paper/collect/hot`).

## Current implementation

- paper-only safety configuration;
- canonical venue/event/market/order-book models;
- read-only venue abstraction;
- Matchbook official API login + event/market/order-book reads;
- Polymarket public Gamma/CLOB event/market/order-book reads;
- automated tests proving execution is unavailable;
- backend CI for compile, tests and static correctness.

## Project documents

**Start with `docs/CORE_TENETS.md`.** The core-tenets directory is the product/architecture contract for humans and AI agents and should be used as acceptance criteria during implementation and review.

See also:

- `docs/ARCHITECTURE.md`
- `docs/INITIAL_INTEGRATION_PLAN.md`
- `docs/RESEARCH_MODULE.md`
- `docs/MARKET_INTELLIGENCE_MODULE.md`
- `docs/SCENARIO_RESPONSE_PROFILES.md`
- `docs/MANAGER_ERA_AND_REGIME_CONTEXT.md`
- `docs/PRIORITY_ARB_ALERTS.md`
- `docs/HISTORICAL_ODDS.md` — source-neutral historical odds repository (Smarkets optional; quality tiers; coverage and Excel export)
- `docs/HISTORICAL_BACKFILL.md` — operator-invoked Football-Data.co.uk 2025/26 PL/Championship backfill

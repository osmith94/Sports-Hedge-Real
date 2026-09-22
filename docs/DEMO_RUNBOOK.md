# Sports Hedge — paper demo runbook (Windows)

Owner walkthrough for a **working paper demo**. This is not a production-readiness claim and does not enable live execution.

Exact click path below is for the consolidation branch that connects prepared size to confirmation (`prepared_deployment_id`) and keeps repeat scans from duplicating exposure.

Phase 1 remains:

```text
SPORTS_HEDGE_MODE=paper
SPORTS_HEDGE_EXECUTION_ENABLED=false
```

## Two surfaces (do not mix)

| Surface | Class | Use |
| --- | --- | --- |
| `http://127.0.0.1:3000/` | **LIVE PAPER** | Read-only collection, truthful venue health, naturally discovered fixtures. Empty stays empty. Never inject fixture rows here. |
| `http://127.0.0.1:3000/demo` | **DEMO / FIXTURE REPLAY** | Executable lifecycle proof when live collection has no qualifying arb: qualify → preview £10 → confirm → HOLD → UNWIND. Labelled separately from live `/`. |

Matchbook credentials (`MATCHBOOK_USERNAME` / `MATCHBOOK_PASSWORD`) are **optional**. Without them Matchbook health is honestly `unavailable`. Polymarket and Kalshi public read-only collection still runs. Owner-Windows Matchbook-credentialed smoke is a separate requirement and is not claimed by this remote walkthrough.

## One-time setup

In PowerShell, from the repo root:

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
cd ..\frontend
npm install
```

Copy repository-root `.env.example` to repository-root `.env` if you want local Matchbook credentials. Leave them blank for public PM/K-only discovery. `backend/.env` is ignored and is not active configuration.

## Start / stop

Double-click:

- `scripts\windows\Start-SportsHedge-Demo.bat` — starts FastAPI `:8000` and Next `:3000`, waits for health, opens **`/`** (not `/demo`). Prints current branch/SHA and whether backend/frontend were reused or restarted. Reuses a healthy process only when launcher PID identity, command, repo root, and Git HEAD match this checkout.
- `scripts\windows\Stop-SportsHedge-Demo.bat` — stops only launcher-owned PIDs, including verified Next.js descendants of the frontend `npm` wrapper. Unrelated Node processes are not killed.
- `scripts\windows\Refresh-SportsHedge-Demo.ps1` — one-command refresh onto latest `owner-live`: stop owned tree, verify ports 3000/8000 are gone, `git fetch` / `switch owner-live` / `pull --ff-only`, clear `frontend\.next` only, print branch/SHA, then start. Fails closed if git or stop fails; does not delete `node_modules`.

Or from PowerShell:

```powershell
.\scripts\windows\Start-SportsHedge-Demo.ps1
powershell.exe -ExecutionPolicy Bypass -File .\scripts\windows\Refresh-SportsHedge-Demo.ps1
```

The launcher sets `PAPER_AUTOFILL_ENABLED=true` so qualifying **LIVE_PAPER** opportunities auto-capture through the existing paper autofill path (allocator-sized; fail-closed; no venue orders). The console should show **AUTO PAPER CAPTURE ON** while retaining **PAPER MODE · NO EXECUTION**. Labelled `/demo` fixture replay does **not** inherit that setting: qualify → preview £10 → confirm remains explicit. It sets `PAPER_LIVE_REFRESH_ENABLED=true` and `ACCOUNTING_SCHEDULE_ENABLED=true` for this local process only. It never sets `SPORTS_HEDGE_EXECUTION_ENABLED=true`.

On backend start the accounting scheduler bootstraps the latest published ECB daily USD close when `fx_rates.sqlite` is empty or the persisted USD rate is stale, including Sunday/holiday starts (Friday's close is carried forward). It does **not** substitute the treasury demo FX snapshot (`paper_demo_fx_snapshot`, 0.80) into arb qualification. If ECB/network fetch fails, `/paper/economics-status` stays `missing_fx_rate:USD` and scans fail closed.

After bootstrap, `/paper/economics-status` (econ strip on `/`) shows USD GBP-per-unit, ECB source date, valuation date, carried-forward status, retrieval time, and BoE check status when available. Startup logs under `logs\demo-backend.*.log` repeat the same fields. Daily ingest is once per 16:15 Europe/London working day: a same-day restart does not refetch if today's published ECB primary is already persisted. If the 16:15 payload is still the previous working-day close, the day stays due and retries with backoff. ECB bootstrap fails closed quickly (bounded timeout); the BoE check is best-effort, skipped on startup, and must not hold the demo start.

Labelled `/demo` books are frozen `DEMO / FIXTURE REPLAY` snapshots (`data_kind=demo_fixture_replay`). Live quote-age rejection on `/` still fail-closes stale `live_paper` rows. Fixture replay is not a live-freshness waiver.

Logs: `logs\demo-backend.out.log`, `logs\demo-backend.err.log`, and `logs\demo-frontend.*.log`. Python `faulthandler` tracebacks, unhandled asyncio exceptions, and a single `backend_fatal` record (build SHA/branch plus the current/longest scanner event-loop phase) are written to the backend process stderr. The launcher keeps `RedirectStandardError` on `logs\demo-backend.err.log`, so those diagnostics land there. There is no periodic traceback dump.

## Click path

### 1. Live / read-only discovery (`/`)

1. Confirm the console shows **PAPER MODE · NO EXECUTION** and, for the Windows paper demo, **AUTO PAPER CAPTURE ON**.
2. Open `/health` or `/build-info` in a tab if needed: `mode=paper`, `execution_enabled=false`, `paper_autofill_enabled=true` on the launcher process, plus the serving Git SHA (`build.git_sha`) so the running process matches the checkout you launched.
3. Fast scan / Full sweep are server-owned. The browser polls `GET /paper/live-refresh`; do not expect auto-refresh to POST `/paper/collect`.
4. Primary **Run scan** posts `/paper/collect/hot`: the same current HOT identity/venue scope and bounded timing as Fast Scan, with no full discovery. If a scheduled lane is active, HTTP 409 / BUSY is truthful. Advanced **Run full diagnostic** retains broad, bounded `/paper/collect` discovery and may return partial coverage.
5. Inspect venue health: Matchbook `unavailable` without credentials is truthful. Polymarket/Kalshi public data may still populate fixtures.
6. If a **fresh** observation passes every fail-closed gate and the allocator accepts a sized plan, it should appear as an OPEN paper trade in Paper Portfolio / active trades without an operator click. Tracked/Near rows and historical discovery (including the prior Leeds v Newcastle 1.35% net candidate) are not trades. If **no qualifying live arb**, leave `/` honest and go to `/demo`. Do not paste fixture rows onto `/`.

### 2. Fixture-replay lifecycle (`/demo`) — use when live has no qualifying arb

1. Open `http://127.0.0.1:3000/demo`. The page is labelled **DEMO / FIXTURE REPLAY**.
2. **Start / seed pools** (ordinary reset). Three native pools: Matchbook GBP, Polymarket USD, Kalshi USD.
3. **Refresh Live Discovery** still uses the same read-only collect path. Live triggered/near lists stay empty when empty.
4. Venue pair: `matchbook ↔ polymarket`. Solver: `simple complete-set`.
5. Click **Qualify labelled replay**. This does **not** OPEN or lock.
6. In **Fixed-size paper preparation**, leave requested size **10**. Click **Prepare paper legs**.
7. Confirm: preview shows exact native legs/venues/currencies; available/locked treasury is unchanged.
8. Click **Confirm paper OPEN**. Confirm revalidates current economics. The accepted £10 legs are what lock. If economics changed, the UI asks you to prepare again — it does not silently resize. Provenance on this path is `fixture_demo`, not live paper.
9. Section 4 shows **OPEN**, opening fills, locked native amounts, guaranteed-at-open economics, realised P&L still empty (locking capital is not P&L).
10. Click **Retry confirm (idempotent)**. Same trade id; no second fill, lock, or journal. `PENDING` / `AWAITING_MANUAL_EXTERNAL` are not reachable on this labelled `simulate_external` confirm path; those states are covered by lifecycle tests, not this click path.
11. **Hold — do not release**. Locked capital stays locked. Hold vs unwind is analytical.
12. **Complete validated unwind**. Separate close fills, exact lock release, realised P&L/fees/FX posted once.
13. Click unwind again: idempotent (same trade, no second release).
14. **Record paper settlement** on the same trade must fail closed (unwind and settlement are mutually exclusive).

### 3. Restart persistence

1. Stop with `Stop-SportsHedge-Demo.bat`, then start again. Same SQLite paper ledger under `backend/data/` is reused.
2. `/paper` and `/treasury` still show the CLOSED (or OPEN, if you stopped before unwind) trade, native balances, fills, locks, and realised values.

### 4. Ledger reconstruction

1. Open `http://127.0.0.1:3000/treasury`.
2. The **Ledger reconstruction** line must read **OK**: native available/locked reconstruct from the append-only paper journal. GBP carrying values are FX translations, not spendable cash. Cash-in-transit remains deferred (#116), not a silent invent.

## What this runbook does not claim

- Live qualifying arbitrage during a given session.
- Matchbook-credentialed discovery unless you supply owner credentials locally.
- Simultaneous real venue fills (Tenet 18). Paper full-fill is not proof of live atomic execution.
- Production general ledger.

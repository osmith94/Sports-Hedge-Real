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

Copy `.env.example` to `.env` if you want local Matchbook credentials. Leave them blank for public PM/K-only discovery.

## Start / stop

Double-click:

- `scripts\windows\Start-SportsHedge-Demo.bat` — starts FastAPI `:8000` and Next `:3000`, waits for health, opens **`/`** (not `/demo`).
- `scripts\windows\Stop-SportsHedge-Demo.bat` — stops only launcher-owned PIDs.

Or from PowerShell:

```powershell
.\scripts\windows\Start-SportsHedge-Demo.ps1
```

The launcher sets `PAPER_AUTOFILL_ENABLED=false` so the operator can choose £10 before OPEN. It sets `PAPER_LIVE_REFRESH_ENABLED=true` for this local process only. It never sets `SPORTS_HEDGE_EXECUTION_ENABLED=true`.

Logs: `logs\demo-backend.*.log` and `logs\demo-frontend.*.log`.

## Click path

### 1. Live / read-only discovery (`/`)

1. Confirm the console shows **PAPER MODE · NO EXECUTION**.
2. Open `/health` in a tab if needed: `mode=paper`, `execution_enabled=false`.
3. Click **Run paper scan** / refresh live discovery. Wait until the scan finishes (bounded; may be degraded if Matchbook is unavailable).
4. Inspect venue health: Matchbook `unavailable` without credentials is truthful. Polymarket/Kalshi public data may still populate fixtures.
5. Open a discovered fixture if one exists. If there is a **preparable** settlement-equivalent opportunity, continue the £10 path on that fixture. If **no qualifying arb**, leave `/` honest and go to `/demo`. Do not paste fixture rows onto `/`.

### 2. Fixture-replay lifecycle (`/demo`) — use when live has no qualifying arb

1. Open `http://127.0.0.1:3000/demo`. The page is labelled **DEMO / FIXTURE REPLAY**.
2. **Start / seed pools** (ordinary reset). Three native pools: Matchbook GBP, Polymarket USD, Kalshi USD.
3. **Refresh Live Discovery** still uses the same read-only collect path. Live triggered/near lists stay empty when empty.
4. Venue pair: `matchbook ↔ polymarket`. Solver: `simple complete-set`.
5. Click **Qualify labelled replay**. This does **not** OPEN or lock.
6. In **Fixed-size paper preparation**, leave requested size **10**. Click **Prepare paper legs**.
7. Confirm: preview shows exact native legs/venues/currencies; available/locked treasury is unchanged.
8. Click **Confirm paper OPEN**. Confirm revalidates current economics. The accepted £10 legs are what lock. If economics changed, the UI asks you to prepare again — it does not silently resize.
9. Section 4 shows **OPEN**, opening fills, locked native amounts, guaranteed-at-open economics, realised P&L still empty (locking capital is not P&L).
10. **Hold — do not release**. Locked capital stays locked. Hold vs unwind is analytical.
11. **Complete validated unwind**. Separate close fills, exact lock release, realised P&L/fees/FX posted once.
12. Click unwind again: idempotent (same trade, no second release).
13. **Record paper settlement** on the same trade must fail closed (unwind and settlement are mutually exclusive).

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

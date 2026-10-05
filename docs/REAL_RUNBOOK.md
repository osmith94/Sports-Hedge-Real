# Sports Hedge Real — Windows dry-run runbook

One command starts the Real backend and operator console on this checkout. Live order execution stays off. This launcher does not place, cancel, or sign venue orders.

```text
SPORTS_HEDGE_MODE=real
SPORTS_HEDGE_EXECUTION_ENABLED=false
PAPER_LIVE_REFRESH_ENABLED=true
PAPER_AUTOFILL_ENABLED=false
PAPER_AUTO_UNWIND_ENABLED=false
ACCOUNTING_SCHEDULE_ENABLED=false
```

`PAPER_LIVE_REFRESH_ENABLED` is a legacy-named scanner-loop setting. Setting it `true` does **not** switch the runtime into paper mode. `GET /health` must report `mode=real`.

`ACCOUNTING_SCHEDULE_ENABLED=false` keeps journal revaluation off. It does not disable scanner FX. Startup bootstraps or reuses the latest published ECB USD close in the shared FX repository before any scanner pricing worker starts. The treasury demo rate is not used for scanner economics.

## One command

From the repository root:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\windows\Start-SportsHedge-Real.ps1
```

Or double-click:

```text
scripts\windows\Start-SportsHedge-Real.bat
```

Stop only the processes this launcher owns:

```powershell
powershell.exe -ExecutionPolicy Bypass -File .\scripts\windows\Stop-SportsHedge-Real.ps1
```

Or double-click `scripts\windows\Stop-SportsHedge-Real.bat`.

The launcher starts:

```text
backend\.venv\Scripts\python.exe -m uvicorn sports_hedge.api.main:app --host 127.0.0.1 --port 8000
```

Working directory is `backend\`. The canonical dotenv remains the repository-root `.env`. `backend\.env` is ignored. Process environment overrides dotenv. The script does not print secret values.

The frontend listens on `http://127.0.0.1:3000/`. The browser opens only after the health gate below passes and the console status route is ready.

## Health gate

The launcher does not print `Sports Hedge Real is running` unless `GET http://127.0.0.1:8000/health` proves all of:

- `mode` is `real`
- `execution_enabled` is `false`
- `live_refresh.server_loop_enabled` is `true`

It also prints `scanner_stopped`, `universe_scans_paused`, `background_pricing_paused`, `settlement_scans_paused`, and the Git branch and SHA.

Persisted operator controls are reported only. The launcher does not POST resume or unpause endpoints. If a control is paused, startup prints a warning such as:

```text
BACKGROUND pricing is persisted as paused by operator.
```

The same pattern is used for the scanner master stop, the UNIVERSE schedule, and settlement scans.

Startup text includes:

```text
REAL MODE
LIVE MARKET SCANNING ENABLED
LIVE ORDER EXECUTION DISABLED
```

## Ports and process ownership

Port occupancy is read from the OS listener (`Get-NetTCPConnection`, with `netstat` as fallback), not from HTTP. A healthy-looking `/health` response does not mean the port is free.

- An unrelated process on port 8000 or 3000 is refused and left running.
- A launcher-owned Real process recorded in `logs\real-backend.pid` or `logs\real-frontend.pid` is reused when the command, repo root, and Git HEAD match this checkout, and the listener is that PID or a verified descendant.
- A different Git HEAD restarts that owned process, and only after the port is confirmed free.
- A reused owned backend that fails the Real health gate is stopped and replaced once, again only after port 8000 is free.
- Stop verifies identity before it terminates a PID, then stops verified frontend descendants. It does not broadly kill Python or Node.

Real logs and PID files are separate from the paper demo launcher:

```text
logs\real-backend.pid
logs\real-frontend.pid
logs\real-backend.out.log
logs\real-backend.err.log
logs\real-frontend.out.log
logs\real-frontend.err.log
```

## Troubleshooting

Real mode can be healthy while the scanner loop is off. `PAPER_LIVE_REFRESH_ENABLED` defaults to false, so a backend started without this launcher can serve `GET /health` with `mode=real` and `live_refresh.server_loop_enabled=false`. The UNIVERSE scanner does not run in that state. This launcher sets the flag to `true` and fails closed unless `/health` shows `live_refresh.server_loop_enabled=true`.

Port 8000 may still be held by an older launcher-owned backend. A replacement process then fails with WinError 10048 while `/health` continues to hit the old process. This launcher treats the OS listener as the occupancy source. It stops and restarts only a verified Real-launcher process, and it waits until the port is gone before binding again. An unrelated occupant is not killed.

Do not start the backend with `Start-Process -Command` and unquoted environment assignments. PowerShell can strip the quotes and then try to execute `real`, `false`, and `true` as commands. Use this script, which assigns quoted values in-process and starts `python.exe` with `-FilePath` and `-ArgumentList`.

`execution_enabled` must remain false during this Real dry-run. The launcher sets `SPORTS_HEDGE_EXECUTION_ENABLED=false` and refuses success if `/health` reports anything else. Do not flip that flag to "finish" a scan.

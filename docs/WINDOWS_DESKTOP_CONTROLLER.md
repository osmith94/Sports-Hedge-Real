# Windows desktop controller — `SportsHedge.exe`

`SportsHedge.exe` is a resident Windows controller that owns one local Sports Hedge session. It runs the **current local checkout** of this repository in **PAPER MODE only**. The Sports Hedge UI stays in the operator's normal browser.

It is not an installer. Python, Node and the application source are not bundled: the exe drives the existing `backend\.venv`, Node/npm, `frontend\node_modules` and repo-root `.env`.

## Operator use

| Action | What happens |
| --- | --- |
| Double-click `dist\windows\SportsHedge.exe` | Validates the checkout, rebuilds the production interface if needed, starts the backend and interface, waits for genuine health, opens `http://127.0.0.1:3000/`, stays in the tray. |
| Close the browser tab/window | Nothing. Sports Hedge keeps running (tray icon, backend, interface). The browser is never owned or terminated. |
| Tray → **Open Sports Hedge** / double-click tray icon | Re-opens the UI in the default browser. |
| UI sidebar → **Exit Sports Hedge** → confirm | Graceful shutdown. The page switches to *Stopping Sports Hedge…* then *Sports Hedge has stopped. You may close this browser tab.* |
| Tray → **Exit Sports Hedge** → confirm | Same shutdown state machine as the UI exit. |
| Launch `SportsHedge.exe` again while running | No new backend/frontend/controller. The running controller opens the browser; the second exe exits. |
| Task Manager → End task on `SportsHedge.exe` | Windows closes the controller's Job Object handles, which kills the backend and interface process trees. |

Tray states: `Sports Hedge — Starting`, `Sports Hedge — PAPER MODE — Running`, `Sports Hedge — Degraded`, `Sports Hedge — Stopping`.

A small progress window shows `Starting Sports Hedge…`, `Checking frontend build…`, `Building interface…` (only when needed), `Starting backend…`, `Waiting for backend…`, `Starting interface…`, `Waiting for interface…`. On failure a dialog names the failed component, the reason, the log directory and the Git SHA/branch, after cleaning up anything the controller started.

## Build

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\windows\Build-SportsHedge-App.ps1
# optional: -CreateDesktopShortcut  -SkipFrontendChecks  -SkipDotnetTests
```

Requires Git, Node.js 22 (npm), the .NET 8 SDK and, at runtime, `backend\.venv`. Output: `dist\windows\SportsHedge.exe` (self-contained, single-file, win-x64) and `dist\windows\SportsHedge.launcher.json` (records the repo root so a moved `dist\windows` folder still finds the checkout). `dist/`, `bin/`, `obj/` and `.next/` are gitignored; CI publishes the exe as a GitHub Actions artifact instead of committing it.

## Updates: application code vs controller code

| Change | Action needed |
| --- | --- |
| Backend, frontend, scanner, matching, accounting, treasury, reporting, UI components merged to `main` | Update the local checkout (for example `git pull`) yourself, then launch `SportsHedge.exe`. No exe rebuild. |
| Anything under `desktop\` (controller code) | Re-run `Build-SportsHedge-App.ps1` to rebuild `SportsHedge.exe`. |

`SportsHedge.exe` never runs `git fetch`, `pull`, `switch`, `reset` or any other command that mutates the checkout. If GitHub `main` is newer than the local checkout, the local checkout runs as-is. There is no self-update or self-replacement.

### Frontend build SHA marker

Production Next.js needs a build. At every start the controller compares `git rev-parse HEAD` with `frontend\.next\sports-hedge-build.json`:

```json
{
  "schema": "sports-hedge-frontend-build/v1",
  "git_sha": "…",
  "git_branch": "…",
  "build_timestamp": "2026-09-25T13:00:00Z",
  "next_build_id": "<contents of frontend\\.next\\BUILD_ID>",
  "frontend_dirty": false,
  "builder": "SportsHedge.exe | Build-SportsHedge-App.ps1"
}
```

The interface is rebuilt (`npm run build`) when the marker is missing or invalid, `.next\BUILD_ID` is missing or differs from the marker, the SHA differs, or `frontend\` has uncommitted changes. Otherwise the build is reused and startup is fast.

- The marker is deleted before every build and written only after a successful one. A failed build therefore never leaves an old `.next` looking current: startup is refused, a dialog explains why, and Sports Hedge stays stopped.
- The marker lives inside `.next\`. `next dev` (the PowerShell demo launcher) and `Refresh-SportsHedge-Demo.ps1` both wipe `.next`, which also removes the marker and forces a rebuild next time.
- A separate `distDir` was rejected because Next.js rewrites the tracked `next-env.d.ts` and `tsconfig.json` for any non-default `distDir`, which would mutate the checkout.
- Missing `frontend\node_modules` is installed with `npm ci` (never rewrites `package-lock.json`).

## Architecture

```text
desktop/
  SportsHedge.Desktop.sln
  SportsHedge.Launcher/          WinExe tray app  -> SportsHedge.exe (net8.0-windows, WinForms)
  SportsHedge.Launcher.Core/     testable controller logic (net8.0)
  SportsHedge.Launcher.Tests/    xUnit: unit, Windows Job Object integration, end-to-end
  SportsHedge.Launcher.TestHost/ headless harness for cross-process / end-to-end tests
```

| Concern | Core type |
| --- | --- |
| Session lifecycle, startup + shutdown state machine | `SessionController`, `ControllerStateMachine` |
| Process ownership | `IProcessGroupLauncher`, `IOwnedProcessGroup`, `WindowsJobProcessLauncher`, `WindowsJobObject`, `JobOwnedProcessGroup` |
| Port inspection (read-only) | `IPortProbe`, `SystemPortProbe`, `PortPreflight` |
| Health | `IHealthProbe`, `HttpHealthProbe`, `HealthEvaluator` |
| Git / build SHA | `IGitReader`, `GitCli`, `FrontendBuildDecision`, `FrontendBuildMarkerStore`, `NpmFrontendBuilder` |
| Single instance | `SingleInstanceGuard`, `SecondaryInstance` |
| IPC + authentication | `ControllerPipeServer`, `ControllerIpcProtocol`, `SessionSecrets` |
| Child environment (paper safety, secret routing) | `ChildEnvironment` |

### Children

| Child | Command | Working dir | Logs |
| --- | --- | --- | --- |
| Backend | `backend\.venv\Scripts\python.exe -m sports_hedge.api.desktop_host --host 127.0.0.1 --port 8000` | `backend\` | `logs\desktop-backend.out.log`, `logs\desktop-backend.err.log` |
| Interface | `cmd.exe /d /s /c ""npm.cmd" run start -- -H 127.0.0.1 -p 3000"` (production `next start`, never `next dev`) | `frontend\` | `logs\desktop-frontend.out.log`, `logs\desktop-frontend.err.log` |
| Interface build | `npm run build` | `frontend\` | `logs\desktop-frontend-build.out.log`, `.err.log` |

Controller log: `logs\desktop-controller.log` (startup, Git SHA/branch, build decision, rebuild result, child PIDs, health results, shutdown source, graceful/forced outcome, verification). Each child log keeps one `.previous.log` copy. The PowerShell demo launcher's `logs\demo-*.log` files are untouched. Backend faulthandler / `backend_fatal` diagnostics continue to go to the backend stderr log.

### Process ownership and Job Objects

- Every child is created with `CreateProcessW(CREATE_SUSPENDED | CREATE_NO_WINDOW)`, assigned to a fresh Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` (plus `DIE_ON_UNHANDLED_EXCEPTION`), then resumed. It cannot spawn anything before it is in the job; every descendant (venv `python.exe` shim → real `python.exe`; `cmd` → `npm` → `node` → `next`) inherits membership. Breakaway is not allowed.
- One job per service (backend, interface, build) so each can be stopped precisely. The controller holds the only handle to each job for the whole session.
- The controller itself is **not** in a job, so the browser it opens via the shell is never owned.
- When `SportsHedge.exe` exits for any reason — normal exit, crash, Task Manager End task — Windows closes the handles and terminates every job member.
- There is no API anywhere in the controller to act on an arbitrary PID, process name or port owner. A static test guards against `GetProcessesByName`, `taskkill`, `Stop-Process` and stray `Kill(` calls.

### Normal shutdown (UI Exit, tray Exit, Windows session end)

1. State → `Stopping`; further requests return `already_stopping` (idempotent).
2. Controller sends `POST http://127.0.0.1:8000/desktop/shutdown` with the backend secret.
3. `desktop_host` sets `server.should_exit = True`; uvicorn shuts down gracefully and runs the FastAPI lifespan cleanup in `sports_hedge.api.main` (accounting schedule, live-refresh coordinator, shared provider runtime, shared Matchbook client). The backend log shows `Application shutdown complete.`
4. Controller waits up to 30 s for every backend job member to exit, then for port 8000 to stop listening. If graceful shutdown fails or times out it logs `backend_forced_cleanup` and terminates **only the backend Job Object**.
5. Interface: Next.js production keeps no durable state and Windows has no SIGTERM equivalent, so the interface Job Object is terminated; the controller waits for its members and port 3000.
6. Verification: logs remaining owned PIDs (must be none) and whether 8000/3000 still listen. A foreign listener is reported, never killed.
7. Named pipe closed, Job Objects released, state → `Stopped`, `SportsHedge.exe` exits.

### Crash handling

- Backend or interface process exits unexpectedly → `Degraded`, tray balloon, log entry naming the exit code and log file. The controller never claims healthy and never auto-restarts. The operator uses Exit to clean up.
- Three consecutive failed health checks → `Degraded` (recovers to Running if health returns while both processes are alive).
- Controller crash / forced termination → Job Objects kill the children.

### Port and process safety

Before starting (and again after any rebuild) the controller inspects 8000 and 3000. Any listener refuses startup:

> Sports Hedge cannot start because port 8000 is already in use by another process (PID 1234 (python)). No processes were terminated. …

Because the controller holds the single-instance mutex, a listener cannot be this session's own child, so there is no "reuse" path to confuse with an unrelated process. Health checks additionally require each listener to echo this controller's public per-launch session id, the current Git SHA, `mode=paper` and `execution_enabled=false`.

## Paper-only boundary

Every child environment forces, after inheriting the operator's environment and overriding repo-root `.env`:

```text
SPORTS_HEDGE_MODE=paper
SPORTS_HEDGE_EXECUTION_ENABLED=false
PAPER_AUTOFILL_ENABLED=true
PAPER_AUTO_UNWIND_ENABLED=true
PAPER_LIVE_REFRESH_ENABLED=true
ACCOUNTING_SCHEDULE_ENABLED=true
```

These mirror `Start-SportsHedge-Demo.ps1`. No venue write, order, wallet-signing or credential behaviour is added or changed. Startup health fails unless the backend reports `mode=paper` and `execution_enabled=false`. The tray, progress window and web UI all show PAPER MODE.

## Security review

| Topic | Design |
| --- | --- |
| Secret generation | `RandomNumberGenerator`, 32 bytes each, base64url. Two independent secrets per launch: controller IPC token and backend shutdown token. Pipe name uses a third random value; a fourth, public session id is used only for health identity. |
| Secret lifetime | Controller memory for one session; never written to disk. |
| Secret transport | Child process environment blocks only. The Next.js **server** process receives only `SPORTS_HEDGE_CONTROLLER_TOKEN` and `SPORTS_HEDGE_CONTROLLER_PIPE`; the backend receives only `SPORTS_HEDGE_DESKTOP_SHUTDOWN_TOKEN`. The `npm run build` environment contains neither, and pre-existing `SPORTS_HEDGE_CONTROLLER_*` / `SPORTS_HEDGE_DESKTOP_*` values from the operator's shell are stripped. |
| Secret logging | Both secrets are registered with a redactor applied to every controller log line. IPC and HTTP rejections log reasons, never presented tokens. Tests assert no secret appears in logs. |
| Browser visibility | Never `NEXT_PUBLIC_*`. `/api/desktop/status` returns only `desktop_controller`, the public `session_id`, `git_sha`, `mode`, `execution_enabled`. Client components never import the server bridge (test-enforced). |
| Named pipe ACL | Windows DACL: owner and sole Allow ACE = current user; explicit Deny for `NT AUTHORITY\NETWORK`. One instance at a time (`maxInstances=1`), so the name cannot be shared with a squatter. Requests are one newline-terminated JSON line ≤ 4 KiB with a 5 s timeout, and must carry the controller token (constant-time comparison). |
| Route authentication | `POST /api/desktop/exit` requires: desktop mode configured; `Host` ∈ {`127.0.0.1:3000`, `localhost:3000`}; `Origin` present, allow-listed and matching `Host`; `Sec-Fetch-Site` = `same-origin` when present; custom header `x-sports-hedge-action: exit`; `application/json` body `{"confirm": true}`. Only then does the server route authenticate to the controller over the pipe. |
| Cross-origin behaviour | An unrelated website cannot trigger exit: browsers set `Origin` (rejected), the custom header forces a CORS preflight that the route never approves (no `Access-Control-Allow-Origin`), and DNS-rebinding requests carry the attacker's `Host` (rejected). Even a request that reached the controller would still need the server-only token. |
| Backend shutdown route | Exists only under `desktop_host` (the normal `uvicorn sports_hedge.api.main:app` has no shutdown route). Requires desktop mode, a ≥ 32-char per-launch secret, a loopback peer and `Authorization: Bearer` (constant-time compare). Missing token → 401, invalid → 403, non-loopback → 403, disabled → 404. The backend only binds loopback; `desktop_host` refuses non-loopback bind addresses. |
| Loopback binding | Backend `127.0.0.1:8000`, interface `127.0.0.1:3000`. No controller network listener exists. |
| Threat model limit | A malicious process already running as the same Windows user can read other processes' memory/environment and terminate them directly; the controller does not try to defend against that. |

## Known limitations

- Windows 10/11 x64 only. The controller runs on other OSes only through a development fallback launcher without kill-on-close (used by Linux CI/dev tests, never by `SportsHedge.exe`).
- The interface is stopped by Job Object termination (no graceful Next.js signal on Windows); it holds no durable state.
- Running `next dev` against the same checkout while `SportsHedge.exe` is running on another port would wipe `.next` underneath the production server.
- Any change to `frontend\` while uncommitted forces a rebuild on every launch (correctness over speed).
- The exe is unsigned; SmartScreen may warn on first run.

## Remaining work before a fully portable installed app

- **Python runtime**: bundle an embedded CPython + locked wheels (or a frozen backend) instead of `backend\.venv`.
- **Node / Next.js**: bundle Node or use Next.js `output: "standalone"` so no `npm`/`node_modules` is needed at runtime.
- **Installer**: MSI/MSIX (WiX or similar) with Start-menu shortcut and prerequisites.
- **Install location and data paths**: move code to Program Files and mutable data/logs/`.env` to `%LOCALAPPDATA%\SportsHedge` with a migration from the repo layout.
- **Upgrade/update**: signed update channel, safe stop → replace → restart, rollback.
- **Signing**: Authenticode-sign the exe and installer.
- **Versioning**: stamp the controller version and application version/SHA into the binary and UI.
- **Uninstall lifecycle**: stop running session, remove binaries/shortcuts, optionally keep data.
- **Explicit "Refresh from Main"**: owner-triggered stop → verify clean checkout → fetch → switch main → pull --ff-only → rebuild → restart (deliberately not in this version).

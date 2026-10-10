# Wave 40 live canary — operator runbook

This is capability preparation. It does **not** authorize Cursor, CI, or a merge to place bets, set live flags on `main`, deploy, restart a PC, or touch credentials.

The ordinary Real launcher (`scripts/windows/Start-SportsHedge-Real.ps1`) stays dry-run: `SPORTS_HEDGE_EXECUTION_ENABLED=false` and `PAPER_AUTOFILL_ENABLED=false`.

## What the £5 cap means

The durable canary session reserves **combined requested GBP outlay** (both legs, FX from the accepted Price-2 snapshot, rounded up to a penny) **before any venue write**. Pending, partial, unknown-ACK, failed-after-submit, and filled amounts all count. The reservation is released only when the process never attempted a venue write.

It is **not** a guarantee of fill, profit, or matched hedge. An unmatched filled leg can lose more than residual session room. Fees and FX are the snapshot's own economics, not a new haircut.

Hard constants (not env-overridable):

- opening tranche combined both legs ≤ **£3**
- session-wide including a genuine fully-hedged top-up ≤ **£5**
- **one** approved opportunity
- minimum guaranteed net edge **0.20%**
- Price-2 skew **500 ms**, quote age **2000 ms**

If native minima make £3 infeasible, the UI/health reason is `canary_native_minimum_exceeds_budget` / NOT READY. Stakes are never raised to satisfy an exchange minimum.

## Prepare (operator machine only)

1. Integrate and review Wave 38 STREAM fee/FX (#42) and Wave 39 Matchbook Price-2 tail refresh (#43) with this branch. Do not arm live on a split Price-2.
2. Confirm Matchbook and Polymarket accounts, wallet, `exchange_v3` allowance, free-funds, and **current-territory** permission for **both** venues. Polymarket geoblock uses the **current outbound IP** and fails closed. Do not use a VPN/proxy or claim permission from prior travel.
3. Leave `.env` credentials in place on the operator machine. This runbook does not print or rotate them.
4. Independent venue UIs should be open so you can verify order history if an ACK is unknown.

## Start one-off canary

```text
scripts\windows\Start-SportsHedge-LiveCanary.bat
```

Type exactly `ARM LIVE CANARY`. Unattended start is refused.

Expect `/health` to show `mode=real`, `execution_enabled=true`, `live_canary.enabled=true`, `paper_autofill_enabled=false`. **Do not** treat `execution.live_execution_ready` as trading permission.

The console must say **LIVE CANARY**, not AUTO PAPER CAPTURE / PAPER-only.

## Observe

1. At most one registered MB–PM package should be able to reserve.
2. Opening combined outlay ≤ £3; a later top-up only after **both** opening legs are positively reconciled and fully hedged, remaining session ≤ £5.
3. Differentiate accepted / no fill / partial / unknown / fully hedged in Activity, journal, `/health.live_canary`, and `/health.orphaned_live_executions`.
4. After an unknown ACK or restart, do **not** retry the same client order. Inspect venue histories manually.

## Stop new submits

`POST http://127.0.0.1:8000/live-canary/halt-new-orders` or the console **HALT NEW ORDERS** control.

This is durable **HALT NEW ORDERS**. It does **not** cancel resting or unknown venue orders.

Then `scripts\windows\Stop-SportsHedge-LiveCanary.bat`. A later Real dry-run start must still report execution OFF.

## Fail-closed preflight

Read-only Matchbook and Polymarket preflight stay operator-local after merge. Cursor must not run them against live credentials.

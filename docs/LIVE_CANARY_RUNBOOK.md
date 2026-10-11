# Wave 40 live execution — operator runbook

There is **one Real mode** and **one Windows launcher**. Execution boots **OFF**. A top-right dashboard toggle arms **continuous** Real execution immediately after one explicit confirmation. The ON switch records operator intent; it does **not** perform trading/account/geoblock preflights.

This does **not** authorize Cursor, CI, or a merge to place bets, set live flags on `main`, deploy, restart a PC, or touch credentials.

```text
scripts\windows\Start-SportsHedge-Real.bat
```

`SPORTS_HEDGE_EXECUTION_ENABLED=false` and `PAPER_AUTOFILL_ENABLED=false` stay the dry-run defaults. Do not hand-edit them to arm.

## Capital authority

Use the **currently persisted** operator Max Event / Max Opportunity / Max One-Time settings (screenshot example £7 / £7 / £5). Wave 40 does **not** add a £3 opening tranche, £5 session budget, or one-opportunity stop. Those limits are not silently raised to get an order through.

Price-2 still uses the saved min net edge (example 0.20%), 500 ms skew and 2000 ms freshness unless the operator saved different thresholds.

If native minima make a candidate infeasible under those caps, that opportunity is rejected. Stakes are never raised.

Server **restarts always OFF**. Open/unknown/orphan ledger state persists so the same package is not blindly retried.

## Toggle ON

1. In the top-right header, EXECUTION OFF → one **REAL-MONEY EXECUTION** confirmation → click **Confirm EXECUTION ON**. Confirmation shows the **actual** saved operator limits; there is no typed phrase or extra cap. A page visit never arms.
2. The backend sets **EXECUTION ON** without probing venue accounts, Matchbook currency, Polymarket geoblocking, funds, FX, orphan orders or market data. Neither a slow exchange nor failed preflight may hold the ON switch OFF.
3. ON stays ON during HOT/BACKGROUND/STREAM scanning until the operator switches OFF or the backend restarts. Browser refresh reads server ON/OFF state; it never auto-arms.
4. **Separately, before each actual live order**, existing current-IP venue eligibility, credentials/allowance/funds, native currency, fees/FX, unknown orders, accepted Price-2, native minimums, persisted operator caps and risk protections remain mandatory. A blocked/unreachable venue denies that order **without toggling execution OFF**. Never bypass geoblocking or use VPN/proxy to circumvent venue restrictions. No fabricated books or orders.
5. GET /live-canary/preflight is optional operator diagnosis, not a prerequisite to ON. A fast GET /health has no venue calls. ON is *not* a claim that current trading permission has been verified.

`/health.execution.live_execution_ready` is **not** trading permission.

## Toggle OFF

OFF (or `POST /live-canary/disarm`) stops **new** submits immediately, including in-flight capture races. It does **not** cancel orders already submitted. Monitor venue histories for unknown ACK. Do not retry ambiguous client orders after restart.

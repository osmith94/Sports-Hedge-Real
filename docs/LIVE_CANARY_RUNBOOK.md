# Wave 40 live execution — operator runbook

There is **one Real mode** and **one Windows launcher**. Execution boots **OFF**. A top-right dashboard toggle arms **continuous** Real execution after confirmation and read-only preflight.

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

Unresolved/partial/unknown live orders are read from the **durable** `live_execution_attempts` table in `paper_ledger.sqlite` (`Settings.paper_ledger_db_path`). The in-memory orphan report is empty after restart and is not proof that none exist. If that journal cannot be inspected, new submits are refused. Per-package attempt rows still block duplicate POSTs of the same package; this is not a session trade-count cap. Matchbook live submits require **affirmative** currency compatibility (`True`); missing/unknown is not permission.

## Toggle ON

1. Integrate Wave 38 (#42) and Wave 39 (#43) before using live Price-2.
2. Confirm current-territory permission for **both** venues. Polymarket geoblock is current outbound IP, fail closed. No VPN/proxy.
3. In the top-right header, EXECUTION OFF → confirm **REAL-MONEY EXECUTION** → type `ARM LIVE CANARY`. Confirmation shows the **actual** operator limits, not invented £3/£5 constants.
4. While preflight runs the switch shows **CHECKING**, not ON. Fail reasons stay OFF.
5. ON stays ON during normal HOT/BACKGROUND/STREAM scanning until the operator switches OFF or a real risk/venue block. A browser refresh reads server state and does not auto-arm.

`/health.execution.live_execution_ready` is **not** trading permission.

## Toggle OFF

OFF (or `POST /live-canary/disarm`) stops **new** submits immediately, including in-flight capture races. It does **not** cancel orders already submitted. Monitor venue histories for unknown ACK. Do not retry ambiguous client orders after restart.

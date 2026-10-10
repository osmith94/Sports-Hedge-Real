# Wave 40 live canary — operator runbook

There is **one Real mode** and **one Windows launcher**. Execution boots **OFF**. A top-right dashboard toggle arms a bounded live canary after confirmation and read-only preflight.

This does **not** authorize Cursor, CI, or a merge to place bets, set live flags on `main`, deploy, restart a PC, or touch credentials.

```text
scripts\windows\Start-SportsHedge-Real.bat
```

`SPORTS_HEDGE_EXECUTION_ENABLED=false` and `PAPER_AUTOFILL_ENABLED=false` stay the dry-run defaults. Do not hand-edit them to arm.

## What the £5 cap means

The durable canary session reserves **combined requested GBP outlay** (both legs, FX from the accepted Price-2 snapshot, rounded up to a penny) **before any venue write**. Pending, partial, unknown-ACK, failed-after-submit, and filled amounts all count. The reservation is released only when the process never attempted a venue write.

It is **not** a guarantee of fill, profit, or matched hedge. An unmatched filled leg can lose more than residual session room.

Hard constants (not env-overridable):

- opening tranche combined both legs ≤ **£3**
- session-wide including a genuine fully-hedged top-up ≤ **£5**
- **one** approved opportunity
- minimum guaranteed net edge **0.20%**
- Price-2 skew **500 ms**, quote age **2000 ms**

If native minima make £3 infeasible, status is NOT READY. Stakes are never raised.

Server **restarts always OFF**. Session spend and unresolved venue attempts remain durable. Re-arming cannot spend above the remaining £5.

## Toggle ON

1. Integrate Wave 38 (#42) and Wave 39 (#43) before using live Price-2.
2. Confirm current-territory permission for **both** venues. Polymarket geoblock is current outbound IP, fail closed. No VPN/proxy.
3. In the top-right header, EXECUTION OFF → confirm **REAL-MONEY EXECUTION** → type `ARM LIVE CANARY`.
4. While preflight runs the switch shows **CHECKING**, not ON. Fail reasons stay OFF.
5. ON is real only after the backend acknowledges. A browser refresh reads server state and does not auto-arm.

`/health.execution.live_execution_ready` is **not** trading permission.

## Toggle OFF

OFF (or `POST /live-canary/disarm`) stops **new** submits immediately, including in-flight capture races. It does **not** cancel orders already submitted. Monitor venue histories for unknown ACK. Do not retry ambiguous client orders after restart.

A lost heartbeat / expired arm lease fails closed to OFF.

# Core Tenet 11 — UI, Drill-Down & Data Honesty

## Principle

Sports Hedge should be fast to browse like a football-management game, dense enough for serious analysis, and explicit about what each number represents.

The UI must never make demo, modelled, historical or stale data look live.

## Non-negotiables

- Clearly label `PAPER MODE` where execution context matters.
- Clearly label `DEMO / FIXTURE DATA` where temporary UI fixtures are used.
- Clearly distinguish live market quotes, historical observations, model outputs and illustrative fixtures.
- Do not fabricate fallback opportunities when an API is offline/empty.
- Show stale/carried-forward/exception states rather than silently presenting them as fresh.
- When the scanner has more than one cadence, operator status must name each cadence (for example Fast scan vs Full sweep). A single ambiguous `Last scan` is not sufficient.
- Tracked current-state must not present expired observations as current. The live contract today is latest-completed-cohort; the dual-cadence merge (Issue #158, architect-accepted, not yet implemented) is `docs/DUAL_CADENCE_SCANNER.md`. Kickoff-passed unknown in-play must not be labelled live, and elapsed time must not fabricate a completed result.
- Keep sample size, confidence, stability and data quality visible near analytical claims.

## Football-style drill-down

The user should be able to browse naturally:

```text
Research Home
  -> Fixture
  -> Team
  -> Scenario Response Profile
  -> Scenario Lab / individual scenario
  -> odds/value view
  -> paper scenario rule
```

Team pages should feel like persistent club profiles rather than one-off query forms.

Arbitrage should feel operational:

```text
Near-Arb Watchlist
  -> Triggered opportunity
  -> Priority Alert if exceptional
  -> Paper fill/activity
  -> capital/P&L/liquidity
```

## Home-screen hierarchy

Research Home should emphasize odds-weighted value signals and upcoming fixtures.

Arbitrage Home should emphasize near-arbs, triggered/executable opportunities, activity, P&L/capital and liquidity pools.

## Review checks

- Can the user tell whether each number is live, historical, modelled or demo?
- Can a user click from a fixture into both teams and relevant scenarios?
- Does an empty/offline state remain honest rather than inventing content?
- Are Research and Arbitrage visually and linguistically distinct?
- Are important warnings visible without requiring deep navigation?

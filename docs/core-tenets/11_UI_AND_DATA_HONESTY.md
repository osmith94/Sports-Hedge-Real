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
- Tracked current-state must not present expired observations as current. Dual-cadence merge (Issue #158): HOT observations for the hot cohort use the HOT radar TTL (90s). UNIVERSE discovery observations use the UNIVERSE current-state TTL (360s). A BACKGROUND-priced opportunity may remain `radar_current` on Opportunity Monitor for the separate BACKGROUND current-state retention (default 45 minutes), which is longer than one ordinary full catalogue pass. That retention does not make stale quotes executable, does not raise the HOT or UNIVERSE TTL, and does not backfill from audit history. Kickoff-passed unknown in-play must not fabricate provider `in_running` or a completed result. A post-kickoff fixture with a successful current evaluation and remaining matched equivalents may show the operator reason IN PLAY; that label is a read-model and does not overwrite provider lifecycle provenance. A successful post-kickoff evaluation that confirms zero matched equivalents leaves current HOT radar without marking the fixture completed. Incomplete scans are not that signal. Issue #164: explicit terminal/completed/settled provider status, and kickoff-passed unknown beyond the bounded 3h window, must leave current Fixture Discovery / HOT identity / Tracked radar while preserving append-only audit history.
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

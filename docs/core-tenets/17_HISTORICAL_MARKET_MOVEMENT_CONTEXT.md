# Core Tenet 17 — Historical Market Movement Context

## Principle

Large or unusually rapid market movements are themselves important Research signals, but Sports Hedge must interpret them using historical context rather than treating movement direction as proof of what will happen next.

When a meaningful odds/probability move is detected, Sports Hedge should retrieve comparable historical movements for the same team/market/regime where data quality permits and show what happened afterwards.

## Non-negotiables

- Detect both **magnitude** and **speed** of movement in probability/logit space, not only raw decimal-odds differences.
- Keep movement windows explicit, for example 24h→kickoff, 6h→kickoff, 60m→kickoff, 15m→kickoff, and in-play windows where supported.
- Historical analogue matching must use canonical team/match/market identities and only comparable settlement semantics.
- Context should prefer the current team/manager regime when sample size is sufficient, while exposing broader team/league history when it is not.
- Outputs must expose sample size, data quality, timestamp/odds quality tier, confidence/uncertainty, and the analogue-selection rules.
- If comparable history is thin, say so. If the move is outside the observed historical distribution, explicitly surface **NO COMPARABLE HISTORICAL PRECEDENT** (or equivalent) rather than inventing a prediction.
- A historical relationship may be positive, negative, weak, or effectively uncorrelated. Sports Hedge must be willing to report **little/no observed relationship**.
- Movement correlation must never be presented as causation. A shortening price may reflect information already incorporated by the market, liquidity effects, team news, participant behaviour, or other factors.
- Historical movement context is a Research/Market Intelligence signal. It does not by itself make a bet executable and does not redefine arbitrage.
- Current odds/value assessment still uses fresh, economically equivalent executable prices after applicable costs.

## Example interpretation

A large Chelsea pre-kickoff shortening might produce a card such as:

- Current move: implied probability +8.4 percentage points in 42 minutes
- Historical comparable Chelsea moves: N=17
- Chelsea win rate after comparable moves: 59%
- Comparable pre-move baseline win probability: 56%
- Excess outcome signal: small / uncertain
- Current-manager comparable sample: N=3 — insufficient
- Classification: **HISTORICAL RELATIONSHIP WEAK**

Another event may instead show `NO COMPARABLE HISTORICAL PRECEDENT` if its magnitude/speed is outside the historical sample.

Values above are illustrative only and must never appear as real findings without real historical observations.

## What violates this tenet

- Showing a 20% odds shortening and claiming the team is therefore more likely to win without a benchmark.
- Comparing raw odds movement across materially different starting probabilities without probability/logit normalization.
- Mixing current-manager and old-manager eras silently.
- Calling a movement unprecedented merely because the current season has no match, while older valid data exists.
- Fabricating historical timestamp precision or intraday odds that a source does not provide.
- Hiding N, uncertainty, or data-quality limitations.
- Treating historical correlation as causal evidence.

## Review checks

1. Are movement magnitude and velocity calculated from timestamped observations with known precision?
2. Are comparisons made in probability/logit terms and against economically/semantically equivalent markets?
3. Can the system retrieve team, manager-era and league analogue cohorts without silently blending them?
4. Does every historical analogue result expose N, uncertainty and data quality?
5. Can the result explicitly be `WEAK/NO RELATIONSHIP`, `INSUFFICIENT HISTORY`, or `NO COMPARABLE HISTORICAL PRECEDENT`?
6. Are opening/closing-only sources prevented from masquerading as minute-by-minute history?
7. Is the current live price/value signal kept separate from the historical context signal?
8. Is correlation language kept distinct from causality?

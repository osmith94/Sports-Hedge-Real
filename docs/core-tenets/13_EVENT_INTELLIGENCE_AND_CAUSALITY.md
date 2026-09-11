# Core Tenet 13 — Event Intelligence & Causality

## Principle

Sports Hedge should align football/news events with market movement to study timing, reaction, reversion and cross-market lead/lag — but temporal proximity must not be presented as proof of causation.

## Event context

Important event categories include, where data exists:

```text
TEAM_SHEET
PLAYER_OUT
PLAYER_IN
INJURY_NEWS
JOURNALIST_REPORT
NEWS_ARTICLE
GOAL
RED_CARD
YELLOW_CARD
PENALTY
SUBSTITUTION
HALF_TIME
```

Each annotation should retain source time separately from retrieval/ingestion time, plus source/provenance and confidence.

## Analysis goals

Research should be able to measure, by event/team/market cohort:

- first market response;
- peak movement;
- time to peak;
- end-window movement;
- retracement/reversion;
- spread/liquidity change;
- cross-market lead/lag;
- sample size and stability.

Example product language:

> A TEAM_SHEET event occurred. Historically, match-result markets in this cohort moved before corners markets. This corners market has not yet moved.

Not:

> The team sheet caused the corners price to be wrong.

## Non-negotiables

- Preserve source timestamp and retrieval timestamp separately.
- Deduplicate provider events without silently discarding materially changed records.
- Unknown/ambiguous event mapping fails closed.
- Event annotations are temporal research context unless stronger evidence is explicitly established.
- Lead/lag results must expose N, uncertainty/data quality and market liquidity/spread context.
- Do not use event-timing analysis to introduce unauthorized courtsiding or latency-exploitation behavior.

## Review checks

- Can the system distinguish when an event happened from when Sports Hedge learned about it?
- Are causality claims avoided unless actually supported?
- Are repeated/changed provider events auditable?
- Are cross-market reaction results cohort- and sample-aware?

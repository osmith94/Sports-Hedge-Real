# Sports Hedge — Market Intelligence & Historical Movement Module

**Status:** Product / architecture proposal  
**Mode:** Research and paper analysis only  
**Primary sport:** Football

## 1. Purpose

Sports Hedge should not only answer **where an arbitrage exists now**. It should also retain enough market history to answer:

- What moved?
- How unusual was the move?
- When did it start?
- What happened around the move?
- Did the market subsequently continue or mean-revert?
- Is this behaviour typical for this team, competition, market type, or time-to-kickoff window?

This becomes a separate dashboard module called **Market Intelligence**.

The module is descriptive/research-first. It must not create directional live-betting execution in Phase 1.

---

## 2. Product Modules

Sports Hedge should expose three major analytical areas:

1. **Arbitrage**
   - current cross-venue discrepancies;
   - executable size;
   - fees, slippage and risk;
   - guaranteed paper payoff.

2. **Paper Portfolio**
   - simulated positions;
   - capital usage;
   - P&L;
   - capital velocity.

3. **Market Intelligence**
   - historical odds / probability movement;
   - unusual-move detection;
   - event annotations;
   - mean-reversion analysis;
   - historical team / league / market trend analysis.

These modules share the same canonical event and market identifiers.

---

## 3. Historical Market Data

Current-price snapshots are insufficient for Market Intelligence. Sports Hedge must retain timestamped observations.

For each canonical market/outcome snapshot store at minimum:

- timestamp UTC;
- venue;
- canonical event ID;
- canonical market ID;
- venue market / runner / token identifiers;
- best back / bid;
- best lay / ask where available;
- midpoint where meaningful;
- available size at touch;
- selected order-book depth levels;
- spread;
- total observed liquidity/depth where available;
- decimal odds;
- implied probability;
- snapshot latency / venue timestamp where available.

Do not overwrite market states. Historical observations should be append-only or represented by a time-series store.

---

## 4. Price Representation

Raw decimal odds should be retained, but analysis should primarily operate on probability-space measures.

Useful representations:

- decimal odds;
- raw implied probability: `1 / decimal_odds`;
- bid/ask probability range;
- midpoint implied probability;
- probability-point movement;
- percentage change in implied probability;
- log-odds / logit movement for statistical comparison.

Using probability/log-odds avoids misleading comparisons where the same change in decimal odds has very different meaning at 1.20 versus 8.00.

---

## 5. Adaptive Snapshot Frequency

Do not poll every market at maximum frequency continuously.

Suggested Phase 1 sampling policy:

- >24h before kickoff: low frequency;
- 24h–3h: moderate frequency;
- 3h–2h: higher frequency;
- 2h–45m: high frequency because team-sheet information often arrives in this region;
- 45m–kickoff: highest practical frequency within API limits/cost constraints;
- in-play: initially optional / lower priority unless explicitly enabled later.

The scheduler must respect each venue's API limits and economic cost.

Future versions can dynamically increase frequency when abnormal price movement is detected.

---

## 6. Event Annotation Timeline

A market chart is much more useful when price movement can be aligned to football events or information events.

Create a generic `MarketEventAnnotation` model with:

- event ID;
- timestamp;
- canonical football event ID;
- event category;
- source;
- confidence;
- title;
- optional structured metadata.

Initial categories:

- TEAM_SHEET;
- PLAYER_OUT;
- PLAYER_IN;
- INJURY_NEWS;
- MANAGER_NEWS;
- WEATHER;
- KICKOFF;
- GOAL;
- RED_CARD;
- PENALTY;
- HALF_TIME;
- FULL_TIME;
- MANUAL_NOTE;
- OTHER.

Phase 1 may begin with manual annotations and known timestamps. External news/team-sheet integrations can be added later.

Annotations must not be silently inferred as causal. The UI should say that an event occurred near a move, not that it caused the move unless supported by evidence.

---

## 7. Unusual Movement Detection

Create an **Unusual Movement Score** rather than using a single absolute odds threshold.

Candidate inputs:

- implied-probability move over 1m / 5m / 15m / 30m / 60m;
- log-odds return;
- movement relative to historical volatility;
- spread expansion/contraction;
- change in available liquidity;
- order-book imbalance;
- cross-venue agreement/disagreement;
- time to kickoff;
- market family;
- competition;
- team(s);
- baseline price bucket.

A move should be judged relative to an appropriate historical cohort.

Example cohorts:

- Premier League 1X2 favourites 45–90 minutes before kickoff;
- Newcastle home 1X2 markets;
- Over 2.5 markets in La Liga;
- favourites priced between 1.50 and 2.00;
- team-sheet-window moves for the same team.

Initial implementation can use robust z-scores / percentile ranks before more sophisticated models are justified.

---

## 8. Mean Reversion Analysis

For each detected movement event, record a reference point and evaluate subsequent windows.

Example windows:

- +1 minute;
- +5 minutes;
- +15 minutes;
- +30 minutes;
- +60 minutes;
- kickoff;
- close.

Metrics:

- peak move from baseline;
- percentage of move retraced;
- time to 25% / 50% / 75% retracement;
- continuation beyond initial move;
- closing probability versus pre-event probability;
- liquidity/spread before and after;
- cross-venue convergence time.

Definitions should distinguish:

- **reversion** — price moves back toward the pre-shock baseline;
- **continuation** — price extends in the direction of the initial shock;
- **stabilization** — price remains near the new level.

No result should be labelled mean-reverting until a defined historical window has elapsed.

---

## 9. Historical Trend Analysis

Allow the user to explore patterns by:

### Team
- home vs away;
- favourite vs underdog;
- pre-team-sheet drift;
- post-team-sheet reaction;
- average pre-kickoff volatility;
- frequency of >X percentile moves;
- median reversion/continuation after large moves.

### Competition
- liquidity profile;
- spread profile;
- volatility by time to kickoff;
- team-sheet sensitivity;
- market convergence behaviour.

### Market family
- 1X2;
- totals;
- BTTS;
- DNB;
- double chance;
- future supported canonical markets.

### Price bucket
Movement should also be analyzed by starting implied probability/price, because behaviour at short odds is not directly comparable with long odds.

---

## 10. Dashboard Experience

### Market Intelligence landing page

Show:

- largest unusual moves now;
- highest movement scores;
- markets with spread/liquidity shocks;
- recent annotated events;
- largest cross-venue divergences;
- strongest historical mean-reversion / continuation cohorts.

### Event detail

Primary visualization:

**Historical probability / odds chart**

- one line per selected venue or consolidated reference price;
- selectable outcome;
- event annotations on the timeline;
- team-sheet marker;
- kickoff marker;
- optional spread/liquidity panels;
- selectable windows such as 24h, 6h, 3h, 90m, 30m;
- hover with exact timestamp, odds, implied probability, spread and liquidity.

Supplementary cards:

- current movement percentile;
- movement score;
- pre-event baseline;
- peak move;
- current retracement;
- historical cohort mean/median;
- sample size;
- cross-venue convergence.

### Historical Trends page

Filters:

- team;
- competition;
- venue;
- market family;
- favourite/underdog;
- home/away;
- price bucket;
- event annotation type;
- time-to-kickoff bucket;
- date range.

Outputs:

- movement distributions;
- percentile tables;
- mean-reversion curves;
- continuation rates;
- liquidity/spread trends;
- sample size and confidence context.

---

## 11. Cross-Venue Signal

Market Intelligence should retain venue-specific prices as well as a normalized/consolidated view.

This allows analysis of:

- which venue moves first;
- lag between venues;
- duration of divergence;
- whether divergence closes through one venue moving or both;
- whether liquidity disappears before price movement;
- whether an arbitrage opportunity coincided with an unusual movement event.

This is research data only in Phase 1. It must not become a latency-arbitrage execution strategy.

---

## 12. Data Model Additions

Likely persistence entities:

- `market_snapshots`;
- `order_book_snapshots`;
- `market_event_annotations`;
- `movement_events`;
- `movement_metrics`;
- `historical_cohort_stats`;
- `team_aliases` / canonical entity references.

Indexes should prioritize:

- canonical event + market + outcome + timestamp;
- team + market family + kickoff-relative time;
- competition + market family + kickoff-relative time;
- movement-score / event-annotation queries.

---

## 13. Important Statistical Guardrails

The dashboard must expose sample size and avoid turning noise into a claimed pattern.

Requirements:

- show N for historical cohorts;
- use robust statistics where possible;
- distinguish median from mean;
- avoid presenting correlation as causation;
- avoid comparing non-equivalent market families;
- normalize by time-to-kickoff and starting-price bucket where relevant;
- handle missing snapshots explicitly;
- flag thin-liquidity markets separately.

Historical movement should be interpreted alongside spread and depth. A 5% probability move in a thin market is not equivalent to the same move in a deep market.

---

## 14. Relationship to Arbitrage

The Market Intelligence module should share raw data with the arbitrage engine but remain architecturally separate.

Potential future uses:

- explain why an arb appeared;
- show whether the underlying market is unusually unstable;
- contribute to execution-risk scoring;
- identify periods when quoted prices are likely to disappear quickly;
- analyze how long cross-venue discrepancies historically survive.

Do not use historical directional patterns to convert the Phase 1 arbitrage system into a directional betting bot.

---

## 15. Phase 1 Build Sequence

1. Persist timestamped market snapshots.
2. Add kickoff-relative time to each snapshot.
3. Build historical probability chart.
4. Add manual/system timeline annotations.
5. Add team-sheet annotation support.
6. Implement rolling movement calculations.
7. Add unusual-movement percentile / robust z-score.
8. Implement post-move reversion metrics.
9. Add team / competition / market cohort queries.
10. Add Market Intelligence dashboard pages.

---

## 16. Phase 1 Definition of Done

The module is useful when Sports Hedge can answer, for a selected football market:

> The home-win implied probability moved from X to Y over Z minutes. This was in the Nth percentile for comparable markets. A team-sheet event occurred at time T. Historically, moves of this size for the selected cohort reverted by a median R% after 30 minutes / by kickoff, based on N observations.

The wording should remain descriptive and evidence-based rather than implying causality or guaranteed predictive value.

# UNIVERSE scale: indexed clustering (Phases 1–4)

Status: implemented on owner-live for issue #466.
Data class: live UNIVERSE observations motivated the change; tests in this
wave use synthetic/fixture events unless a soak notes otherwise.

## Problem

With Polymarket enabled across the selected competitions, a UNIVERSE generation
surfaced ~1,640 canonical fixtures. The 150s chunk was consumed by naive
all-pairs clustering (`ClusterPass.pairs()`), so market evaluation never ran:

- 0 market matches
- 0 evaluated / almost all leftover
- UI: `Not evaluated — scan budget exhausted`

## Core principle

**discover broadly, match cheaply, evaluate narrowly**

Matchbook + Kalshi + Polymarket stay enabled. Provider concurrency is unchanged.
`EventMatcher` remains the final identity arbiter. Thresholds, the hard
competition veto, squad-category safety, NFL strict identity, aliases/learned
mappings, and market-family equivalence are not widened.

Ambiguous connected components are solved by the identity graph in
`docs/FIXTURE_IDENTITY_GRAPH.md` (#472). Indexed candidate generation stays
the fast first stage; global assignment is not used for obvious cliques.

## Phase 1 — indexed candidate generation

`build_indexed_candidates()` replaces N² enumeration with a correctness-preserving
superset blocked on:

- sport
- known target competition code (unresolved labels use a safe fallback path
  that still pairs against same-sport kickoff neighbours)
- overlapping kickoff windows for the current ±5 minute hard rule, using
  adjacent buckets so a pair on a bucket boundary is not dropped
- soccer squad-category fingerprints
- `(venue, source_event_id)` identity (no self-pairs)

`EventMatcher.could_match()` is the official conservative prefilter. Index
generation builds the could-match graph and then keeps every compatible pair
inside a connected component so veto/miss evidence on a matching path is
never dropped. Pairs that cannot share a component are not materialised.
``match()`` still runs only on the remaining candidates. Same-venue sibling
events (PM/Kalshi family splits) still union when they fall in the kickoff
window.

Candidates are ordered by kickoff/name locality so a truncated chunk can
finish every incident pair of the earliest fixtures and assign them. Global
cross-venue-first order is not used: it left almost every node unscored until
the entire list finished.

Diagnostics (existing `scan_diagnostics`, not a new telemetry system):

- `naive_pair_space`
- `candidate_pairs_generated`
- `candidate_pairs_considered`
- `pairs_pruned_by_index`
- `pairs_skipped_by_generation_cache`
- `pairs_rejected_by_could_match`
- `unresolved_competition_events` / `max_sport_bucket_size`
- `clustering_resume_applied` / `clustering_resume_candidates_reused`
- `clustering_resume_cursor_before` / `clustering_resume_cursor_after`
- `candidate_reduction_pct`
- `cross_venue_clusters` / `single_venue_clusters`
- `clustering_duration_ms`
- `clustering_truncated`

`max_event_pairs` remains a positive bound for API compatibility and still
does not drop a multi-venue cluster.

## Phase 2 — cross-venue-first market evaluation

UNIVERSE clusters are ordered multi-venue first (`_select_lane_clusters` only;
HOT and unlaned collection keep discovery order). Single-venue rows stay in
the inventory/current-state with

`market_evaluation_state = single_venue_no_cross_venue_candidate`

They are not marked evaluated and do not fetch markets/order books. Identity
metadata is preserved so a later chunk or generation can attach another venue.

Approved catalogue persistence still requires settlement-equivalent pairs.
There is no global one-sided market fetch. `cluster_needs_one_sided_catalogue_markets()`
is the explicit opt-in if an approved workflow later needs it.

HOT is unchanged: targeted refresh still fetches the scoped fixtures.

## Phase 3 — clustering stage reserve

On a ~150s UNIVERSE chunk, clustering yields to market evaluation after an
adaptive reserve (`clustering_market_eval_reserve_seconds`, default 30% /
cap 45s). Tiny remaining budgets (<5s, including tests) do not starve
clustering.

If candidate `consider()` is truncated:

- union-find progress is checkpointed in the generation-scoped cache
- skipped pairs are not marked complete
- `clustering_truncated` keeps the generation open (no 30/60-minute cadence
  until identity work actually finishes)
- already-found multi-venue clusters can still be evaluated in the reserved
  slice

Provider concurrency is not increased.

## Phase 4 — generation-scoped negative/candidate cache

`GenerationIdentityCache` is process-local and keyed by UNIVERSE generation
id. After a complete clustering pass, single-venue events record “no
cross-venue candidate” against the then-known other-venue set. Later chunks
of the same generation skip that proven-negative fuzzy work unless metadata
changes or a new other-venue event appears.

Clustering resume (union-find + cursor) is kept only in memory for the live
process. Durable SQLite checkpoints stay compact and do not store venue
bodies.

Invalidation (generation-scoped resume/negatives only):

- new generation id (`bind`)
- generation close
- operator Clear & update (`LiveRefreshCoordinator.reset`)

Cross-generation fingerprints (#471) survive generation close and bind.
They are discarded on Clear & update, semantic-version mismatch, or process
restart (process-local; restart correctly re-checks).

A process restart correctly re-checks identity.

## Phase 5 — deep vs near horizon (design only)

Not implemented. No 7/14-day cutoff is approved.

A later owner-approved approach could:

1. keep every selected fixture discoverable (identity + inventory)
2. give near-term multi-venue clusters full market-evaluation priority
3. keep long-dated single-venue rows as cheap metadata until they become
   multi-venue or are promoted by kickoff proximity / HOT membership

That would be an extra priority key inside the existing UNIVERSE chunk, not a
second scanner, not a coverage reduction, and not a silent time-horizon gate
on discovery. Any cutoff needs an explicit owner decision and a documented
data-class label so long-dated cheap rows are not mistaken for evaluated
markets.

## Phase 6 — cross-generation incremental identity (#471)

Implemented in `docs/FIXTURE_IDENTITY_INCREMENTAL.md`. Clean full
recomputation remains the correctness oracle. Pairwise EventMatcher evidence
is reused across UNIVERSE generations only when the identity-relevant
fingerprint and the matcher/alias/competition-registry semantic version are
unchanged. Operator Clear & update invalidates the cache; generation close
does not. Truncated clustering does not commit a snapshot.

## Phase 7 — competition shards (#511)

Implemented in `universe_identity_shards.py`. After scope filtering, normalised
events are partitioned by `(sport, target_competition_code)` before the
identity graph. Exact verified Polymarket Gamma series ids and Kalshi series
tickers fill that code when the canonical label does not resolve, and the
registry display name is stamped so EventMatcher scores a known competition.
Thresholds are unchanged. Events that still cannot be proven stay in an
explicit `*unresolved*` shard.

Each shard has its own candidate list, signature and resume cursor. A
discovery change in one competition does not invalidate the others
(`global_resume_invalidated_by_discovery` stays false). Shards keep first-seen
order rather than shard-id sort. Scheduling is multi-venue, then single-venue,
then hot/unresolved with a smaller pair slice, and market evaluation walks
that same order. The soft scan deadline stops before the next fixture, so an
alphabetically earlier competition cannot consume the only evaluation slot,
and a partial hot shard cannot relabel a fixture already evaluated. Hot and
unresolved shards also apply a fail-open name canopy before fuzzy scoring;
EventMatcher remains the oracle.

Diagnostics on `scan_diagnostics` include `blocking_shard_key`,
`largest_shard_*`, per-shard timings, provenance counts, and whether a
discovery snapshot change kept other shard resumes. Data in the #511 tests
are synthetic fixtures, not live quotes.

## Invariants preserved

- PAPER-only, no venue writes
- soccer PAPER matcher injection remains 0.80 where currently composed
- default matcher 0.92
- HOT / BACKGROUND / ACTIVE independent of this clustering path
- settlement worker untouched

# Cross-generation incremental fixture identity

Status: implemented on top of indexed clustering (#466 / PR #468) and the
identity graph (#472 / PR #483).
Issue: #471.
Data class: tests in this wave use synthetic/fixture events. Not live,
historical, or modelled quotes. PAPER only.

## Problem

UNIVERSE generations rediscover largely the same provider events. Indexed
clustering and the identity graph made one generation cheap enough to finish,
but the next generation still scored every EventMatcher candidate from scratch.

## Core principle

**Clean full recomputation is the correctness oracle.** Incremental clustering
may reuse prior pairwise identity evidence only when:

1. the identity-relevant fingerprint of both source events is unchanged; and
2. the cache semantic version still matches the current matcher threshold,
   kickoff window, assignment margin, competition-registry version, curated
   alias catalog, and enabled learned-rule versions.

Changed and new events are recomputed. Removed events are retired from the
snapshot. A matcher / alias / registry version bump forces re-evaluation even
when source fingerprints did not change. Operator Clear & update invalidates
the cache. Generation close does not.

## Fingerprint

One compact digest per provider source event over the fields that can change
fixture identity:

- venue / source event id
- sport
- competition label and resolved target-competition code
- home / away names
- kickoff
- soccer squad-category fingerprints

Raw venue bodies, credentials, and other secrets are never fingerprinted or
persisted. The record schema is `(venue string, source_event_id)` so a new
provider does not require redesign.

## Semantic version

`identity_cache_semantic_version()` hashes:

- `IDENTITY_CACHE_SEMANTIC_VERSION` (algorithm/contract bump)
- EventMatcher threshold
- kickoff tolerance seconds
- identity-graph assignment margin
- `OPERATOR_COMPETITION_REGISTRY_VERSION`
- curated alias-catalog digest
- enabled learned-rule id:version digest

Mismatch → treat every event as changed/new and re-score every candidate.

## Algorithm

1. Discover the current source snapshot (UNIVERSE only).
2. Classify events against the previous committed snapshot: unchanged,
   changed, new, removed.
3. Build indexed candidates for the full snapshot (Phase 1, unchanged).
4. For each candidate, reuse the cached EventMatcher score when both
   fingerprints and the semantic version match; otherwise call EventMatcher.
5. Run the identity graph on the full scored set. Incremental reuse never
   invents an edge the matcher did not produce.
6. Commit the compact snapshot only when clustering completed. Truncation
   keeps the previous snapshot.

Intra-generation resume/negatives stay on `GenerationIdentityCache` (Phase 4).
Cross-generation fingerprints live on `CrossGenerationIdentityCache`.

## Diagnostics

Attached to existing `scan_diagnostics`:

- `identity_events_discovered`
- `identity_unchanged_reused`
- `identity_changed_recomputed`
- `identity_events_new`
- `identity_events_removed`
- `identity_cache_hit_pct`
- `identity_saved_candidate_comparisons`
- `identity_cache_semantic_version`
- `identity_cache_semantic_version_mismatch`

## Invariants preserved

- PAPER-only, no venue writes
- EventMatcher remains the only pairwise identity oracle
- identity graph fail-closed behaviour unchanged
- HOT / BACKGROUND / ACTIVE independent; HOT does not write this cache
- settlement worker untouched
- approved catalogue / market-family equivalence unchanged
- compact UNIVERSE checkpoint (#334) still excludes venue bodies

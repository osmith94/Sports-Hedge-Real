# Wave G scanner validation

## Scope and data classification

This note separates three different evidence classes:

- owner-observed scheduled timings on the failed candidate are live runtime
  evidence: Fast Scan about 58s and Full Sweep about 148/150s with 53 fixtures;
- the benchmark in `backend/tests/test_scanner_synthetic_stress.py` uses
  deterministic fixture/demo provider responses and controlled latency;
- credentialed real-provider benchmarking remains separate and must not inherit
  a synthetic SLA.

No benchmark fixture or opportunity is exposed through the production UI.

## Manual versus scheduled paths

Primary **Run scan** now calls `POST /paper/collect/hot`.

That endpoint asks `LiveRefreshCoordinator.manual_hot_plan()` for the same plan
shape used by scheduled Fast Scan:

- current HOT identities from the existing `FixtureCurrentStateStore`;
- known per-venue source events already retained for those identities;
- current HOT venue participation;
- 25s collector budget plus 5s coordinator return grace;
- the unchanged collection and paper-decision pipeline.

Passing a non-null HOT identity scope, including an empty list, instructs
`ReadOnlyCrossVenueCollector` to skip universe event discovery. An empty HOT
scope therefore returns an honest empty refresh; it does not fall through to a
broad scan. Manual HOT does not advance scheduled HOT due time, UNIVERSE cursor,
generation work, or UNIVERSE due time. If scheduled HOT/UNIVERSE owns the
coordinator, the endpoint returns HTTP 409 `scheduled scan in progress` instead
of queueing another scanner.

The old `POST /paper/collect` path remains available as **Run full diagnostic**
under Advanced. It intentionally:

1. discovers events across all enabled UNIVERSE venues;
2. normalizes and clusters the venue-union inventory;
3. evaluates up to the request pair limits;
4. fetches venue markets and executable books for each selected cluster;
5. runs settlement/equivalence, freshness, depth, fee/FX, risk, solver and
   allocation gates;
6. returns a truthful partial inside its 20s collector plus 5s coordinator
   envelope, omits redundant nested market inventory from the HTTP body, then
   persists after the body.

The browser's existing 60s diagnostic timeout is only a client guard. Increasing
it would hide broad one-shot latency rather than make the primary operator action
honest. Scheduled Fast Scan can remain healthy because it skips discovery and
evaluates only HOT current identities, while Full Sweep uses resumable chunks
that yield to HOT. A broad diagnostic can therefore approach its independent
envelope without implying a scheduled-lane bottleneck.

## Synthetic assumptions and results

The deterministic benchmark uses:

- Matchbook and Polymarket fixture/demo payloads with one settlement-equivalent
  both-teams-to-score market per fixture;
- fixed 2ms latency for every synthetic provider call;
- two Polymarket token-book reads per fixture;
- 2s scaled collector budget;
- 1, 4, 16 and 50 fixture workloads for both HOT and UNIVERSE;
- HOT seed setup excluded from measured wall time;
- twelve repeated 16-fixture cycles for each lane;
- no real credentials, network variance, provider throttling or production SLA
  assertion.

Measured exact-head results are recorded in
`SCANNER_SYNTHETIC_VALIDATION.md`. Every workload must report provider/stage attribution, zero provider
cancellations, zero orphans and zero live in-flight tasks. The repeated-cycle
test also requires no event-loop task delta and no progressive second-half
slowdown.

Bounded concurrency is retained because source/stage attribution and controlled
latency both identify serial cluster plus venue market/depth work as the scaling
topology. Cluster fan-out is capped at 8 and explicit provider semaphores cap
Matchbook/Polymarket/Kalshi at 4/8/4. These limits are settings, appear in scan
diagnostics together with observed peaks, and do not imply a real-provider SLA.
See `WAVE_G_SCANNER_PERFORMANCE.md` for higher-latency results and stalled
provider behavior.

## 53 fixtures / zero cross-venue / zero equivalent

`fixtures discovered` is a venue-union inventory count, not a cross-venue count.
The compact UI also previously mixed generations: fixture/equivalent totals
came from merged Fast+Full inventory while cross-venue used the most recently
completed lane's `last_matched_event_pairs`. Cross-venue now derives from each
current row's venue flags. The observed `53 / 0 / 0` can still be legitimate
when current providers expose disjoint fixtures:

- `matched_event_pairs == 0` means no two venue events formed a canonical
  cross-venue cluster;
- `matched_equivalent_count == 0` follows necessarily when no event pair exists;
- a fixture may still be retained truthfully with one venue flag and a
  `no_comparison_reason`;
- zero qualifying opportunities is required when no equivalent executable pair
  exists.

The response already contains the evidence needed to distinguish coverage from
a matcher regression: raw and normalized counts by venue, `pair_counts`,
`target_coverage`, per-fixture venue flags, source event IDs, kickoff,
competition and `no_comparison_reason`.

Interpretation:

| Evidence | Conclusion |
| --- | --- |
| Each of the 53 rows has only one venue flag, with no same fixture offered by another enabled venue | legitimate current provider coverage gap |
| A clearly identical fixture exists on two venues but their normalized event records differ in competition, participants or kickoff beyond tolerance | provider metadata/mapping case requiring review; fail closed |
| Normalized records have the same competition, participants and kickoff but still do not pair | event-matching regression |
| Events pair but market period/line/outcomes/settlement differ | legitimate settlement non-equivalence; do not relax rules |
| Events and markets are equivalent but books are stale, unavailable or lack executable depth | legitimate non-qualifying executable state |

Deterministic tests already prove exact cross-venue fixtures pair and that
single-venue target coverage remains unmatched without invented markets. The
aggregate owner screenshot alone does not contain source IDs or normalized
fields, so it cannot establish which row of the table applies to that live
53-fixture sample. Capture the full diagnostic response (with secrets removed)
or inspect fixture drill-down before classifying the owner state. Do not
fabricate opportunities or weaken canonical/settlement checks to change the
counts.

## Mapping Verify

Mapping Verify is an operator-only review path. Prompt construction is local,
synchronous string generation behind `/paper/mapping-reviews/prompt`; scanning
does not import or call `MappingReviewService` and makes no ChatGPT/OpenAI
request. Operator-supplied ChatGPT text cannot activate a rule without explicit
confirmation. The scanner uses only already-active deterministic learned rules
inside canonical matching.

## Safety/economic review

- Venue clients remain read-only; execution remains paper-only.
- Manual HOT uses the same canonical settlement, freshness, executable taker
  depth, fees, FX, risk, allocator and treasury gates as scheduled collection.
- Persistence remains append-only/idempotent and occurs outside the response
  timing envelope.
- No SQLite database is deleted, reset or replaced.
- Empty and degraded current states remain visible; no fallback opportunity is
  generated.

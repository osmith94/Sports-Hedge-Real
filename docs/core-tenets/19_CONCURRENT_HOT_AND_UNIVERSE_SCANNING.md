# Core Tenet 19 — Concurrent HOT and UNIVERSE Scanning

## Principle

Sports Hedge must separate **catalogue discovery** from **price surveillance**.

The authoritative runtime model is:

- **UNIVERSE** is the broad, durable catalogue-maintenance worker for the approved Matchbook↔Kalshi families.
- **One price engine** derives work from that durable catalogue and owns full ACTIVE-catalogue economics coverage.
- **HOT** and **BACKGROUND** are priority tiers inside that price engine. HOT is frequent and freshness-sensitive; BACKGROUND is slower but must eventually cover every remaining ACTIVE row.
- The HOT pricing loop and UNIVERSE catalogue worker are independently scheduled and may overlap in wall-clock time. They are not two competing discovery scanners.

They serve different product purposes and must not be serialized into one shared scan cycle.

A HOT refresh becoming due must **never terminate, reset, restart, discard or artificially time-slice an active UNIVERSE sweep**.

A UNIVERSE catalogue sweep may take several minutes. That is acceptable and expected if it is making forward progress. HOT must continue to run during that time.

The architectural contract is:

> **UNIVERSE catalogues. The price engine prices every ACTIVE catalogue row. HOT is a priority tier inside that engine. Execution decides.**

UNIVERSE does **not** require executable books or solver/economics to finish a catalogue item. Pricing work is derived from the durable catalogue; it is not a second durable work-queue authority.

## Authoritative scanner operating model

This is the non-negotiable logical flow for normal runtime scanning:

```text
VENUE EVENT / MARKET LISTINGS
        ↓
UNIVERSE
broad discovery + canonical fixture identity
+ approved-family recognition
+ exact native market/contract IDs
+ exact outcome mapping / parameters
+ family-scoped lifecycle/invalidation
        ↓
DURABLE APPROVED-MARKET CATALOGUE
source of truth for what may be priced
        ↓
ONE PRICE ENGINE
derive work from ACTIVE catalogue rows
        ↓
HOT priority        BACKGROUND priority
frequent refresh    slower eventual coverage
exact known IDs     exact known IDs
        ↓
net economics / opportunity state
        ↓
promotion / watchlist / PAPER decision
        ↓
treasury + fill simulation / execution layer
```

The key architectural separation is:

> **UNIVERSE discovers and maintains identity. The catalogue remembers it. The price engine reprices it. HOT only changes priority.**

Once an ACTIVE row contains the canonical fixture, Approved Match Register key, exact native IDs and required outcome mapping, routine HOT/BACKGROUND refresh must **not** redo broad event discovery, fixture matching, market equivalence review, or settlement re-proof. It should fetch the exact known native market/book inputs required to price that catalogue row.

A new competition, fixture set, or approved market family must enter through the same flow. Expansion is allowed to increase the number of catalogue rows; it must not create a parallel scanner, second matcher, second catalogue, or league-specific execution path.

### Catalogue completeness and partial discovery

Catalogue disappearance/invalidation must be based on **family-scoped discovery completeness**, not a fixture-wide assumption.

For example, successful GAME and BTTS discovery does not prove that TOTAL or FTTS was checked successfully. If TOTAL discovery times out, is deferred, or was not queried, an existing TOTAL row must remain unconfirmed/retryable rather than being marked disappeared merely because another family succeeded.

Parameterized families such as `TOTAL_GOALS_FT:{line}` are separate catalogue rows per exact approved line. Adding 2.5, 3.5, 4.5, 5.5, etc. increases pricing workload but does not change scanner architecture.

### Architecture-preservation rules

Normal scanner changes must preserve all of the following:

- no global scan lock between UNIVERSE and price-engine work;
- no broad rediscovery/rematching on every HOT cadence;
- no separate HOT matcher or HOT-only catalogue;
- no durable second pricing queue that competes with the catalogue as scheduler truth;
- no provider concurrency increase merely to hide architectural inefficiency;
- one shared provider-access layer with bounded concurrency and anti-starvation;
- item-level timeout/retry isolation so one failing market does not stall unrelated catalogue rows;
- completed catalogue or price items publish incrementally rather than waiting for a whole sweep/slice;
- operator diagnostics are read-only and must not create provider load merely to explain provider load;
- scaling decisions should be judged by actual catalogue items, due work, cycle utilisation and provider queues, not fixture count alone.

Any proposal that requires materially changing this flow must be treated as an architectural change and reviewed explicitly against this tenet before implementation.

## Why this is a core tenet

Sports Hedge loses product value if broad catalogue maintenance is repeatedly interrupted by fast surveillance, or if economics coverage collapses to whatever is already on the HOT roster.

The scanner must be capable of doing both jobs at once:

1. continuously maintaining a durable catalogue of approved-market identities across the configured universe; and
2. rapidly refreshing already-interesting fixtures **without** leaving the remaining ACTIVE catalogue unpriced.

These workloads are complementary, not mutually exclusive.

A design where UNIVERSE receives only the leftover seconds before the next HOT deadline creates predictable failure modes:

- broad `list_events` discovery begins but is cancelled before useful work completes;
- provider health appears failed even when targeted HOT calls work normally;
- the same early portion of the universe is revisited repeatedly;
- later fixtures may never be catalogued;
- successful work can be delayed until an entire batch completes;
- opportunities are missed because promotion cannot occur before interruption;
- scanner history becomes dominated by tiny partial cycles rather than completed sweeps.

A design where only HOT-priority rows are priced creates a second, equally prohibited failure mode:

```text
outside HOT horizon → not priced → cannot discover edge → cannot promote to HOT
```

Every ACTIVE supported catalogue row must eventually be refreshed and evaluated, including:

- rows outside the HOT lifecycle horizon;
- rows that have never shown an edge.

Sports Hedge must therefore prefer **concurrent worker architecture with shared provider coordination**, not a single global scan lock with alternating HOT/UNIVERSE turns, and not exclusive HOT-only pricing.

## 1. HOT worker

HOT exists for fast surveillance. It is a **priority tier inside the price engine**, not a separate market catalogue and not the only set of rows that may be priced.

Typical HOT membership includes:

- in-play fixtures;
- fixtures close to kickoff;
- already-interesting fixtures;
- fixtures promoted by opportunity logic;
- fixtures with significant cross-venue divergence;
- fixtures near the configured trade trigger;
- fixtures with large or rapid price movement;
- manually pinned fixtures where supported.

HOT characteristics:

- independently scheduled;
- typically short cadence, for example around 30 seconds;
- small canonical fixture set relative to the full ACTIVE catalogue;
- targeted source-event and market refresh from catalogued exact native IDs;
- high request priority when provider capacity is constrained;
- does not perform unnecessary broad event discovery when source identities are already known;
- uses the same approved-market catalogue, Approved Match Register, venue recognition/equivalence rules and current-state store as UNIVERSE.

HOT is a surveillance lane and a frequent-cadence pricing tier. HOT membership is not itself permission to trade.

## 2. UNIVERSE worker

UNIVERSE exists for durable catalogue completeness.

UNIVERSE characteristics:

- independently scheduled;
- long-running full sweep;
- may take several minutes;
- broad venue event discovery;
- deterministic canonical fixture construction;
- Approved Match Register canonical key;
- exact native market/contract IDs and outcome mapping;
- lifecycle and invalidation;
- compact Kalshi fee-metadata capture where the listing payload already carries it (not quotes);
- immediate persistence of completed catalogue work;
- durable sweep progress.

UNIVERSE owns catalogue completeness for the approved Matchbook↔Kalshi families. It should aim to reach the end of the eligible configured universe, not merely process whichever fixtures fit before the next HOT interval.

UNIVERSE does **not** need executable books, solver output, or paper economics to finish a fixture’s catalogue job. A row may be ACTIVE, `NOT_LISTED`, `VENUE_UNAVAILABLE`, `TERMINAL`, `DISAPPEARED`, or `INVALIDATED` without a fresh order book.

## 3. Price engine

One price engine owns **full ACTIVE-catalogue economics coverage**.

The engine derives in-memory work from the durable catalogue. The durable catalogue is the scheduler source of truth. Pricing work, in-flight leases, and short retry/backoff are process-memory by default. Sports Hedge must **not** introduce a durable pricing-work queue, `next_retry_at` checkpoint, or second matcher into this tenet.

Every ACTIVE supported catalogue row is eventually refreshed and evaluated, including rows outside the HOT lifecycle horizon and rows that have never shown an edge.

Priority tiers:

- **HOT** — in-play, near kickoff, already-interesting, opportunity-promoted; frequent cadence. Default **30s**, operator-adjustable within the existing safe range. HOT cadence is independent of BACKGROUND and UNIVERSE.
- **BACKGROUND / CATALOGUE** — the remaining ACTIVE rows, at a slower bounded cadence. Default **90s**, operator-adjustable within **60–600s** (`background_cadence_seconds`). Environment fallback is `paper_background_price_interval_seconds`. BACKGROUND does not rediscover, rematch, or wait for UNIVERSE cadence.
- **UNIVERSE discovery** — generation 0 is due immediately. Incomplete generations resume/chunk/retry as today; a bounded **~8s** intra-generation yield (`paper_universe_worker_cooldown_seconds`) may delay chunk continuation without using the discovery interval. After a terminal-complete generation, the next fresh discovery generation waits the operator UNIVERSE cadence (default **1800s**, adjustable **60–3600s**, `universe_cadence_seconds`). Environment fallback is `paper_universe_discovery_interval_seconds`. Operators may **pause scheduled UNIVERSE scans** (`universe_scans_paused`) so the periodic timer does not start a new generation; this is not a giant-cadence hack. BACKGROUND, HOT and ACTIVE TRADE continue. Pause is persisted and survives restart: startup still hydrates saved scope, performs one fresh generation, then remains paused. `UNIVERSE now`, a material selected-scope change, and that startup refresh remain one-shot exceptions; after they complete, periodic scheduling stays paused until Resume. Resume restarts from now + the persisted cadence with no catch-up burst. Pause/resume must not themselves call providers, raise concurrency, or create a second worker. Provider retry/backoff inside an open generation may resume sooner. These cadences are independent authorities; do not reuse radar membership interval, worker cooldown, or generation budget as discovery cadence. Updating the cadence reschedules the next fresh generation and must not itself run UNIVERSE. `UNIVERSE now` remains the explicit wait bypass.

A positive, near, or qualifying BACKGROUND decision may promote that row to HOT immediately, without waiting for kickoff or the lifecycle horizon. Promotion does not stop UNIVERSE.

Lane classification decides **HOT vs BACKGROUND priority**, not whether the row is priced. An ACTIVE row outside the HOT horizon is BACKGROUND-priced. Exclusive HOT-only pricing is prohibited.

The price engine refreshes exact catalogued native IDs (complete required outcome set), applies known fees/FX (fail closed if unknown or partial), and publishes the paper decision when that item is complete. It must not wait for the rest of the HOT slice, BACKGROUND slice, or UNIVERSE chunk before a completed item may be published.

Kalshi fee metadata is a first-class fail-closed economics input. It is not quote data. Unknown, partial, or unsupported fee metadata must not become zero and must not be treated as an executable book.

Paper capture remains subject to the merged OPEN-or-`PAPER_FILL_REJECTED` contract. Auto-capture must not silently vanish a paper-eligible item.

## 4. True concurrency is required

HOT and UNIVERSE must be able to be active during the same wall-clock interval.

For example:

```text
18:00:00  UNIVERSE catalogue sweep starts
18:00:30  HOT-priority refresh starts while UNIVERSE is still running
18:00:33  HOT-priority refresh completes
18:00:33  UNIVERSE continues from its current position
18:01:00  HOT-priority refresh starts again
18:04:20  UNIVERSE reaches the final catalogue item and completes
```

The fact that HOT runs at 18:00:30 must not cause UNIVERSE to return to fixture 1, close its generation, discard its event set, or lose already-completed catalogue work.

BACKGROUND-priority pricing may also run during an active UNIVERSE sweep. It must not cancel UNIVERSE, and a UNIVERSE chunk watchdog must not cancel unrelated price-engine items.

Concurrency may be implemented using asynchronous tasks in one backend process. Separate operating-system processes are not required unless future operational evidence justifies them.

## 5. Independent scheduling does not mean uncontrolled provider traffic

HOT and UNIVERSE are independent workers, but venue access must be coordinated.

Both workers, and both price-engine tiers, should use a **shared provider-access layer** for Matchbook, Kalshi, Polymarket and any future venue.

The shared layer is responsible for:

- provider concurrency limits;
- rate-limit handling;
- cooldown and backoff state;
- HTTP/session reuse;
- identical-request coalescing or caching where safe;
- provider-specific retry rules;
- request attribution and diagnostics;
- priority arbitration when capacity is constrained.

HOT-priority requests may receive higher request priority because their value depends on freshness.

However:

> HOT priority may delay an individual UNIVERSE or BACKGROUND provider request; it must not cancel or reset the UNIVERSE sweep.

If provider capacity allows simultaneous safe requests, HOT and UNIVERSE may both make provider calls concurrently.

Preserve existing provider slot caps and anti-starvation semantics unless a later reviewed change proves otherwise. A continuously busy HOT roster must still yield slots so UNIVERSE catalogue work and BACKGROUND pricing can progress.

## 6. No global scan-cycle lock

Sports Hedge must not use one global lock that prevents HOT and UNIVERSE from running concurrently.

Locks should be scoped to the resource that actually needs protection.

Appropriate lock boundaries may include:

- one provider connection or rate-limit bucket;
- one cache mutation;
- one canonical fixture upsert;
- one catalogue-row upsert;
- one paper-position persistence transaction;
- one scheduler-state mutation.

An architectural lock equivalent to:

```text
if any scan is running:
    the other lane cannot run
```

violates this tenet.

The scanner should instead maintain distinct state such as:

- `hot_worker_running`;
- `universe_worker_running`;
- HOT next due;
- UNIVERSE sweep / catalogue state;
- derived in-memory price-engine work;
- provider-level in-flight state.

## 7. UNIVERSE must be a real catalogue sweep

UNIVERSE should have a clear sweep lifecycle:

```text
START SWEEP
  ↓
discover current venue event universe
  ↓
construct/update canonical fixture set
  ↓
process next unevaluated fixture
  ↓
recognize approved families via the Approved Match Register
  ↓
persist exact native IDs / outcome mapping / invalidation
  ↓
capture compact Kalshi fee metadata where present (not quotes)
  ↓
stream catalogue progress
  ↓
next fixture
  ↓
...
  ↓
all eligible catalogue work evaluated/skipped with explicit reason
  ↓
COMPLETE SWEEP
```

A sweep may use batching internally for efficiency, but batching must not change the semantic contract: successful catalogue work is durable and the sweep progresses monotonically toward completion.

UNIVERSE must not require a fresh executable book or solver result to mark a fixture’s catalogue job complete.

Issue #330 remains in force: UNIVERSE chunks are bounded, the generation is not, stale chunk callbacks are epoch-quarantined, and the chunk wall must not yield to `next_hot_due` leftover-until-HOT time-slicing.

## 8. UNIVERSE and the price engine must stream useful results incrementally

UNIVERSE must not wait until the entire sweep has completed before publishing useful catalogue work.

As soon as a fixture is successfully catalogued, its result should be eligible to update shared current state and to derive price-engine work.

Examples of incremental UNIVERSE outputs:

- canonical fixture discovered;
- exact native IDs catalogued;
- Approved Match Register key stored;
- catalogue row invalidated, disappeared, or marked terminal;
- Kalshi fee-metadata snapshot captured or superseded;
- provider/market-specific catalogue error recorded.

The price engine likewise must not wait until a whole HOT or BACKGROUND slice has completed before publishing a finished item.

Examples of incremental price-engine outputs:

- near opportunity found;
- positive edge found;
- qualifying opportunity found;
- BACKGROUND row promoted to HOT;
- paper decision published;
- OPEN or explicit `PAPER_FILL_REJECTED` recorded when auto-capture runs.

If fixture 84 of 531 contains a meaningful catalogue result or a meaningful priced decision, the product should react at fixture 84. It must not wait for fixture 531.

## 9. UNIVERSE progress is durable; pricing work is derived

An active UNIVERSE sweep must have durable progress sufficient to avoid starting from zero after:

- HOT refreshes;
- provider backoff;
- application restart;
- bounded provider failures;
- transient network errors.

The durable catalogue is the source of truth for approved-market identities. Useful persisted catalogue/sweep state may include:

- sweep/generation ID;
- sweep start time;
- discovered event snapshot/version used by the live generation;
- canonical fixture cursor;
- completed canonical fixture IDs;
- failed/skipped fixture IDs with reasons;
- catalogue row identity, native IDs, lifecycle, and invalidation;
- current provider/series position where applicable;
- last successful fixture;
- incremental counters.

Issue #334 remains in force: the compact off-loop UNIVERSE checkpoint is resume telemetry only. Do **not** put catalogue rows, books, current-state, or price-engine work-queue state into that checkpoint.

Pricing work is derived in memory from ACTIVE catalogue rows by default:

- no durable QUEUED / IN_FLIGHT / RETRY_WAIT pricing table as scheduler authority;
- `next_retry_at` is not durable;
- after restart, rebuild the working set from ACTIVE rows;
- bounded item backoff may reset on restart.

Implementation detail may evolve, but the invariant is fixed:

> A successfully completed unit of UNIVERSE catalogue work must not be lost merely because HOT ran or another provider request failed later.

## 10. Broad discovery should not be repeated unnecessarily

Broad event discovery can be expensive.

Within a single UNIVERSE sweep, a successfully acquired event universe should be reusable while the sweep processes fixtures.

Continuation should not repeatedly call broad `list_events` merely because:

- HOT ran;
- a fixture catalogue pass took too long;
- a later provider request failed;
- the scheduler woke again.

Refresh broad discovery when justified by the sweep design, expiry rules or explicit provider evidence, not as an accidental consequence of lane scheduling.

Once exact IDs are catalogued, the price engine should not rediscover the event’s full market inventory as a substitute for targeted refresh.

## 11. Provider failure must be isolated and truthful

A provider timeout does not prove that the venue itself is globally unavailable.

This distinction is especially important when HOT targeted calls continue to succeed, and when one slow call must not leftover-mark unrelated work.

Provider timeout isolation applies to **both** price-engine tiers and to UNIVERSE catalogue items:

- one slow call fails or defers **one item**;
- unrelated items remain eligible;
- a held provider lease occupies that call’s slot until the HTTP ends, not the rest of the roster;
- provider capacity saturation is not a venue outage and must not be reported as `scan_budget_exhausted`.

If all provider slots for a venue are genuinely occupied, remaining items wait as `provider_capacity_saturated` / `deferred`. Unstarted items remain due/queued (honest `not_started_this_cadence`), not leftover-exhausted.

Diagnostics must distinguish at least:

- provider unavailable;
- authentication failure;
- rate limited/cooldown;
- broad discovery timeout;
- individual market timeout;
- individual order-book timeout;
- scheduler cancellation;
- request deferred due to provider priority;
- provider capacity saturation;
- cached/known-event HOT path healthy;
- partial catalogue sweep with successful provider work retained.

The UI must not simply display `MATCHBOOK FAILED` when the real condition was:

> `UNIVERSE broad discovery timed out while HOT targeted Matchbook refresh remains healthy`.

Lane-specific health and operation-specific failure attribution are required.

Shared collector `remaining_soft` must not leftover-mark unrelated fixtures or price-engine items as `scan_budget_exhausted`. Item timeout is not roster timeout.

## 12. HOT uses targeted refresh where possible

Once canonical fixture identity and exact native IDs are catalogued, HOT-priority and BACKGROUND-priority pricing should prefer targeted retrieval.

They should not unnecessarily repeat the same broad discovery operation needed by UNIVERSE.

The intended distinction is:

```text
UNIVERSE:
broad discovery → register key → persist exact native IDs → invalidate as needed

PRICE ENGINE:
known catalogue row → known venue market/contract IDs → refresh relevant books
  HOT priority: frequent cadence
  BACKGROUND priority: remaining ACTIVE rows
```

This reduces latency and provider load while preserving one shared canonical truth.

The price engine must not invent IDs. If a targeted refresh says a market is gone or identity-changed, request catalogue revalidation. Do not silently list the whole event as a shortcut.

## 13. One shared canonical state

Concurrency must not create two product truths.

HOT, BACKGROUND, and UNIVERSE must write to the same canonical current-state model.

They must share:

- canonical fixture identity;
- source aliases;
- the durable approved-market catalogue;
- Approved Match Register as the sole runtime market-equivalence authority;
- market normalization;
- operator-approved venue recognition/mapping rules;
- fee/FX model;
- solver semantics;
- opportunity state.

There must not be:

- a HOT-only market catalogue/equivalence path;
- a UNIVERSE-only market catalogue/equivalence path;
- a BACKGROUND-only matcher;
- duplicate canonical fixture stores;
- separate exception-rule systems;
- a second durable pricing-work authority that can disagree with the catalogue.

Concurrent updates must be idempotent and keyed by canonical identity.

Admission policy (`PAPER_ASSUMED_EQUIVALENT` vs `APPROVED_EQUIVALENT`, settlement assumption, live-execution eligibility) is derived at use time from the live register and `SPORTS_HEDGE_MODE=paper`. It must not be copied onto the catalogue row as a second drifting policy.

## 14. Canonical deduplication is mandatory

One real football fixture must correspond to one active canonical scheduling identity.

Source aliases such as:

- `Real Betis Balompié v Getafe CF`;
- `Real Betis v Getafe`;

may remain as provenance and lookup aliases, but must not create multiple HOT scheduling units.

Concurrent HOT, BACKGROUND, and UNIVERSE activity increases the importance of this invariant.

All scheduling/upsert paths must prevent alias duplication.

## 15. BACKGROUND → HOT promotion is immediate

The price engine must be able to promote a BACKGROUND row into HOT while UNIVERSE continues cataloguing and while other price-engine items continue.

Promotion is an event/state update, not a reason to end UNIVERSE.

Example:

```text
BACKGROUND evaluates an ACTIVE row outside the HOT lifecycle horizon
  ↓
cross-venue books complete
  ↓
edge +0.40% / near / qualifying
  ↓
HOT promotion criterion met
  ↓
canonical fixture added/upserted into HOT priority
  ↓
UNIVERSE continues its catalogue sweep
  ↓
other price-engine items remain eligible
```

HOT may then refresh that row on its next cadence while UNIVERSE continues elsewhere.

UNIVERSE catalogue completion of a new ACTIVE row may create derived price-engine work immediately. That is catalogue streaming, not UNIVERSE performing economics.

## 16. Surveillance promotion and trade eligibility are separate

HOT promotion should be broader than actual paper/live trade eligibility.

A fixture may deserve rapid monitoring because it has:

- positive edge below the trade threshold;
- near-trigger edge;
- significant divergence;
- rapid movement;
- newly catalogued approved-equivalent cross-venue market.

Paper entry remains subject to stricter gates such as:

- approved-catalogue market equivalence with exact required parameters/outcome semantics;
- no hard contradiction;
- fresh executable quotes;
- exact applicable costs/FX, including fail-closed Kalshi fee metadata;
- solver qualification;
- liquidity/depth;
- risk policy;
- treasury availability;
- configured minimum trade edge;
- idempotent capture;
- durable OPEN or explicit `PAPER_FILL_REJECTED` (merged #336 / #338 contract).

Therefore:

> **HOT membership means watch closely, not execute.**

Phase 1 remains paper-only and read-only toward venues. Concurrent scanning, catalogue maintenance, and price-engine evaluation never place, cancel, or sign real orders.

## 17. HOT may have priority without starving UNIVERSE or BACKGROUND

Provider scheduling should bias freshness-sensitive HOT work when necessary.

But priority must not become starvation.

The system should guarantee:

- eventual UNIVERSE catalogue progress under ordinary healthy-provider conditions;
- eventual BACKGROUND pricing of remaining ACTIVE rows;
- existing anti-starvation grants remain unless a later reviewed change proves otherwise.

Useful safeguards may include:

- bounded HOT provider concurrency;
- fair provider queueing after HOT priority;
- metrics for UNIVERSE provider wait time;
- metrics for BACKGROUND not-started / retry-wait counts;
- alarms if catalogue sweep progress stalls despite healthy providers;
- alarms if ACTIVE rows remain unpriced despite healthy providers.

A continuously busy HOT roster must not prevent the broad universe from ever being catalogued, and must not prevent BACKGROUND rows from ever being priced.

## 18. Sweep completion and price-engine coverage semantics

UNIVERSE is complete only when every eligible catalogue item in the sweep is one of:

- successfully catalogued;
- explicitly unsupported;
- explicitly out of scope;
- explicitly terminal;
- explicitly failed after the configured isolated retry policy.

A coordinator timeout, HOT refresh, BACKGROUND slice, or temporary provider wait is not UNIVERSE sweep completion.

Price-engine coverage is a separate completeness property:

- every ACTIVE supported catalogue row is eventually refreshed and evaluated;
- evaluated means priced (complete required outcome set, or an explicit item-level failure/deferral), not “is an arb”;
- a BACKGROUND row that has never shown an edge still counts toward coverage.

The UI and audit trail must distinguish:

- running;
- waiting on provider;
- deferred / capacity saturated;
- degraded but progressing;
- retrying isolated failure;
- completed;
- aborted by explicit operator/system shutdown.

## 19. Restart and shutdown behaviour

Graceful shutdown should preserve UNIVERSE catalogue progress before exit.

On restart:

- an open valid sweep may resume from durable catalogue/checkpoint state;
- already-completed catalogue work should remain completed;
- the price-engine working set should rebuild from ACTIVE catalogue rows without waiting for a full UNIVERSE re-list;
- bounded in-memory item backoff may reset;
- HOT should independently bootstrap from current canonical / catalogue state;
- provider cooldown/backoff should be restored where appropriate.

Issue #334 compact checkpoint remains resume telemetry. Unsafe or corrupt checkpoint state may be invalidated explicitly, but silent restart-from-zero of the durable catalogue should not be the normal recovery path.

## 20. Observability requirements

The operator console should expose HOT, BACKGROUND, and UNIVERSE independently.

### HOT status

Useful fields include:

- running/idle;
- next due;
- unique canonical HOT count;
- current refresh duration;
- items evaluated;
- provider health;
- last successful completion;
- promoted vs lifecycle HOT counts.

### BACKGROUND / price-engine coverage

Useful fields include:

- ACTIVE catalogue rows due;
- evaluated / retry-wait / not-started-this-cadence counts;
- promotions to HOT;
- isolated item timeouts;
- capacity-saturated / deferred counts.

The product must make T-6d / outside-horizon coverage honest. Operator labels may still say Fast scan / HOT for the frequent tier.

### UNIVERSE status

Useful fields include:

- sweep ID;
- running/waiting/degraded/complete;
- sweep start time;
- elapsed time;
- discovered fixture total;
- catalogued fixture count;
- remaining fixture count;
- matched event count;
- catalogued approved-family count;
- isolated failures;
- retries;
- progress percentage where meaningful.

The product should make it obvious that a five-minute active catalogue sweep is healthy if progress is increasing.

`scan_budget_exhausted` must not be the explanation for unstarted unrelated items. Capacity waits say `provider_capacity_saturated` / `deferred`.

## 21. No misleading venue-health interpretation

Venue health is contextual.

The product must not conflate:

- HOT targeted success;
- BACKGROUND item timeout;
- UNIVERSE broad-discovery failure;
- provider-wide outage;
- provider capacity saturation.

For example, simultaneous states such as these are valid and should be expressible:

```text
Matchbook:
  HOT targeted refresh: healthy
  UNIVERSE event discovery: timed out

Kalshi:
  HOT targeted refresh: healthy
  one BACKGROUND order_book: timed out after 8s
  remaining items: eligible
  UNIVERSE series discovery: partial/retrying
```

This is more truthful than one global red/green label.

## 22. Backpressure and overload

Concurrent workers require explicit overload handling.

If provider or local capacity is constrained:

1. protect freshness-sensitive HOT requests;
2. apply provider-aware backpressure to BACKGROUND pricing and UNIVERSE catalogue work;
3. preserve UNIVERSE cursor and completed catalogue work;
4. keep unrelated price-engine items eligible;
5. resume automatically;
6. do not fabricate provider failure from intentional local backpressure;
7. do not leftover-mark the roster as `scan_budget_exhausted`.

Local overload is not equivalent to venue unavailability.

Issue #330 remains in force when a UNIVERSE chunk hits its watchdog: the chunk ends, the epoch quarantines stale callbacks, the generation resumes, and the price engine keeps running.

## 23. Database and state-write safety

HOT, BACKGROUND, and UNIVERSE may update shared canonical state concurrently.

Writes must therefore be:

- canonical-ID keyed;
- idempotent where retried;
- transactionally safe where multiple records must move together;
- monotonic with respect to observation timestamps/freshness;
- protected against an older UNIVERSE snapshot overwriting newer HOT truth.

A later/fresher valid observation may supersede older state.

An older observation must not regress current price/status truth solely because its worker finishes later.

Catalogue identity writes and quote/economics writes are different facts. A newer HOT book must not be discarded because an older UNIVERSE catalogue confirm finished later, and a newer catalogue invalidation must not be ignored because a derived in-memory item is still in flight.

## 24. Current anti-patterns explicitly prohibited

The following architecture is prohibited by this tenet:

```text
one global scan lock
   ↓
UNIVERSE gets whatever seconds remain before HOT
   ↓
UNIVERSE broad discovery begins
   ↓
HOT deadline approaches
   ↓
UNIVERSE cancelled/returned
   ↓
HOT runs
   ↓
UNIVERSE starts another short chunk
```

Also prohibited:

- resetting UNIVERSE cursor because HOT completed;
- leftover-until-HOT time slicing;
- repeatedly rediscovering the same event universe within one sweep without cause;
- representing scheduler cancellation as provider outage;
- discarding healthy-provider results because another venue timed out;
- waiting for full UNIVERSE completion before persisting/promoting useful catalogue results;
- waiting for a whole price-engine slice before publishing a completed item;
- creating duplicate HOT entries from aliases;
- using separate matching truth per lane;
- requiring executable books or solver output before a UNIVERSE catalogue item can complete;
- exclusive HOT-only pricing that leaves ACTIVE rows outside the HOT horizon unpriced;
- a durable pricing-work queue or `next_retry_at` checkpoint as scheduler authority;
- putting catalogue rows, books, or work-queue state into the compact UNIVERSE checkpoint;
- leftover-marking unrelated items as `scan_budget_exhausted` because one call timed out or shared `remaining_soft` hit zero;
- treating provider slot saturation as a venue outage;
- inventing Kalshi fees of zero, or treating fee metadata as quote data;
- weakening Phase 1 paper-only / read-only venue boundaries;
- replacing the Approved Match Register with a second runtime equivalence authority;
- silently vanishing a paper-eligible capture instead of OPEN or `PAPER_FILL_REJECTED`.

## 25. Preferred implementation direction

The preferred architecture is:

```text
                         Sports Hedge backend (paper / read-only)
                                      │
              ┌───────────────────────┴────────────────────────┐
              │                                                │
     UNIVERSE catalogue worker                      PRICE ENGINE
     persistent generation                          one engine, two priority tiers
     chunk watchdog / epoch #330                    HOT frequent / BACKGROUND slower
              │                                     no exclusive HOT-only membership
     broad discovery + fixture identity                       │
     Approved Match Register key                    derive items from ACTIVE rows
     exact native IDs + invalidation                (process memory; restart rebuilds)
     Kalshi fee snapshot (not quotes)                         │
     do NOT require executable books                per item: isolated timeout
              │                                                │
              └───────────────────────┬────────────────────────┘
                                      │
                    Shared provider managers
                    priority + rate limiting
                    existing slot caps / anti-starve
                                      │
                    Durable approved-market catalogue
                    Compact UNIVERSE checkpoint (#334, unchanged role)
                    Paper ledger / watchlist / audit
                    Shared canonical current state
                                      │
                    paper/execution gates
```

The precise classes and task model may evolve, but the behavioural contract above is non-negotiable.

## 26. Required test coverage

At minimum, automated tests should prove:

1. HOT can start while UNIVERSE is still running.
2. UNIVERSE remains active after HOT completes.
3. HOT does not reset the UNIVERSE cursor.
4. HOT does not clear already-catalogued UNIVERSE fixture IDs.
5. UNIVERSE eventually reaches the final catalogue item under healthy providers.
6. Multiple HOT cycles can occur during one UNIVERSE sweep.
7. Provider capacity can be shared safely.
8. HOT receives request priority when capacity is saturated.
9. UNIVERSE resumes after provider contention.
10. A HOT provider request does not classify UNIVERSE as completed.
11. A single provider/series timeout does not discard other successful venue results.
12. A catalogue result found mid-UNIVERSE is persisted immediately.
13. A BACKGROUND qualifying decision can be promoted immediately to HOT.
14. Promotion does not stop the UNIVERSE sweep.
15. Sub-trade-threshold promotion does not itself open a paper position.
16. Canonical aliases do not create duplicate HOT scheduling units.
17. Older UNIVERSE observations cannot overwrite fresher HOT state.
18. Restart/resume preserves completed UNIVERSE catalogue progress and rebuilds price-engine work from ACTIVE rows.
19. UI/read-model status can show HOT healthy while a specific UNIVERSE provider operation is degraded.
20. No global cycle lock serializes the two workers.
21. An outside-HOT-horizon ACTIVE row is BACKGROUND-priced.
22. One order-book timeout fails only that price-engine item; unrelated HOT and BACKGROUND items remain eligible.
23. Provider slot saturation is reported as deferred/capacity, not `scan_budget_exhausted` and not a venue outage.
24. UNIVERSE can complete a catalogue item without executable books.
25. No durable pricing-work queue is required for coverage after restart.

This tenet is the contract. Runtime tests land with later implementation phases; they must not be deleted or weakened to preserve leftover-until-HOT behaviour.

## 27. Live acceptance scenario

A representative owner-live acceptance should prove the following behaviour:

```text
UNIVERSE starts cataloguing 500+ fixtures.

HOT has one known fixture and refreshes every ~30s.

While UNIVERSE is still running:
- HOT completes several refreshes.
- UNIVERSE catalogue progress continues increasing.
- no UNIVERSE restart occurs.
- BACKGROUND-priority ACTIVE rows outside the HOT horizon remain eligible to be priced.

UNIVERSE catalogues a fixture that is not yet HOT.
- exact native IDs persist without requiring a fresh executable book.
- a derived price-engine item becomes eligible.

BACKGROUND prices that ACTIVE row.
- a positive but sub-trigger cross-venue edge publishes immediately.
- the row is promoted to HOT without waiting for kickoff/lifecycle.
- UNIVERSE proceeds to the next catalogue fixture.
- an unrelated item timeout does not stop that BACKGROUND evaluation.

HOT refreshes the promoted fixture.
- if the edge remains below trade threshold, no paper trade opens.
- if it crosses the threshold and all execution gates pass, exactly one paper
  entry may OPEN, or capture records explicit PAPER_FILL_REJECTED.

UNIVERSE continues to catalogue completion independently.
```

The acceptance evidence must include timestamps or diagnostics showing true wall-clock overlap between HOT and UNIVERSE, plus honest BACKGROUND coverage rather than HOT-membership-only evaluated counts.

## 28. Relationship to other core tenets

This tenet does not weaken any existing safety requirement.

In particular:

- **Tenet 2 — Paper Mode and Execution Boundaries:** concurrent scanning, catalogue maintenance, and price-engine evaluation remain read-only toward venues in Phase 1.
- **Tenet 3 — Canonical Market Equivalence:** concurrency does not permit incompatible markets to be compared as executable equivalents. The Approved Match Register remains the sole runtime market-equivalence authority.
- **Tenet 20 — Approved Market Catalogue and Exception Review:** both lanes and both price-engine tiers use one pre-approved market catalogue and the same bounded approved archetypes; REVIEW_REQUIRED and UNSUPPORTED markets do not enter the normal solver. Onboarding review may add register entries; it is not a runtime admission gate for registered rows.
- **Tenet 4 — Arbitrage Operations:** near opportunities and qualifying opportunities remain economically and risk aware.
- **Tenet 11 — UI and Data Honesty:** lane/provider/item state must be labelled truthfully. Radar is not executable. BACKGROUND coverage must be visible.
- **Tenet 15 — Effective Venue Economics and Fees:** promotion does not bypass net economics. Kalshi fee metadata is fail-closed and is not quote data.
- **Tenet 18 — Execution Atomicity and Fill Risk:** HOT surveillance, BACKGROUND pricing, or UNIVERSE cataloguing never bypasses execution/fill safety gates.

Related live contracts preserved by this tenet:

- Issue #330 chunk watchdog and epoch quarantine;
- Issue #334 compact off-loop UNIVERSE checkpoint;
- Issue #331 / Tenet 20 Approved Match Register authority;
- merged #336 / #338 OPEN-or-`PAPER_FILL_REJECTED` capture handoff.

Concurrency is an observation, catalogue, and pricing-coverage architecture, not a relaxation of execution safety.

## 29. Review checklist

Any PR changing HOT, UNIVERSE, the price engine, provider scheduling or current-state orchestration should answer:

- [ ] Can HOT and UNIVERSE actually overlap in wall-clock time?
- [ ] Is there any global lock that still serializes them?
- [ ] Can HOT completion reset, close or restart an active UNIVERSE sweep?
- [ ] Is UNIVERSE catalogue progress monotonic and durable?
- [ ] Can UNIVERSE finish the entire configured catalogue sweep without leftover-until-HOT?
- [ ] Does UNIVERSE require executable books or solver output to complete a catalogue item? It must not.
- [ ] Does one price engine eventually evaluate every ACTIVE supported catalogue row?
- [ ] Are rows outside the HOT lifecycle horizon BACKGROUND-priced rather than ignored?
- [ ] Can a qualifying BACKGROUND decision promote to HOT immediately without stopping UNIVERSE?
- [ ] Does HOT receive provider priority only when necessary rather than terminating UNIVERSE?
- [ ] Are existing provider slot caps and anti-starvation grants still respected unless explicitly reviewed?
- [ ] Are provider failures isolated to one item, with unrelated items still eligible?
- [ ] Is provider capacity saturation reported as deferred/capacity rather than venue outage or `scan_budget_exhausted`?
- [ ] Are scheduler deferrals distinguished from venue failures?
- [ ] Are useful UNIVERSE catalogue results persisted incrementally?
- [ ] Can a completed price-engine item publish immediately, without waiting for the rest of the slice?
- [ ] Is HOT canonical-deduplicated?
- [ ] Do both workers and both pricing tiers share the same approved market catalogue, register, recognition rules and canonical state?
- [ ] Is pricing work derived from the durable catalogue rather than a second durable work queue?
- [ ] Does the compact UNIVERSE checkpoint still exclude catalogue rows, books, and work-queue state?
- [ ] Can stale/older UNIVERSE data overwrite fresher HOT truth? It must not.
- [ ] Can the operator see separate HOT, BACKGROUND, and UNIVERSE progress/health?
- [ ] Does restart/resume preserve successful catalogue progress and rebuild pricing work from ACTIVE rows?
- [ ] Does the implementation preserve all existing paper/execution safety boundaries, including OPEN-or-`PAPER_FILL_REJECTED`?

## 30. Guiding questions

For every scanner architecture change, reviewers should be able to answer:

> **If UNIVERSE needs five minutes to catalogue everything, can HOT refresh every 30 seconds throughout those five minutes without causing UNIVERSE to restart or lose work?**

And:

> **If both workers want the same venue at the same moment, does shared provider coordination protect rate limits while allowing both workers to continue making progress?**

And:

> **If a BACKGROUND item outside the HOT horizon develops a qualifying edge, can the price engine publish and promote it immediately while UNIVERSE keeps cataloguing?**

And:

> **If one provider call times out, do unrelated ACTIVE catalogue rows remain eligible to be priced, instead of leftover-marked as scan-budget exhausted?**

If the answer to any of these is no, the implementation does not satisfy this core tenet.

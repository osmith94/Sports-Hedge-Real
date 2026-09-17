# Core Tenet 19 — Concurrent HOT and UNIVERSE Scanning

## Principle

Sports Hedge must treat **HOT** and **UNIVERSE** as two independently scheduled, concurrently runnable scanner workers.

They serve different product purposes and must not be serialized into one shared scan cycle:

- **HOT** is the rapid-surveillance worker for fixtures that already deserve close attention.
- **UNIVERSE** is the broad-discovery worker that continuously sweeps the configured football universe for new cross-venue mappings, price dislocations and emerging opportunities.

A HOT refresh becoming due must **never terminate, reset, restart, discard or artificially time-slice an active UNIVERSE sweep**.

A UNIVERSE sweep may take several minutes. That is acceptable and expected if it is making forward progress. HOT must continue to run during that time.

The architectural contract is:

> **UNIVERSE discovers. HOT watches. Execution decides.**

## Why this is a core tenet

Sports Hedge loses product value if broad discovery is repeatedly interrupted by fast surveillance.

The scanner must be capable of doing both jobs at once:

1. continuously searching the wider market for new opportunities; and
2. rapidly refreshing already-interesting fixtures.

These workloads are complementary, not mutually exclusive.

A design where UNIVERSE receives only the leftover seconds before the next HOT deadline creates predictable failure modes:

- broad `list_events` discovery begins but is cancelled before useful work completes;
- provider health appears failed even when targeted HOT calls work normally;
- the same early portion of the universe is revisited repeatedly;
- later fixtures may never be evaluated;
- successful work can be delayed until an entire batch completes;
- opportunities are missed because promotion cannot occur before interruption;
- scanner history becomes dominated by tiny partial cycles rather than completed sweeps.

Sports Hedge must therefore prefer **concurrent worker architecture with shared provider coordination**, not a single global scan lock with alternating HOT/UNIVERSE turns.

## 1. HOT worker

HOT exists for fast surveillance.

Typical HOT membership includes:

- in-play fixtures;
- fixtures close to kickoff;
- fixtures promoted by opportunity logic;
- fixtures with significant cross-venue divergence;
- fixtures near the configured trade trigger;
- fixtures with large or rapid price movement;
- manually pinned fixtures where supported.

HOT characteristics:

- independently scheduled;
- typically short cadence, for example around 30 seconds;
- small canonical fixture set;
- targeted source-event and market refresh;
- high request priority when provider capacity is constrained;
- does not perform unnecessary broad event discovery when source identities are already known;
- uses the same approved-market catalogue, venue recognition/equivalence rules and current-state store as UNIVERSE.

HOT is a surveillance lane. HOT membership is not itself permission to trade.

## 2. UNIVERSE worker

UNIVERSE exists for broad discovery.

UNIVERSE characteristics:

- independently scheduled;
- long-running full sweep;
- may take several minutes;
- broad venue event discovery;
- canonical fixture construction;
- cross-venue mapping;
- market normalization;
- approved-market recognition and strict equivalence assessment;
- economics and solver evaluation where eligible;
- immediate persistence of completed work;
- immediate opportunity promotion into HOT;
- durable sweep progress.

UNIVERSE should aim to reach the end of the eligible configured universe, not merely process whichever fixtures fit before the next HOT interval.

## 3. True concurrency is required

HOT and UNIVERSE must be able to be active during the same wall-clock interval.

For example:

```text
18:00:00  UNIVERSE sweep starts
18:00:30  HOT refresh starts while UNIVERSE is still running
18:00:33  HOT refresh completes
18:00:33  UNIVERSE continues from its current position
18:01:00  HOT refresh starts again
18:04:20  UNIVERSE reaches the final fixture and completes
```

The fact that HOT runs at 18:00:30 must not cause UNIVERSE to return to fixture 1, close its generation, discard its event set, or lose already-completed comparisons.

Concurrency may be implemented using asynchronous tasks in one backend process. Separate operating-system processes are not required unless future operational evidence justifies them.

## 4. Independent scheduling does not mean uncontrolled provider traffic

HOT and UNIVERSE are independent workers, but venue access must be coordinated.

Both workers should use a **shared provider-access layer** for Matchbook, Kalshi, Polymarket and any future venue.

The shared layer is responsible for:

- provider concurrency limits;
- rate-limit handling;
- cooldown and backoff state;
- HTTP/session reuse;
- identical-request coalescing or caching where safe;
- provider-specific retry rules;
- request attribution and diagnostics;
- priority arbitration when capacity is constrained.

HOT may receive higher request priority because its value depends on freshness.

However:

> HOT priority may delay an individual UNIVERSE provider request; it must not cancel or reset the UNIVERSE sweep.

If provider capacity allows simultaneous safe requests, HOT and UNIVERSE may both make provider calls concurrently.

## 5. No global scan-cycle lock

Sports Hedge must not use one global lock that prevents HOT and UNIVERSE from running concurrently.

Locks should be scoped to the resource that actually needs protection.

Appropriate lock boundaries may include:

- one provider connection or rate-limit bucket;
- one cache mutation;
- one canonical fixture upsert;
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
- UNIVERSE sweep state;
- provider-level in-flight state.

## 6. UNIVERSE must be a real full sweep

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
normalize markets
  ↓
compare eligible cross-venue markets
  ↓
persist current state
  ↓
evaluate HOT promotion
  ↓
emit progress
  ↓
next fixture
  ↓
...
  ↓
all eligible work evaluated/skipped with explicit reason
  ↓
COMPLETE SWEEP
```

A sweep may use batching internally for efficiency, but batching must not change the semantic contract: successful work is durable and the sweep progresses monotonically toward completion.

## 7. UNIVERSE must stream useful results incrementally

UNIVERSE must not wait until the entire sweep has completed before publishing useful work.

As soon as a fixture is successfully processed, its result should be eligible to update shared current state.

Examples of incremental outputs:

- canonical fixture discovered;
- cross-venue fixture match found;
- market equivalence found;
- approved-market equivalence or review state updated;
- near opportunity found;
- positive edge found;
- qualifying opportunity found;
- fixture promoted to HOT;
- provider/market-specific error recorded.

If fixture 84 of 531 contains a meaningful opportunity, the product should react at fixture 84. It must not wait for fixture 531.

## 8. UNIVERSE progress is durable

An active sweep must have durable progress sufficient to avoid starting from zero after:

- HOT refreshes;
- provider backoff;
- application restart;
- bounded provider failures;
- transient network errors.

Useful persisted state may include:

- sweep/generation ID;
- sweep start time;
- discovered event snapshot/version;
- canonical fixture cursor;
- completed canonical fixture IDs;
- failed/skipped fixture IDs with reasons;
- current provider/series position where applicable;
- last successful fixture;
- incremental counters;
- next retry information for isolated provider failures.

Implementation detail may evolve, but the invariant is fixed:

> A successfully completed unit of UNIVERSE work must not be lost merely because HOT ran or another provider request failed later.

## 9. Broad discovery should not be repeated unnecessarily

Broad event discovery can be expensive.

Within a single UNIVERSE sweep, a successfully acquired event universe should be reusable while the sweep processes fixtures.

Continuation should not repeatedly call broad `list_events` merely because:

- HOT ran;
- a fixture evaluation took too long;
- a later provider request failed;
- the scheduler woke again.

Refresh broad discovery when justified by the sweep design, expiry rules or explicit provider evidence, not as an accidental consequence of lane scheduling.

## 10. Provider failure must be isolated and truthful

A provider timeout in UNIVERSE does not prove that the venue itself is globally unavailable.

This distinction is especially important when HOT targeted calls continue to succeed.

Diagnostics must distinguish at least:

- provider unavailable;
- authentication failure;
- rate limited/cooldown;
- broad discovery timeout;
- individual market timeout;
- scheduler cancellation;
- request deferred due to provider priority;
- cached/known-event HOT path healthy;
- partial sweep with successful provider work retained.

The UI must not simply display `MATCHBOOK FAILED` when the real condition was:

> `UNIVERSE broad discovery timed out while HOT targeted Matchbook refresh remains healthy`.

Lane-specific health and operation-specific failure attribution are required.

## 11. HOT uses targeted refresh where possible

Once canonical fixture and venue source identities are known, HOT should prefer targeted retrieval.

It should not unnecessarily repeat the same broad discovery operation needed by UNIVERSE.

The intended distinction is:

```text
UNIVERSE:
broad discovery → mapping → identify candidates

HOT:
known canonical fixture → known venue event IDs → refresh relevant markets/books
```

This reduces latency and provider load while preserving one shared canonical truth.

## 12. One shared canonical state

Concurrency must not create two product truths.

HOT and UNIVERSE must write to the same canonical current-state model.

They must share:

- canonical fixture identity;
- source aliases;
- market normalization;
- market-equivalence rules;
- operator-approved venue recognition/mapping rules;
- fee/FX model;
- solver semantics;
- opportunity state.

There must not be:

- a HOT-only market catalogue/equivalence path;
- a UNIVERSE-only market catalogue/equivalence path;
- duplicate canonical fixture stores;
- separate exception-rule systems.

Concurrent updates must be idempotent and keyed by canonical identity.

## 13. Canonical deduplication is mandatory

One real football fixture must correspond to one active canonical scheduling identity.

Source aliases such as:

- `Real Betis Balompié v Getafe CF`;
- `Real Betis v Getafe`;

may remain as provenance and lookup aliases, but must not create multiple HOT scheduling units.

Concurrent HOT and UNIVERSE activity increases the importance of this invariant.

All scheduling/upsert paths must prevent alias duplication.

## 14. UNIVERSE → HOT promotion is immediate

UNIVERSE must be able to promote a fixture into HOT while the sweep continues.

Promotion is an event/state update, not a reason to end UNIVERSE.

Example:

```text
UNIVERSE evaluates fixture 112 / 531
  ↓
cross-venue market found
  ↓
edge +0.40%
  ↓
HOT promotion criterion met
  ↓
canonical fixture added/upserted into HOT
  ↓
UNIVERSE continues fixture 113
```

HOT may then refresh fixture 112 on its next cadence while UNIVERSE continues elsewhere.

## 15. Surveillance promotion and trade eligibility are separate

HOT promotion should be broader than actual paper/live trade eligibility.

A fixture may deserve rapid monitoring because it has:

- positive edge below the trade threshold;
- near-trigger edge;
- significant divergence;
- rapid movement;
- newly approved-equivalent cross-venue market.

Paper entry remains subject to stricter gates such as:

- approved-catalogue market equivalence with exact required parameters/outcome semantics;
- no hard contradiction;
- fresh executable quotes;
- exact applicable costs/FX;
- solver qualification;
- liquidity/depth;
- risk policy;
- treasury availability;
- configured minimum trade edge;
- idempotent capture.

Therefore:

> **HOT membership means watch closely, not execute.**

## 16. HOT may have priority without starving UNIVERSE

Provider scheduling should bias freshness-sensitive HOT work when necessary.

But priority must not become starvation.

The system should guarantee eventual UNIVERSE progress under ordinary healthy-provider conditions.

Useful safeguards may include:

- bounded HOT provider concurrency;
- fair provider queueing after HOT priority;
- metrics for UNIVERSE provider wait time;
- alarms if sweep progress stalls despite healthy providers.

A continuously busy HOT roster must not prevent the broad universe from ever being scanned.

## 17. Sweep completion semantics

UNIVERSE is complete only when every eligible item in the sweep is one of:

- successfully evaluated;
- explicitly unsupported;
- explicitly out of scope;
- explicitly terminal;
- explicitly failed after the configured isolated retry policy.

A coordinator timeout, HOT refresh, or temporary provider wait is not sweep completion.

The UI and audit trail must distinguish:

- running;
- waiting on provider;
- degraded but progressing;
- retrying isolated failure;
- completed;
- aborted by explicit operator/system shutdown.

## 18. Restart and shutdown behaviour

Graceful shutdown should preserve UNIVERSE progress before exit.

On restart:

- an open valid sweep may resume from durable state;
- already-completed work should remain completed;
- HOT should independently bootstrap from current canonical state;
- provider cooldown/backoff should be restored where appropriate.

Unsafe or corrupt checkpoint state may be invalidated explicitly, but silent restart-from-zero should not be the normal recovery path.

## 19. Observability requirements

The operator console should expose both workers independently.

### HOT status

Useful fields include:

- running/idle;
- next due;
- unique canonical HOT count;
- current refresh duration;
- fixtures evaluated;
- provider health;
- last successful completion;
- promoted vs lifecycle HOT counts.

### UNIVERSE status

Useful fields include:

- sweep ID;
- running/waiting/degraded/complete;
- sweep start time;
- elapsed time;
- discovered fixture total;
- evaluated fixture count;
- remaining fixture count;
- matched event count;
- equivalent market count;
- near/positive/qualifying opportunity counts;
- HOT promotion count;
- provider/series currently being processed;
- last successful fixture;
- isolated failures;
- retries;
- progress percentage where meaningful.

The product should make it obvious that a five-minute active sweep is healthy if progress is increasing.

## 20. No misleading venue-health interpretation

Venue health is contextual.

The product must not conflate:

- HOT targeted success;
- UNIVERSE broad-discovery failure;
- provider-wide outage.

For example, simultaneous states such as these are valid and should be expressible:

```text
Matchbook:
  HOT targeted refresh: healthy
  UNIVERSE event discovery: timed out

Kalshi:
  HOT targeted refresh: healthy
  UNIVERSE series discovery: partial/retrying
```

This is more truthful than one global red/green label.

## 21. Backpressure and overload

Concurrent workers require explicit overload handling.

If provider or local capacity is constrained:

1. protect freshness-sensitive HOT requests;
2. apply provider-aware backpressure to UNIVERSE;
3. preserve UNIVERSE cursor and completed work;
4. resume automatically;
5. do not fabricate provider failure from intentional local backpressure.

Local overload is not equivalent to venue unavailability.

## 22. Database and state-write safety

HOT and UNIVERSE may update shared canonical state concurrently.

Writes must therefore be:

- canonical-ID keyed;
- idempotent where retried;
- transactionally safe where multiple records must move together;
- monotonic with respect to observation timestamps/freshness;
- protected against an older UNIVERSE snapshot overwriting newer HOT truth.

A later/fresher valid observation may supersede older state.

An older observation must not regress current price/status truth solely because its worker finishes later.

## 23. Current anti-patterns explicitly prohibited

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
- repeatedly rediscovering the same event universe within one sweep without cause;
- representing scheduler cancellation as provider outage;
- discarding healthy-provider results because another venue timed out;
- waiting for full UNIVERSE completion before persisting/promoting useful results;
- creating duplicate HOT entries from aliases;
- using separate matching truth per lane.

## 24. Preferred implementation direction

The preferred architecture is:

```text
                    Sports Hedge backend
                           │
              ┌────────────┴────────────┐
              │                         │
       HOT async worker          UNIVERSE async worker
       short cadence             long-running sweep
       targeted refresh          broad discovery/eval
              │                         │
              └────────────┬────────────┘
                           │
                Shared provider managers
                priority + rate limiting
                           │
             ┌─────────────┼─────────────┐
             │             │             │
         Matchbook       Kalshi      Polymarket
                           │
                           v
                Shared canonical state
                           │
             approved-catalogue recognition → economics → solver
                           │
                  paper/execution gates
```

The precise classes and task model may evolve, but the behavioural contract above is non-negotiable.

## 25. Required test coverage

At minimum, automated tests should prove:

1. HOT can start while UNIVERSE is still running.
2. UNIVERSE remains active after HOT completes.
3. HOT does not reset the UNIVERSE cursor.
4. HOT does not clear already-evaluated UNIVERSE fixture IDs.
5. UNIVERSE eventually reaches the final fixture under healthy providers.
6. Multiple HOT cycles can occur during one UNIVERSE sweep.
7. Provider capacity can be shared safely.
8. HOT receives request priority when capacity is saturated.
9. UNIVERSE resumes after provider contention.
10. A HOT provider request does not classify UNIVERSE as completed.
11. A single provider/series timeout does not discard other successful venue results.
12. An opportunity found mid-UNIVERSE is persisted immediately.
13. An opportunity found mid-UNIVERSE can be promoted immediately to HOT.
14. Promotion does not stop the UNIVERSE sweep.
15. Sub-trade-threshold promotion does not itself open a paper position.
16. Canonical aliases do not create duplicate HOT scheduling units.
17. Older UNIVERSE observations cannot overwrite fresher HOT state.
18. Restart/resume preserves completed UNIVERSE progress.
19. UI/read-model status can show HOT healthy while a specific UNIVERSE provider operation is degraded.
20. No global cycle lock serializes the two workers.

## 26. Live acceptance scenario

A representative owner-live acceptance should prove the following behaviour:

```text
UNIVERSE starts with 500+ fixtures.

HOT has one known fixture and refreshes every ~30s.

While UNIVERSE is still running:
- HOT completes several refreshes.
- UNIVERSE progress continues increasing.
- no UNIVERSE restart occurs.

UNIVERSE reaches a fixture with a positive but sub-trigger cross-venue edge.
- fixture is immediately promoted to HOT.
- UNIVERSE proceeds to the next fixture.

HOT refreshes the promoted fixture.
- if the edge remains below trade threshold, no paper trade opens.
- if it crosses the threshold and all execution gates pass, exactly one paper entry may open.

UNIVERSE continues to completion independently.
```

The acceptance evidence must include timestamps or diagnostics showing true wall-clock overlap between HOT and UNIVERSE.

## 27. Relationship to other core tenets

This tenet does not weaken any existing safety requirement.

In particular:

- **Tenet 2 — Paper Mode and Execution Boundaries:** concurrent scanning remains read-only toward venues in Phase 1.
- **Tenet 3 — Canonical Market Equivalence:** concurrency does not permit incompatible markets to be compared as executable equivalents.
- **Tenet 20 — Approved Market Catalogue and Exception Review:** both lanes use one pre-approved market catalogue; ambiguous probable-archetype markets go to review and unsupported novelty markets stay out of the solver.\n- **Tenet 20 — Approved Market Catalogue and Exception Review:** both lanes use the same bounded approved archetypes; REVIEW_REQUIRED and UNSUPPORTED markets do not enter the normal solver.
- **Tenet 4 — Arbitrage Operations:** near opportunities and qualifying opportunities remain economically and risk aware.
- **Tenet 11 — UI and Data Honesty:** lane/provider state must be labelled truthfully.
- **Tenet 15 — Effective Venue Economics and Fees:** promotion does not bypass net economics.
- **Tenet 18 — Execution Atomicity and Fill Risk:** HOT surveillance or UNIVERSE discovery never bypasses execution/fill safety gates.

Concurrency is an observation/discovery architecture, not a relaxation of execution safety.

## 28. Review checklist

Any PR changing HOT, UNIVERSE, provider scheduling or current-state orchestration should answer:

- [ ] Can HOT and UNIVERSE actually overlap in wall-clock time?
- [ ] Is there any global lock that still serializes them?
- [ ] Can HOT completion reset, close or restart an active UNIVERSE sweep?
- [ ] Is UNIVERSE progress monotonic and durable?
- [ ] Can UNIVERSE finish the entire configured sweep?
- [ ] Does HOT receive provider priority only when necessary rather than terminating UNIVERSE?
- [ ] Are provider concurrency/rate limits still respected?
- [ ] Are provider failures isolated to the smallest practical unit?
- [ ] Are scheduler deferrals distinguished from venue failures?
- [ ] Are useful UNIVERSE results persisted incrementally?
- [ ] Can UNIVERSE promote a fixture to HOT immediately?
- [ ] Does promotion leave UNIVERSE running?
- [ ] Is HOT canonical-deduplicated?
- [ ] Do both workers share the same approved market catalogue, recognition rules, matcher and canonical state?
- [ ] Can stale/older UNIVERSE data overwrite fresher HOT truth? It must not.
- [ ] Can the operator see separate HOT and UNIVERSE progress/health?
- [ ] Does restart/resume preserve successful sweep progress?
- [ ] Does the implementation preserve all existing paper/execution safety boundaries?

## 29. Guiding questions

For every scanner architecture change, reviewers should be able to answer:

> **If UNIVERSE needs five minutes to scan everything, can HOT refresh every 30 seconds throughout those five minutes without causing UNIVERSE to restart or lose work?**

And:

> **If both workers want the same venue at the same moment, does shared provider coordination protect rate limits while allowing both workers to continue making progress?**

And:

> **If UNIVERSE discovers something important halfway through the sweep, can HOT start watching it immediately while UNIVERSE keeps going?**

If the answer to any of these is no, the implementation does not satisfy this core tenet.

# Wave G scanner validation — synthetic stress and manual diagnostics

**Date:** 15 September 2026
**Branch base:** `f0553d35e06dd49434b771af378ec7b49aaf80c1`
**Scope:** deterministic validation of bounded collector concurrency and manual diagnostics.

## Corrected owner observation

Owner-Windows evidence on the failed candidate recorded Fast Scan at about
**58s**, Full Sweep at about **148/150s** with 53 fixtures, and the separate
manual `POST /paper/collect` reaching the 60s browser timeout. The scheduled
figures establish a throughput problem; the browser timeout is still a separate
manual-diagnostic envelope.

## Why the manual request differs

| Path | Work shape | Bound |
| --- | --- | --- |
| Fast Scan / HOT | Known current identities; skips venue event discovery; server-owned cadence | 25s collector + 5s coordinator grace |
| Full Sweep / UNIVERSE | Venue-union discovery; resumable chunks yield to HOT | 150s generation budget split across chunks |
| Manual Fast refresh | Same known HOT identity/venue plan as Fast Scan; does not advance scheduler state | 25s collector + 5s coordinator grace |
| Full diagnostic | One venue-union discovery and bounded-concurrent market/depth sweep, up to 60 fixtures; does not advance scheduler state | 20s collector + 5s coordinator grace |

Before this change, manual collect used the general 45s collector budget plus
5s coordinator grace, returned the entire nested `fixture_markets` response,
and could spend nearly the whole envelope in serial provider market/depth
calls. Event-loop scheduling and response serialization then had little margin
inside the frontend's fixed 60s abort. Scheduled lanes can remain healthy
because HOT skips discovery and UNIVERSE yields/resumes; the manual request does
neither.

The primary UI action is now **Run scan** and calls
`POST /paper/collect/hot`; broad discovery is explicitly Advanced **Run full
diagnostic**. Both manual endpoints tag their collection kind and omit the
redundant nested `fixture_markets` body. The full diagnostic returns a truthful
partial report at its bound. Current fixture drill-down remains available from
the coordinator-owned current-state inventory. The frontend diagnostic timeout
stays at 60s.

## Deterministic benchmark assumptions

`backend/tests/test_scanner_synthetic_stress.py` uses only synthetic providers:

- 2ms controlled latency per provider call;
- one Matchbook and one Polymarket event per canonical fixture;
- one settlement-equivalent BTTS market per venue;
- two Polymarket depth calls per fixture;
- exact event identity, current source timestamps, deterministic fee/FX inputs;
- no Kalshi call;
- no credential, internet, real-provider SLA, retry, rate-limit, or production
  network claim;
- 2s scaled test budget; production HOT/UNIVERSE budgets are not altered.

Run:

```bash
cd backend
python -m pytest -q -s tests/test_scanner_synthetic_stress.py
```

Credentialed real-provider benchmarking remains issue #170 and is deliberately
separate.

## Results

Measured on the Cloud Agent Linux runner. These numbers validate deterministic
architecture behavior under the assumptions above; they are not provider SLAs.

| Fixtures | HOT wall | UNIVERSE wall | Cancels | Orphans/live tasks |
| ---: | ---: | ---: | ---: | ---: |
| 1 | 6.3ms | 8.7ms | 0 | 0 |
| 4 | 10.6ms | 12.8ms | 0 | 0 |
| 16 | 31.6ms | 32.7ms | 0 | 0 |
| 50 | 92.4ms | 92.2ms | 0 | 0 |

50-fixture attribution:

| Lane | Provider attribution | Stage attribution |
| --- | --- | --- |
| HOT | Matchbook 50 calls / 147ms; Polymarket 150 calls / 438ms | market discovery 100 calls / 299ms; book depth 100 / 286ms; mapping 12ms; fees/FX 50 calls; solver 50 calls / 1ms |
| UNIVERSE | Matchbook 51 calls / 149ms; Polymarket 151 calls / 430ms | event lookup 2 calls / 4ms; market discovery 100 / 295ms; book depth 100 / 280ms; mapping 10ms; fees/FX 50 calls; solver 50 calls |

Repeated 16-fixture soak produced 31.2–70.8ms HOT wall time (31.8ms median)
and 33.1–34.0ms UNIVERSE wall time (33.7ms median) across 12 cycles per lane,
with task delta 0, cancellations 0, and orphans 0. The second-half latency guard
prevents progressive slowdown from passing silently.

The controlled 50-fixture run is below 1s and shows no task accumulation.
The higher-latency benchmark in `WAVE_G_SCANNER_PERFORMANCE.md` uses 40ms calls
and demonstrates an 8.38× relative speedup at 50 fixtures versus the explicit
serial provider bound. Cluster fan-out is capped at 8; Matchbook, Polymarket and
Kalshi calls are separately capped at 4/8/4. These synthetic results justify
the topology correction but do not assert a real-provider SLA.

## 53 fixtures / 0 cross-venue / 0 equivalent

The compact UI previously mixed generations: `fixtures` and `equivalent` came
from the merged current inventory, while `cross-venue` came from
`last_matched_event_pairs` on whichever lane completed last. A HOT report could
therefore show `53 fixtures / 0 cross-venue` even though the 53 rows belonged
to the merged Fast+Full radar. That was a read-model attribution bug, not proof
of either provider coverage or a matcher regression.

Cross-venue count is now derived from each current fixture's venue-presence
flags. Collector diagnostics also report:

- single-venue clusters;
- multi-venue identity clusters and matched event pairs;
- current inventory fixtures with at least two venues;
- settlement-equivalent market count;
- qualifying arbitrage count.

This distinguishes:

1. fixtures exist but providers currently share no canonical fixture;
2. providers share canonical fixtures but no settlement-equivalent market;
3. equivalent markets exist but no executable/net-positive opportunity.

Zero remains a valid current result. No fallback opportunity is created and no
canonical, settlement, freshness, depth, fee, FX, risk, or allocator gate is
relaxed. Actual owner-provider coverage still requires the credentialed #170
run; synthetic data cannot prove current live coverage.

## Mapping Verify critical-path check

Scanning creates deterministic local mapping evidence only.
`MappingReviewService.build_prompt()` is invoked solely by
`POST /paper/mapping-reviews/prompt`; neither the collector nor
`PaperScanService` invokes it. The prompt builder contains no HTTP/OpenAI
client, and there is no OpenAI SDK dependency. Tests enforce that prompt
generation remains off the executable quote path. An operator must copy/paste
any ChatGPT response and explicitly confirm a narrow rule; ChatGPT text alone
activates nothing.

## Safety and tenet review

Applicable tenets: 02, 03, 04, 08, 09, 11, 12, 14, 15, 18.

- Paper-only/read-only venue boundary: satisfied; no place/cancel/sign method.
- Canonical and settlement equivalence: satisfied; fail-closed rules unchanged.
- Executable freshness/depth/economics and allocator/treasury: satisfied;
  scanner gates unchanged.
- Current-state truth: satisfied; mixed-generation count corrected and zero
  states explained without fabrication.
- Audit append-only semantics and SQLite retention: satisfied; no deletion,
  reset, migration, or destructive write added.
- Data class: owner observations are live-paper operator evidence; benchmark
  values are fixture/demo synthetic test measurements; no historical or fixture
  result is presented as live.
- Partial/deferred: credentialed provider benchmark #170 and owner-Windows
  smoke remain external acceptance steps.
- Known conflict: none.

Stop for independent architect review. Do not merge from this lane.

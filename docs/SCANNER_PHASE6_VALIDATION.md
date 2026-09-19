# Scanner Phase 6 — PAPER soak and fallback validation

Issue **#350**. Validation/rollout only. This is **not** a scanner redesign and
**not** a demo-ready declaration merely because CI is green.

Phase 1 remains:

```text
SPORTS_HEDGE_MODE=paper
SPORTS_HEDGE_EXECUTION_ENABLED=false
```

The soak harness is a **read-only observer**. It never places, cancels, or signs
venue orders. It never POSTs `/paper/collect` and must not start discovery or
pricing work.

Data honesty:

- Automated tests in this PR use **fixture/demo** snapshots only.
- A real **10–15 minute owner-live PAPER soak** happens only after architect
  acceptance and merge. Do not treat CI JSON examples as live soak evidence.

## Commands (Windows)

Start the paper demo if it is not already running:

```powershell
.\scripts\windows\Start-SportsHedge-Demo.ps1
```

Confirm `/health` shows `mode=paper`, `execution_enabled=false`, and a serving
Git SHA. Then run a bounded observer soak (default **12 minutes** / 720s):

```powershell
.\scripts\windows\Run-PaperScannerSoak.ps1
```

Longer soak without code changes:

```powershell
.\scripts\windows\Run-PaperScannerSoak.ps1 -DurationSeconds 900
.\scripts\windows\Run-PaperScannerSoak.ps1 -DurationSeconds 1800 -IntervalSeconds 15
```

Equivalent from the repo root after `backend\.venv` is installed:

```powershell
.\backend\.venv\Scripts\python -m sports_hedge.application.scanner_phase6 --duration-seconds 720
```

Double-click `scripts\windows\Run-PaperScannerSoak.bat` for the same default.

## Where the report is written

Default path:

```text
logs\scanner-phase6-soak.<UTC timestamp>.json
```

Override with `-Output path.json`. The console also prints `accepted`,
`coverage_ratio`, ACTIVE/evaluated counts, and any hard-fails.

## What success / failure looks like

The report always exposes:

- start/end timestamps and observed build SHA;
- ACTIVE catalogue row count;
- coverage ratio = current ACTIVE `(catalogue_row_id, content_version)`
  identities evaluated at least once / current ACTIVE identities;
  a new content version must earn its own evaluation;
- every current ACTIVE identity not evaluated, with a truthful reason/status
  (`retry_wait`, `deferred` / `provider_capacity_saturated`,
  `not_started_this_cadence`, `revalidation_needed`, `in_flight`, `failed`);
- HOT and BACKGROUND evaluated / retry / deferred / not-started counts;
- revalidation-needed count and reasons;
- provider health by venue + operation + tier;
- `scan_budget_exhausted` occurrences on the price-engine path (**must be zero**);
- HOT cycles while UNIVERSE was active/overlapping;
- BACKGROUND→HOT promotions observed;
- item-completion paper decisions (OPEN / `PAPER_FILL_REJECTED` / duplicates);
- `/health`, `/build-info`, `/paper/live-refresh` (and
  `/paper/scanner-validation`) availability + timing samples;
- explicit evidence that the harness invoked GET only and venue write methods
  are absent.

Do **not** invent success from a single percentage. Strong coverage after
several HOT cycles and multiple BACKGROUND cadences is the owner-live target,
but degraded-provider truth must remain visible.

Hard-fail (exit code 2) when any of these are present:

- no usable observer samples, or core GET evidence missing/inconsistent
  (`/health`, `/build-info`, `/paper/live-refresh`,
  `/paper/scanner-validation`);
- owner-live: **any** sampled core GET that is unavailable/non-2xx/transport
  error (a later success does not clear it);
- `/paper/scanner-validation` unavailable (never treated as a valid empty
  catalogue);
- stated ACTIVE catalogue count disagrees with the row payload;
- owner-live serving build SHA missing, inconsistent, or changed mid-soak;
- any owner-live sample with evidence-integrity errors
  (`build_sha_inconsistent`, `catalogue_count_mismatch`,
  `scanner_validation_unavailable`, `capture_evidence_unavailable`, or
  equivalent observer-payload corruption) — later healthy samples do not
  average it away;
- no ACTIVE catalogue row observed for the whole bounded soak;
- coordinator catalogue/engine wiring unbound on the observer GET;
- silent ACTIVE identities with no evaluated result and no truthful
  retry/deferred/revalidation/not-started reason;
- any price-engine `scan_budget_exhausted`;
- HOT worked while UNIVERSE was due and made no observed progress;
- BACKGROUND ACTIVE rows existed, BACKGROUND was due, and BACKGROUND
  received no work;
- one provider timeout leftover-marking unrelated items;
- eligible + autofill ON without a **per-opportunity** durable OPEN
  (or valid progression) or explicit `PAPER_FILL_REJECTED`;
- duplicate OPEN / treasury lock for the same opportunity/attempt;
- venue write / place / cancel / sign;
- a status/read endpoint triggering discovery or pricing work.

`GET /paper/scanner-validation` is strictly observer-only: it must not bind
coordinator catalogue/engine state, change due times, or start
reconstruction/discovery/pricing. If the runtime is unbound, the snapshot
records that truth instead of repairing it.

Magic `notes` strings are not soak hard-fail switches. Dual-matcher and
reset-quarantine facts stay deterministic CI/architecture tests; a read-only
soak cannot cause them.

Fixture/demo example (CI; **not** owner-live):

```json
{
  "schema_version": 2,
  "issue": 350,
  "data_kind": "fixture_demo",
  "paper_only": true,
  "execution_enabled": false,
  "usable_sample_count": 2,
  "active_catalogue_row_count": 2,
  "rows_evaluated_at_least_once": 1,
  "coverage_ratio": 0.5,
  "evaluated_identities": ["amc-a:1"],
  "unevaluated_rows": [
    {
      "catalogue_row_id": "amc-b",
      "content_version": 1,
      "status": "not_started_this_cadence",
      "reason": "not_started_this_cadence",
      "evaluated_at_least_once": false
    }
  ],
  "hot_universe_overlap_samples": 2,
  "scan_budget_exhausted_price_engine": 0,
  "durable_queue": false,
  "accepted": true
}
```

This example is **fixture/demo** only. CI proves the deterministic contracts
above with fixture/demo data. Owner-live soak evidence is a separate gate.

## Safe restart

Use the existing Windows scripts. Do not invent a new restart protocol.

```powershell
.\scripts\windows\Stop-SportsHedge-Demo.ps1
.\scripts\windows\Refresh-SportsHedge-Demo.ps1
```

Or stop then start:

```powershell
.\scripts\windows\Stop-SportsHedge-Demo.ps1
.\scripts\windows\Start-SportsHedge-Demo.ps1
```

After restart, price-engine work reconstructs from ACTIVE catalogue rows. There
is no durable price queue. Compact UNIVERSE checkpoint v2 is unchanged.

Empty catalogue (first boot / explicit empty DB) is the fallback path:

```text
empty approved_market_catalogue
  → UNIVERSE discovery + Approved Match Register lookup
  → catalogue rows + Kalshi fee snapshots
  → price engine reconstructs ACTIVE derived items
  → HOT/BACKGROUND pricing from exact IDs
```

No second matcher. No full-event fallback from the price engine once exact IDs
are catalogued.

## Known limitations

None discovered by a real owner-live soak in this PR. CI does not fabricate
that soak. If the later owner-live run finds a narrow defect, fix it in a
follow-up Phase 6 PR rather than expanding architecture.

Frozen non-goals still hold: do not raise the 8s provider timeout or 4/4 caps
as a fix; do not add a durable price/observability queue; do not change
Approved Match Register semantics; do not implement live execution.

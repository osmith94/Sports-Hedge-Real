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
- coverage ratio = rows evaluated at least once / ACTIVE rows observed;
- every ACTIVE row not evaluated, with a truthful reason/status
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

- silent ACTIVE rows with no evaluated result and no truthful
  retry/deferred/revalidation/not-started reason;
- any price-engine `scan_budget_exhausted`;
- HOT preventing UNIVERSE progress;
- BACKGROUND starvation;
- one provider timeout leftover-marking unrelated items;
- eligible + autofill ON without OPEN or explicit `PAPER_FILL_REJECTED`;
- duplicate OPEN / treasury lock from repeated observation;
- venue write / place / cancel / sign;
- a status/read endpoint triggering discovery or pricing work;
- stale pre-reset projection resurrection;
- dual matcher / second equivalence authority.

CI proves the deterministic contracts above with fixture/demo data. Owner-live
soak evidence is a separate gate.

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

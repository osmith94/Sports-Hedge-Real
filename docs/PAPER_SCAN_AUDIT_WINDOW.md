# Paper scan history vs current scanner state

**Status:** Investigation evidence for Wave A / Item 10. Draft for architect review.  
**Frozen checkpoint:** `dfda32e0a3c71a6fadc3c38a61ad12cbfd986d7f`  
**Base:** `integration/foundations-1-5-2026-09-15`  
**Date:** 14 September 2026

This note answers why rows disappear from the operations-console **Paper scan history** table. It does **not** redesign Opportunity Monitor (#168), treasury, or audit storage. Items 8/9 (Age + loaded-set sorting) are presentation-only on the historical latest-N table.

Data class: **historical paper audit observations** (live paper when the API is reachable). Not radar current-state, not modelled, not demo fixtures.

## Recommendation

Visible disappearance after enough new scans is the **latest-N audit window** (`LIMIT 100`, newest `scanned_at` first). Preserve append-only `paper_scan_records`. Let later Items 8/9/11 change presentation only.

## Seven questions

### 1. Does the paper scan repository remain append-only?

**Yes.** Production write path is `SqlitePaperScanRepository.append_scan` → `INSERT INTO paper_scan_records`. Class docstring: scan rows are never deleted; unreadable legacy rows are skipped with diagnostics.

Production search at this SHA found:

| Path | Result |
| --- | --- |
| `DELETE FROM paper_scan_records` | none in `backend/src` |
| `UPDATE paper_scan_records` | none in `backend/src` (tests may mutate a sidecar SQLite file to inject malformed decimals) |
| `append_scan` `ON CONFLICT` | none |
| Schema | additive `ALTER TABLE ... ADD COLUMN` only |

Callers: `_persist_decision` in `backend/src/sports_hedge/api/paper.py` after a paper decision that has market history. Each `PaperScanRecord.record_id` is a new UUID.

### 2. Does `GET /paper/scans` return newest N rows (default 100)?

**Yes.** `recent_scans` defaults `limit=100` (`ge=1, le=1000`) and calls `repository.list_scans(...)`. SQL:

```text
SELECT * FROM paper_scan_records ... ORDER BY scanned_at DESC LIMIT ?
```

No grouping by canonical market. No “latest per opportunity”. Repeated Leeds/Newcastle refreshes are separate audit rows until they age out of the window.

`GET /paper/scans/summary` is a **different** query: all readable rows with `scanned_at >= since` (default local midnight), **no limit**. A row can leave the table and still count in today’s summary.

### 3. Does the frontend request the default 100?

**Yes.** `frontend/lib/api.ts` `getPaperScans(query = "limit=100")`. `frontend/app/page.tsx` calls `getPaperScans("limit=100")`.

### 4. Is any client-side dedupe, stale filter, replacement, or eviction applied?

**No.** The server page maps `scans` in API order. No `filter`, no identity merge, no TTL. A row vanishes from the UI only when a later fetch no longer includes it.

### 5. Is current canonical radar state stored independently?

**Yes.** `FixtureCurrentStateStore` (`backend/src/sports_hedge/application/fixture_current_state.py`) is **process-memory**, coordinator-owned, HOT/UNIVERSE upsert + radar TTL (`paper_hot_current_state_ttl_seconds` default 90, `paper_universe_current_state_ttl_seconds` default 360). Tracked / fixture drill-down read that store. **Paper scan history does not.**

### 6. When a row leaves the visible table, does the audit row remain queryable?

**Yes**, if disappearance is window rollover. Proof: `backend/tests/test_paper_scan_audit_window.py`

1. Insert a unique Leeds/Newcastle audit row.
2. Insert `N=5` newer rows.
3. `list_scans(limit=5)` / `GET /paper/scans?limit=5` omit the marker.
4. `list_scans(limit=6)`, `GET /paper/scans` (default 100), and raw `SELECT` still return it.

This investigation did not delete `./data/paper_audit.sqlite`.

### 7. Are there code paths that delete or replace historical audit observations?

**No production path.** Contract concern would be a future `DELETE`/`UPDATE` of `paper_scan_records` or `INSERT ... ON CONFLICT` rewrite. Tests may `UPDATE` a temp DB to plant malformed decimals; those rows stay in SQLite and surface as `PaperScanSummary.malformed_*`.

`LIMIT` is applied **before** decode. A malformed row in the newest N occupies a SQL slot, is omitted from the JSON list, and does not pull in the next readable older row. That is still not deletion.

Radar TTL omitting a fixture from `current_radar_rows` is a **separate** in-memory freshness rule. The matching audit row remains.

## What the existing UI is

Latest-N **audit history**, not current scanner truth. Before this lane the copy (“Newest matched-market decisions” / `SCANNER DATA`) could be read as current radar. This pass only relabels it as `Latest 100 audit observations` / `LATEST 100 AUDIT`.

Repeated same-market scans **do** stack in this table until the rolling window drops the older UUIDs. That is append-only history plus `LIMIT 100`, not current-state replacement.

## Items 8 / 9 (presentation on this historical table) and later Item 11

| Item | Status | Why this investigation matters |
| --- | --- | --- |
| **8** | Live **Age** from audit `scanned_at` (`0s..59s`, then `1m`…), tooltip = exact timestamp, one shared timer | Age uses the audit row’s `scanned_at`, not browser receipt time. This remains an audit-window table unless Item 11 lands. |
| **9** | Sortable headers on the loaded set (numeric/time DESC first; text ASC; nulls last) | Sort only the **loaded latest-N**, and say so. Do not imply server-wide order. |
| **11** | Opportunity Monitor IA (#168): **current radar** as the primary operational view; audit history secondary/collapsible | Do **not** implement by deleting or rewriting `paper_scan_records`. Drive the primary table from `FixtureCurrentStateStore` / watchlist radar. Keep this audit window labelled historical. HOT must not wipe valid UNIVERSE; expired radar rows leave the primary view by TTL, not by audit `LIMIT`. |

## Tenets

Applicable: 02 (paper-only), 04 (append-only operational history), 08 (do not silently overwrite observations), 11 (label latest-N audit vs current), 12 (stop for review).

No tenet was weakened. Phase 1 remains read-only toward venues.

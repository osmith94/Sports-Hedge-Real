# Core Tenet 08 — Historical Data & Provenance

## Principle

The normalized database is the source of truth for historical football and odds data. Excel is an export/review surface, not the primary store.

Every historical conclusion must be traceable back to source evidence and data-quality metadata.

## Initial bounded universe

Prioritize:

```text
Premier League 2025/26
Championship 2025/26
La Liga 2025/26
```

with Champions League schema/readiness added as coverage permits.

## Non-negotiables

Historical football data should retain, where available:

- canonical competition/season/team/match identity;
- final/half-time scores;
- corners;
- yellow/red cards;
- goals and event timestamps;
- penalties/substitutions;
- lineups/team sheets;
- source name/id/reference/URL;
- source timestamp and retrieval timestamp;
- raw payload/hash or equivalent provenance;
- confidence/quality flags.

Historical odds must distinguish data quality rather than invent precision:

```text
A = timestamped exchange odds + liquidity
B = timestamped bookmaker odds
C = opening/closing or sparse odds
D = match facts / no usable odds
```

Do not invent timestamps, liquidity or line histories that a source did not provide.

## Append-only evidence

Re-ingestion may update normalized current facts when justified, but raw/source evidence and historical observations must not be silently overwritten. Corrections and changed source records must remain auditable.

Multiple legitimate sources for the same event should be retainable rather than collapsed into one source row.

## Coverage

Every dataset/backfill should be able to report coverage by competition/season/field/market/source, including missing or unresolved records.

## Review checks

- Can every normalized fact/odds row be traced to a source?
- Are source time and retrieval time distinct where needed?
- Does re-ingestion preserve audit evidence?
- Are missing fields explicit rather than synthesized?
- Does Excel export read from normalized repository data rather than becoming a second truth store?

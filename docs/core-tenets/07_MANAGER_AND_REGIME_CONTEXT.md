# Core Tenet 07 — Manager & Regime Context

## Principle

Sports Hedge should analyse **this version of the team**, not blindly aggregate the club across structurally different eras.

Manager changes are first-class analytical regime changes.

## Non-negotiables

Historical/team analysis should be able to distinguish:

- manager identity and tenure;
- permanent/interim/caretaker status;
- official appointment date and first match managed;
- manager match number;
- days/matches since appointment;
- manager phase;
- season match number and season phase;
- recent regime-change flags.

Suggested manager phases:

```text
DEBUT
VERY_EARLY
EARLY
DEVELOPING
ESTABLISHED
```

New-manager scenarios such as first match / first 3 / first 5 / first 10 should be analyzable independently.

## Structural breaks

Phase 1 prioritizes manager changes. The architecture should remain ready for other regime markers such as:

- start of season;
- promotion/relegation;
- major squad rebuild;
- tactical/formation shift;
- long-term key-player absence.

## Sample discipline

A new manager with five matches must not be treated as equally reliable as a stable regime with 100 matches. Small current-regime samples should retain uncertainty and may partially pool toward team/league priors.

## Review checks

- Does the feature retain manager/regime context where it affects interpretation?
- Can current and previous managerial eras be compared rather than silently blended?
- Does the UI warn when a result mixes materially different regimes?
- Are new-manager samples clearly labelled with N/confidence?

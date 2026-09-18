# Core Tenet 12 — Agent Review Contract

## Principle

Every implementation agent must treat the Sports Hedge core tenets as acceptance criteria, not optional background reading.

A change is not complete simply because tests pass or the requested screen exists.

## Required agent workflow

Before implementation:

1. read `docs/CORE_TENETS.md`;
2. identify which tenets apply;
3. read the linked detailed specifications for the feature area;
4. state any known conflict or ambiguity before changing architecture.

Before handoff:

1. run the relevant tests/lint/build;
2. compare the implementation against every applicable core tenet;
3. explicitly list any tenet that is only partially satisfied;
4. distinguish real/live behavior from demo/fixture behavior;
5. stop for review rather than silently weakening a tenet to make the feature pass.

## PR review template

Agents/reviewers should be able to answer:

```text
Applicable tenets:
- ...

Satisfied:
- ...

Partial / deferred:
- ...

Potential conflicts:
- ...

Data honesty:
- live / historical / modelled / fixture boundaries checked

Safety:
- paper-only boundary checked

Economic correctness:
- approved market catalogue / settlement / fees / FX / liquidity checks reviewed where applicable
```

## Required escalation

A PR should be sent back when it:

- introduces a second canonical identity system;
- weakens settlement equivalence or bypasses the approved-market catalogue;
- invents missing fees/FX/data;
- mixes native currencies incorrectly;
- presents Research as guaranteed arbitrage;
- hides low sample/confidence or regime changes;
- replaces provenance/audit history with destructive updates;
- makes fixture/demo data look live;
- introduces real execution into Phase 1;
- materially violates another core tenet without explicit approval.

For any change touching market recognition, equivalence, matcher rules, market-family coverage or solver eligibility, the handoff must additionally state:

- which approved market archetypes / register keys are affected;
- approved-good examples retained/lost/gained;
- known-bad examples still rejected;
- whether any ambiguous market can enter the solver without an explicit register entry (it must not);
- whether unregistered REVIEW_REQUIRED exceptions are kept out of paper/live execution;
- whether HOT and UNIVERSE consume the same register.

A generic confidence score must not be used as a substitute for Approved Match Register equivalence.

## Product review use

The core-tenets directory should also be used for periodic whole-product reviews. An AI reviewer can be instructed:

> Review Sports Hedge against every file in `docs/core-tenets/`. For each tenet, mark PASS / PARTIAL / FAIL, cite the implementation evidence, and create issues for material gaps.

This is the intended long-term role of these documents.

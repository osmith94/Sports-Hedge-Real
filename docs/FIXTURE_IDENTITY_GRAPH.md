# Fixture identity graph (ambiguous multi-venue assignment)

Status: implemented on top of indexed clustering (#466 / PR #468).
Issue: #472.
Data class: tests in this wave use synthetic/fixture events and scripted
pairwise evidence. Not live, historical, or modelled quotes. PAPER only.

## Problem

Indexed candidate generation made large UNIVERSE identity cheap, but the
second stage was still local: every EventMatcher hit was unioned. That is
correct for obvious cliques (including same-venue market-family siblings).
It does not scale when several venues list confusable fixtures in the same
kickoff window:

- greedy 1:1 pairing can consume the locally heaviest edge and miss a higher
  global assignment;
- union-find can merge two distinct fixtures through a bridge event;
- a 3-venue path with a hard veto on the closing pair can force a match the
  matcher already forbade.

Those cases become more common as venues are added. The graph must not assume
Matchbook, Kalshi, or Polymarket.

## Core principle

**Index first. Score with EventMatcher. Globally assign only the ambiguous
remainder. Fail closed when the global solution is not unique enough.**

EventMatcher hard vetoes and thresholds stay authoritative. This layer does
not change 0.92 / PAPER 0.80, kickoff tolerance, competition veto, squad
category, NFL identity, aliases, or market-family equivalence.

## Stage 1 — indexed candidates

`build_indexed_candidates()` remains the fast first stage. Pair generation is
blocked on sport, known target competition, the authoritative kickoff window,
and squad-category compatibility.

**Candidate-superset rule:** the index is an optimization only. It must retain
every pair the authoritative `EventMatcher` could accept. It may drop
impossible pairs. It must not drop a pair inside that matcher's sport-specific
window because of a stricter key, including an MLB minute embedded in
`scheduled_game_key`. Scheduled team sports use the inclusive five-minute
tolerance. Tennis keeps its 14-day supporting window. MLB Game 1 versus Game 2
stays a hard reject, and a one-sided ordinal stays fail-closed. The window
does not by itself prove two events are the same fixture.

## Stage 2 — pairwise evidence

Every indexed candidate is scored exactly once:

- `could_match() is False` → hard constraint (`prefilter_rejected`)
- `match()` hard-veto reasons → hard constraint
- `match().matched` → weighted edge at matcher confidence
- scored but below threshold → miss, not an edge

Same-venue hits are sibling edges (GAME/BTTS/TOTAL/FTTS splits of one
canonical fixture). They are contracted before cross-venue assignment so a
family split is never consumed as a rival fixture.

## Stage 3 — obvious components stay cheap

After sibling contraction, connected components of matching edges that are a
cross-venue clique with at most one supernode per venue union exactly as
before. No assignment search.

## Stage 4 — constrained maximum-weight assignment

A component is ambiguous when two supernodes of the same venue compete, or
the pairwise evidence is not an obvious clique.

Constraints:

- at most one supernode per venue per canonical cluster
- same-venue siblings may share a cluster
- a cluster of 3+ supernodes must be a pairwise matching clique
- hard vetoes are never overridden
- legal partitions are enumerated deterministically (n ≤ 8 supernodes)

Objective: maximum total confidence of chosen edges. The chosen assignment
must beat the runner-up by `DEFAULT_ASSIGNMENT_MARGIN` (0.03). That margin is
assignment uniqueness, not an EventMatcher threshold.

If the component is larger than the enumeration cap, evidence is incomplete
(truncated candidate scoring), the margin is too small, or a matching path
includes a cross-venue hard veto, the component **fails closed**: sibling
groups stay together, no cross-venue cluster is forced, and provenance
records chosen/rejected edges, margin, and constraints.

## Provenance

Each cluster carries `identity_provenance`:

- method: `obvious_union` / `global_max_weight` / `fail_closed`
- chosen edges
- rejected competing edges
- confidence margin (global weight − second-best)
- greedy-local weight vs global weight on the same evidence
- constraints applied
- fail-closed reason when no cluster is forced

Diagnostics are attached to existing `scan_diagnostics` (not a second
telemetry system):

- `identity_graph_components`
- `identity_graph_obvious_components`
- `identity_graph_ambiguous_components`
- `identity_graph_contradictory_components`
- `identity_graph_fail_closed_components`
- `identity_graph_global_assignments`
- `identity_graph_sibling_groups`

## Invariants preserved

- PAPER-only, no venue writes
- indexed candidate generation still runs first
- EventMatcher is the only pairwise identity oracle
- no Matchbook/Kalshi/Polymarket-only branching in the graph
- HOT / BACKGROUND / ACTIVE independent of this path
- settlement worker untouched
- approved catalogue / market-family equivalence unchanged
- cross-generation incremental identity (#471) reuses EventMatcher scores
  only when fingerprints and the semantic version match; Clear & update
  still invalidates


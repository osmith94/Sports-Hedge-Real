# Core Tenet 20 — Approved Market Catalogue and Exception Review

## Principle

Sports Hedge should **not attempt to understand every football betting contract in the normal arbitrage path**.

The operational scanner must focus on a small, explicit, version-controlled catalogue of **pre-approved market archetypes** whose economic settlement semantics are understood.

The normal path is:

```text
raw venue market
    ↓
approved-market recogniser
    ↓
approved archetype + exact parameters
    ↓
strict cross-venue equivalence
    ↓
economics / solver
```

Anything outside that contract is either:

- `REVIEW_REQUIRED` when it plausibly resembles an approved archetype but evidence is incomplete; or
- `UNSUPPORTED` when it is outside the approved catalogue.

It must not enter the normal arbitrage solver merely because labels look similar or a probabilistic confidence score is high.

The guiding product rule is:

> **Pre-approve common market contracts. Search them deeply. Escalate exceptions. Ignore unsupported noise.**

## Why this is a core tenet

Sports venues expose large numbers of overlapping, novelty and venue-specific contracts.

Trying to probabilistically infer equivalence across every available contract creates:

- unnecessary provider and normalization work;
- noisy diagnostics;
- large ambiguous candidate sets;
- fragile settlement reasoning;
- repeated manual review;
- higher risk of false equivalence.

Sports Hedge gains more value in Phase 1 by supporting a deliberately bounded catalogue of common football markets extremely well.

This is a product-scope decision, not a relaxation of settlement safety.

## 1. Approved market archetypes

An approved archetype is a canonical economic contract, not a literal venue label.

Each archetype must define, as applicable:

- canonical market family;
- period;
- participant/team scope;
- line or handicap parameter;
- outcome state space;
- extra-time / penalties treatment;
- no-goal / draw / void behavior;
- postponement/abandonment assumptions where material;
- required source metadata;
- known prohibited variants.

A venue-specific market can enter the normal arbitrage path only when its recogniser can map it to an approved archetype with all required parameters.

## 2. Initial football catalogue

The intended Phase-1 catalogue should be approximately ten common football archetypes.

The initial target set is:

1. **Match Result / 1X2** — Home / Draw / Away, regulation-time contract.
2. **Both Teams To Score** — Yes / No, regulation-time contract.
3. **Total Goals Over/Under** — exact line required, e.g. 2.5 must equal 2.5.
4. **Team Total Goals Over/Under** — exact team and exact line required.
5. **Handicap** — exact team, exact handicap type and exact line required.
6. **Draw No Bet** — home/away outcome with draw-void treatment explicitly compatible.
7. **Double Chance** — exact outcome combination required: 1X, X2 or 12.
8. **Team To Score** — exact named team and Yes/No settlement contract.
9. **First Team To Score** — exact outcome model and no-goal/void treatment required.
10. **Team Clean Sheet** — exact named team and Yes/No settlement contract.

This list is a target catalogue, not permission to guess unsupported venue semantics.

An archetype becomes operational for a venue pair only when the required venue recognisers and settlement rules are implemented and tested.

Additional archetypes may be added later through explicit review.

## 3. Parameterized archetypes, not hundreds of hard-coded strings

The catalogue should avoid duplicating logic for every numerical line.

For example:

```text
TOTAL_GOALS
period = FULL_TIME
line = 2.5
outcomes = OVER / UNDER
```

and:

```text
TOTAL_GOALS
period = FULL_TIME
line = 3.5
outcomes = OVER / UNDER
```

are two instances of one approved archetype.

Cross-venue equivalence requires the parameters to agree exactly according to the archetype contract.

The same rule applies to:

- handicap lines;
- team-total lines;
- named-team markets;
- double-chance combinations;
- period variants.

## 4. Venue recognisers

Each supported venue may have one or more explicit recognition rules mapping venue market structures into approved archetypes.

Recognition may use:

- venue market name/title;
- structured market type;
- source market IDs/tickers;
- runner/outcome structure;
- rule metadata;
- period metadata;
- line/handicap metadata;
- stable documented venue conventions;
- previously operator-approved mapping rules.

Recognition rules must be deterministic and auditable.

Fuzzy label handling may help identify a possible archetype, but fuzzy similarity alone must not establish executable equivalence.

## 5. Operational equivalence states

The market-equivalence layer should prefer explicit operational states over a generic probabilistic equivalence score.

At minimum:

```text
APPROVED_EQUIVALENT
APPROVED_PARAMETER_MISMATCH
KNOWN_CONTRADICTION
REVIEW_REQUIRED
UNSUPPORTED
```

### APPROVED_EQUIVALENT

Both venue markets normalize to the same approved archetype and all required parameters/outcome semantics agree.

Only this state is eligible for the normal arbitrage solver.

### APPROVED_PARAMETER_MISMATCH

Both are recognizable approved archetypes, but a required parameter differs.

Examples:

- total 2.5 vs total 3.5;
- first half vs full time;
- handicap -1 vs -2;
- different team scope.

This is a hard non-match.

### KNOWN_CONTRADICTION

Explicit settlement evidence conflicts.

Examples:

- regulation-time result vs to-qualify;
- extra time included vs excluded;
- no-goal outcome handled differently in a way that changes economics.

This is a hard non-match.

### REVIEW_REQUIRED

The market plausibly maps to an approved archetype but required evidence is incomplete.

Examples:

- unknown no-goal treatment for First Team To Score;
- generic venue wording with missing period metadata;
- new venue ticker pattern that resembles an existing approved contract.

It must not enter the solver or paper/live execution.

It should enter the exception-review workflow.

### UNSUPPORTED

The market is not part of the approved catalogue or is a novelty proposition Sports Hedge does not currently model.

It should be ignored by the normal arbitrage path while remaining countable in diagnostics.

## 6. Exception review replaces general confidence-based admission

Sports Hedge should not use a high confidence percentage as permission to compare ambiguous betting contracts economically.

When evidence is incomplete:

```text
probable approved archetype
    ↓
REVIEW_REQUIRED
    ↓
exception payload
    ↓
GPT / operator review
    ↓
approve venue recognition rule OR reject
```

The review payload should retain enough source evidence to make the decision auditable, including:

- canonical fixture;
- venue;
- source event and market IDs;
- raw title/name;
- structured market metadata;
- runners/outcomes;
- period;
- line;
- available rule/settlement metadata;
- probable archetype;
- missing evidence;
- detected contradictions.

GPT may assist reasoning, but operator approval remains the authority for adding or changing a production mapping rule.

## 7. Persisted approved rules

An operator-approved exception may become a reusable recognition/mapping rule.

Persisted rules should include:

- rule ID/version;
- venues/scope;
- approved archetype;
- parameter extraction behavior;
- source evidence;
- approval timestamp;
- operator-approved flag;
- provenance/rationale;
- revocation/disable capability.

Approved rules must be shared by HOT and UNIVERSE through the same canonical matcher.

The same exception should not require repeated review once a valid reusable rule exists.

## 8. Search and processing scope

UNIVERSE may still perform broad **event discovery**, because it must find the fixtures that exist.

At the market level, however, Sports Hedge should prioritize and deeply process only markets that are:

- directly recognized as approved catalogue archetypes; or
- plausible approved-archetype exceptions that need review.

Unsupported novelty markets should not consume normal solver/matching effort.

Where provider APIs permit targeted market retrieval, the approved catalogue should be used to reduce unnecessary requests.

Where a provider requires a broader market listing before recognition is possible, Sports Hedge may fetch that listing but should filter before expensive normalization, rule enrichment, order-book retrieval and solver work.

Optimization must not hide a market solely because the metadata required to recognize it was never fetched.

## 9. First Team To Score example

"First Team To Score" illustrates why archetype approval does not mean label-only matching.

One venue may expose:

```text
HOME
AWAY
NO_GOAL
```

while another exposes:

```text
HOME
AWAY
```

with a special 0-0 void rule.

These are not automatically economically equivalent.

The approved archetype must specify the required no-goal/void contract, and each venue recogniser must prove compatibility before the pair can become `APPROVED_EQUIVALENT`.

If that evidence is absent, the state is `REVIEW_REQUIRED`, not a probabilistic executable match.

## 10. Fixture identity may still use confidence

This tenet applies specifically to **market-contract equivalence**.

Fixture identity may still use scored evidence where appropriate, because venue naming and event metadata can differ.

However:

- fixture uncertainty must remain explainable;
- canonical identity safety rules still apply;
- a high fixture score cannot override an explicit wrong-team/wrong-kickoff contradiction;
- market settlement equivalence remains catalogue-based and deterministic.

## 11. Diagnostics

The scanner should report catalogue coverage clearly.

Useful counts include:

- approved markets recognized by archetype;
- approved-equivalent cross-venue pairs;
- parameter mismatches;
- known contradictions;
- review-required exceptions;
- unsupported markets filtered;
- exception rules newly approved/rejected;
- solver candidates by approved archetype.

This gives product coverage visibility without filling the operator console with unsupported market noise.

## 12. Engineering acceptance

Any PR changing market recognition, equivalence or catalogue coverage must report both safety and coverage.

At minimum:

- protected approved-good examples retained;
- approved-good examples newly recognized;
- protected known-bad examples still rejected;
- unexpected approved-good regressions;
- results by archetype;
- review-required count;
- unsupported count.

A strict rule is not automatically an improvement if it silently removes approved catalogue coverage.

## 13. Relationship to Core Tenet 03

Core Tenet 03 remains the settlement-safety authority.

This tenet narrows the operational strategy:

- Tenet 03 says economically different contracts must never be treated as equivalent.
- Tenet 20 says the normal scanner should only attempt executable equivalence inside a pre-approved catalogue.

Therefore an ambiguous market should be **excluded from executable comparison and surfaced for review**, rather than admitted through a general confidence score or silently guessed.

## 14. Relationship to HOT and UNIVERSE

HOT and UNIVERSE must use the same approved catalogue and the same venue recognition rules.

There must not be:

- a HOT-only approved catalogue;
- a UNIVERSE-only catalogue;
- separate exception rules;
- separate settlement semantics.

UNIVERSE discovers approved-market candidates and exceptions.

HOT watches already-known approved-equivalent markets and promoted fixtures.

Execution decides only after approved equivalence plus all economic, freshness, liquidity, treasury and safety gates pass.

## 15. Non-goals

This tenet does not require Sports Hedge to:

- support every market exposed by a venue;
- guess novelty contract semantics;
- model xG contracts as total goals;
- use GPT automatically as the execution-time matcher;
- weaken hard settlement contradictions;
- bypass market equivalence for attractive prices.

## 16. Review checklist

Any matching/catalogue PR should answer:

- [ ] Which approved archetypes are affected?
- [ ] Are venue recognisers deterministic and auditable?
- [ ] Are all required archetype parameters extracted?
- [ ] Can parameter mismatch accidentally enter the solver?
- [ ] Can incomplete settlement evidence accidentally become executable?
- [ ] Are ambiguous probable-archetype markets routed to REVIEW_REQUIRED?
- [ ] Are unsupported markets filtered before expensive solver work where practical?
- [ ] Are operator-approved exception rules reusable by both HOT and UNIVERSE?
- [ ] Are approved-good and known-bad regression counts reported?
- [ ] Does execution remain limited to APPROVED_EQUIVALENT markets?

## Guiding question

For every market pair, Sports Hedge should be able to answer:

> **Is this pair an exact instance of a market contract we have explicitly chosen to support, with compatible parameters and settlement states?**

If yes, compare it.

If not, review it or ignore it — do not guess.

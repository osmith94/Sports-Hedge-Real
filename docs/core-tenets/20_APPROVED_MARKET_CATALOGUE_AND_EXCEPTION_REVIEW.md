# Core Tenet 20 — Approved Market Catalogue and Exception Review

## Principle

Sports Hedge should **not attempt to understand every football betting contract in the normal arbitrage path**.

The operational scanner must focus on a small, explicit, version-controlled catalogue of **pre-approved market archetypes** whose economic settlement semantics are understood.

The normal path is:

```text
raw venue market
    ↓
venue-native archetype recogniser
    ↓
Approved Match Register canonical key + exact parameters
    ↓
paper / solver (PAPER mode) or live-execution gate (APPROVED_EQUIVALENT only)
```

Anything outside that contract is either:

- rejected when parameters/structure mismatch; or
- `NOT_LISTED` / `VENUE_UNAVAILABLE` when a genuine venue market is absent; or
- ignored/`UNSUPPORTED` when it is outside the approved catalogue/register.

It must not enter the normal arbitrage solver merely because labels look similar or a probabilistic confidence score is high. Runtime scanning does not open `REVIEW_REQUIRED` for a registered Matchbook↔Kalshi row.

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
PAPER_ASSUMED_EQUIVALENT
APPROVED_PARAMETER_MISMATCH
KNOWN_CONTRADICTION
REVIEW_REQUIRED
UNSUPPORTED
```

### APPROVED_EQUIVALENT

Both venue markets normalize to the same approved archetype and all required parameters/outcome semantics agree.

Only this state is eligible for **live execution**, and only when the same
pair is also admitted by the Approved Match Register. Unregistered
independently proven fingerprints remain census/onboarding knowledge.
No register entry means no runtime matcher match and no PAPER solver
admission, even when both settlement fingerprints are independently complete.

### PAPER_ASSUMED_EQUIVALENT

Owner-approved Phase-1 **paper-mode** path for Matchbook↔Kalshi rows in the Approved Match Register:

| Canonical key | Matchbook | Kalshi | Structural requirements |
| --- | --- | --- | --- |
| MATCH_RESULT_FT | Match Odds / Final Result | GAME | FULL_TIME + HOME/DRAW/AWAY |
| BTTS_FT | Both Teams To Score | BTTS | FULL_TIME + YES/NO |
| TOTAL_GOALS_FT:{line} | Total Goals | TOTAL | FULL_TIME + exact safe half-line + OVER/UNDER |
| FTTS_FT | First Team To Score | FTTS | FULL_TIME + HOME/AWAY/NO_GOAL |

The register itself is the PAPER cross-venue equivalence decision. Once canonical fixture and canonical market identity/parameters match, the system must not re-litigate settlement text, mapping confidence, or learned labels on every scan.

This state is visibly labelled, carries `settlement_assumption=regulation_time`, may enter the **paper** solver and Priority Alerts path, and is never live-execution eligible. It is not independently proven settlement.

Kalshi cancel/reschedule-to-fair-price wording does not block PAPER admission. Extra time, penalties, and to-qualify are unregistered native archetypes (they do not map to the onboarded full-time keys). Incomplete outcome sets, wrong period, and TOTAL line mismatch remain fail-closed through the register structural gate. Settlement fingerprints are not a second runtime tribunal after register lookup.

Independently proven complete fingerprints remain `APPROVED_EQUIVALENT` as **offline census/onboarding knowledge**. They do not grant runtime PAPER matcher or solver admission until that venue-native archetype is added to the Approved Match Register. DNB, handicap, double chance, team total, team-to-score, and clean sheet stay explicit in the versioned registry as deferred / unsupported / venue-unavailable and must not trigger Phase-1 settlement/depth/solver/HOT work.

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

## 6. Onboarding review replaces runtime confidence-based admission

Sports Hedge should not use a high confidence percentage as permission to compare ambiguous betting contracts economically.

When a **new** venue-native archetype is being onboarded:

```text
probable approved archetype
    ↓
onboarding review payload
    ↓
GPT / operator review
    ↓
add versioned Approved Match Register entry OR reject
```

Runtime scanning then consumes the register. It does not consult `minimum_mapping_confidence`, learned market-label rules, or the dynamic mapping-review/ChatGPT store to decide whether a registered row is equivalent.

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

- registered venue-native archetypes with matching canonical keys; or
- unregistered candidates reserved for **onboarding** review, not runtime scanner admission.

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

If that evidence is absent for an **unregistered** FTTS contract, the pair is not admitted. A registered complete HOME/AWAY/NO_GOAL FTTS row is paper-admitted from the register without a runtime settlement-proof gate.

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

Optimization must not hide an approved market because required recognition metadata was never fetched. Strict pruning is not an improvement when it turns a narrower failure into a fixture-wide rejection. UNIVERSE and HOT share the same Approved Match Register and the same recognition semantics. A market-level 404, a provider timeout, or unknown/incomplete work must not skip sibling registered relationships before they reach that gate. `0 equivalent` remains correct only when no registered relationship is structurally valid.

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

## 14. Relationship to UNIVERSE, catalogue and the price engine

Core Tenet 19 defines the authoritative runtime flow:

```text
UNIVERSE discovery
    ↓
durable approved-market catalogue
    ↓
one price engine
    ↓
HOT / BACKGROUND priority
```

UNIVERSE and the price engine must use the same Approved Match Register and venue recognition rules.

There must not be:

- a HOT-only approved catalogue;
- a UNIVERSE-only catalogue;
- a HOT-only matcher or equivalence tribunal;
- separate exception rules;
- separate settlement semantics.

UNIVERSE discovers and maintains approved-market identity: canonical fixture, canonical key, exact parameters, exact native IDs and required outcome mapping.

HOT and BACKGROUND then reprice those already-known catalogue rows from exact native IDs. They do not re-run broad discovery or re-litigate registered equivalence on every cadence.

For parameterized archetypes such as Total Goals, every exact approved line is its own catalogue row. Discovery completeness is family-scoped: successful discovery of MATCH_RESULT or BTTS must not be used as evidence that TOTAL or FTTS is absent when that family timed out, was deferred, or was not queried.

Execution decides only after approved equivalence plus the applicable economic, treasury and PAPER/execution rules.

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
- [ ] Are unregistered/unsupported markets filtered before expensive solver work where practical?
- [ ] Does runtime scanning consume the Approved Match Register rather than mapping confidence?
- [ ] Are operator-approved register entries reusable by both HOT and UNIVERSE?
- [ ] Are approved-good and known-bad regression counts reported?
- [ ] Does execution remain limited to APPROVED_EQUIVALENT markets?

## Guiding question

For every market pair, Sports Hedge should be able to answer:

> **Is this pair an exact instance of a market contract we have explicitly chosen to support, with compatible parameters and settlement states?**

If yes, compare it.

If not, review it or ignore it — do not guess.

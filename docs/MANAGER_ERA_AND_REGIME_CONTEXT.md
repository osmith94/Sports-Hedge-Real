# Sports Hedge — Manager Era & Regime Context

## Purpose

Sports Hedge must treat major structural changes inside a football club as **analytical regime changes**, not just as ordinary historical matches.

The biggest example is a **change of manager**.

A team's behaviour under one manager may not be representative of the same club under another manager. Scenario Response Profiles, Trend Explorer and historical team analysis therefore need to know which managerial era each match belongs to and whether a match sits close to a major transition.

The same principle also applies to the **start of a new season**, especially where a team has:
- a new manager;
- a major tactical reset;
- substantial squad turnover;
- promotion/relegation;
- a significant change in competition context.

The objective is to stop Sports Hedge from blending structurally different versions of the same team into one misleading historical average.

---

## 1. Product Concept

Name:

```text
Manager Era & Regime Context
```

Short internal label:

```text
REGIME_CONTEXT
```

This should become a first-class analytical dimension alongside:

```text
team
competition
season
home / away
pre-match strength
score state
match minute
scenario
market
manager era
regime phase
```

---

## 2. Managerial Eras

Every team match should map to the manager responsible for that match.

Suggested entities:

```text
managers
manager_tenures
match_manager_context
regime_events
```

### managers

```text
manager_id
canonical_name
country
source_reference
```

### manager_tenures

```text
manager_tenure_id
team_id
manager_id
appointment_type
appointment_date
effective_from
effective_to
first_match_id
last_match_id
is_interim
source_name
source_reference
retrieved_at
```

Appointment type should distinguish:

```text
PERMANENT
INTERIM
CARETAKER
UNKNOWN
```

The system should preserve official appointment dates separately from the first match actually managed.

---

## 3. Match Manager Context

Each historical match should expose contextual fields such as:

```text
match_id
team_id
manager_id
manager_tenure_id
manager_match_number
days_since_manager_start
matches_since_manager_start
manager_phase
season_match_number
days_since_season_start
season_phase
regime_change_recent
```

This allows analysis such as:

> A stable long-running managerial era should normally be considered one regime.

versus:

> A team three matches into a new managerial era should be treated as a structurally different sample from the same club under the previous manager.

The historical team name alone is not enough.

---

## 4. Manager Phase

Suggested manager-phase buckets:

```text
DEBUT          = first match
VERY_EARLY     = matches 2–3
EARLY          = matches 4–10
DEVELOPING     = matches 11–25
ESTABLISHED    = matches 26+
```

The exact thresholds should remain configurable.

For analysis, we should also retain the raw match number rather than only the bucket.

---

## 5. New Manager as a Scenario

A managerial change is itself a Scenario Lab event.

Initial scenarios:

```text
NEW_MANAGER_DEBUT
NEW_MANAGER_FIRST_3
NEW_MANAGER_FIRST_5
NEW_MANAGER_FIRST_10
INTERIM_MANAGER
PERMANENT_MANAGER_AFTER_INTERIM
MANAGER_CHANGE_MID_SEASON
NEW_MANAGER_AT_SEASON_START
```

Metrics can include:

```text
corners
cards
goals
shots
possession
market implied probability
opening-to-closing odds movement
scenario response coefficients
```

Example research question:

> How do Premier League teams' corner rates change in their first five matches under a new manager compared with their previous 10 matches?

Another:

> When a favourite goes behind, does its corner response materially change under the new manager versus the previous managerial era?

---

## 6. Scenario Response Profiles Must Be Regime-Aware

Scenario Response Profiles should support:

```text
TEAM + SCENARIO
TEAM + MANAGER ERA + SCENARIO
LEAGUE + SCENARIO
LEAGUE + NEW-MANAGER PHASE + SCENARIO
```

The platform should never silently combine separate managerial eras when the split materially changes the result.

---

## 7. Structural Break Detection

Manager changes should be explicit structural breaks.

The system should also support future structural-break markers for:

```text
start of season
promotion
relegation
major squad rebuild
formation / tactical change
key player long-term absence
ownership / sporting-director change
competition change
```

Manager changes are the highest-priority structural break for Phase 1.

---

## 8. Start-of-Season Context

The start of a new season should also be explicitly tagged.

Suggested season phases:

```text
OPENING_3
OPENING_5
OPENING_10
MID_SEASON
RUN_IN
```

Every match should retain:

```text
season_match_number
days_since_season_start
season_phase
```

A new season should not automatically mean a completely new behavioural regime, but it should be available as a segmentation boundary.

Particularly important combinations:

```text
same manager + new season
new manager + new season
same manager + major squad turnover
promoted team + new season
relegated team + new season
```

---

## 9. Regime Event Table

Suggested normalized structure:

```text
regime_event_id
team_id
event_type
effective_at
effective_match_id
previous_regime_id
new_regime_id
manager_id
season_id
source_name
source_reference
retrieved_at
confidence
metadata
```

Initial event types:

```text
MANAGER_APPOINTED
MANAGER_DEPARTED
INTERIM_APPOINTED
PERMANENT_MANAGER_APPOINTED
SEASON_STARTED
PROMOTED
RELEGATED
```

This must preserve source provenance and support later correction rather than overwriting historical evidence.

---

## 10. Analytical Weighting

When a team has changed manager recently, older team data should not automatically receive the same weight as current-manager data.

Suggested hierarchy:

```text
current manager / current regime
    highest relevance

same team, previous manager
    lower relevance

league-wide comparable scenarios
    useful prior / benchmark
```

For small new-manager samples, the model should partially pool toward:

```text
team historical behaviour
+
league scenario behaviour
+
manager's prior history where available
```

rather than overfitting a handful of matches.

---

## 11. Manager Historical Profile

Future enhancement:

A manager may carry behavioural tendencies between clubs.

Example analytical questions:

```text
Does this manager's team consistently increase corners when trailing?
Does this manager suppress game tempo when leading?
Do this manager's teams accumulate more tactical cards?
How quickly do their teams react after conceding?
```

This creates a future:

```text
Manager Response Profile
```

similar to a Team Scenario Response Profile.

This should not be Phase 1 blocking scope, but the data model should make it possible.

---

## 12. UI Requirements

### Team page

Example:

```text
Current Regime

Manager: ...
Manager phase: EARLY
Matches in regime: 6
Regime start: ...
```

Historical charts should visibly mark manager changes.

### Scenario Lab

Filters:

```text
Manager
Manager phase
Current regime only
Include previous regimes
Season phase
Matches since appointment
```

### Warning state

If a result combines materially different eras:

```text
⚠ Regime mix

This sample contains matches from multiple managerial eras.
Current-manager sample: N=6
Full-team sample: N=42
```

The user should be able to compare both rather than having the distinction hidden.

---

## 13. Example Insight

A useful Sports Hedge output should look like:

> Under the current manager, Team A's corner rate after falling behind as a favourite is +1.4 corners per 15 minutes above baseline (N=14). Under the previous manager it was +0.5 (N=27). The current-regime response is also +0.6 above the league benchmark.

Not:

> Team A gets more corners when losing.

---

## 14. Historical Repository Integration

The historical football repository should eventually add:

```text
managers
manager_tenures
regime_events
match_manager_context
```

and expose manager/regime fields through historical query APIs and Excel exports.

Suggested Excel additions:

```text
Managers
Manager Tenures
Regime Events
Match Manager Context
```

The existing:

```text
Matches
Team Stats
Match Events
Lineups
Coverage
Sources
```

remain unchanged.

---

## 15. Coverage Reporting

Historical coverage should report:

```text
manager known
manager tenure known
manager start date known
manager match number derivable
season phase derivable
regime event provenance available
```

Unknown manager context should not be silently treated as the same regime.

---

## 16. Relationship to Scenario Response Profiles

```text
Historical Football Data
        |
        +---- matches
        +---- events
        +---- team stats
        +---- lineups
        +---- manager tenures
        +---- regime events
        |
        v
Scenario Detection
        |
        +---- score state
        +---- opponent strength
        +---- match minute
        +---- manager era
        +---- season phase
        |
        v
Scenario Response Profiles
        |
        +---- team response
        +---- league benchmark
        +---- manager-era split
        +---- current-regime relevance
        |
        v
Trend Explorer / Scenario Lab
```

---

## 17. Phase 1 Priority

For the first historical backfill, the minimum manager data required is:

```text
team
manager
tenure start
tenure end
interim/permanent flag
match-to-manager mapping
matches since appointment
season match number
season phase
```

This is enough to make Scenario Response Profiles regime-aware.

---

## 18. Guiding Principle

Sports Hedge should answer:

> "How does this version of the team behave?"

not merely:

> "How has this club behaved over several seasons?"

A football club is a persistent identity, but its statistical behaviour can change sharply when the manager, tactical system or season context changes.

Manager Era & Regime Context exists to preserve that distinction.

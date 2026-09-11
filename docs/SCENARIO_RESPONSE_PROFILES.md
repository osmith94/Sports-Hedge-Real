# Sports Hedge — Scenario Response Profiles

## Purpose

Scenario Response Profiles (SRP) measure how an individual team behaves when a specific football situation occurs, and how that response differs from the league-wide response to the same situation.

The key product question is:

> When this specific football situation occurs, how does this team historically respond — and how is that response different from the league?

This belongs to the Analysis / Market Intelligence / Trend Explorer side of Sports Hedge, separate from the arbitrage engine.

## Core metric: Scenario Response Coefficient (SRC)

For a team, scenario, metric and time window, calculate:

- team baseline
- team scenario value
- team scenario effect
- league baseline
- league scenario value
- league scenario effect
- team excess response = team scenario effect - league scenario effect
- sample size
- uncertainty/confidence
- stability across seasons/eras
- data quality

SRC should summarize the strength and direction of the team's excess response. Raw correlation may be shown as supporting evidence but must not be the headline metric.

## Example

Scenario: pre-match favourite falls behind against an underdog.
Metric: team corners.
Window: next 15 minutes.

A team such as Arsenal may show a large positive corner response compared with its own baseline and the Premier League benchmark, while another team may show only a small increase. Sports Hedge should expose that difference directly.

## Scenario definition

Scenarios must be structured, configurable definitions rather than hard-coded prose. Dimensions can include:

- competition
- team
- home/away
- pre-match implied-probability or odds bucket
- favourite/underdog status
- opponent strength
- score state
- match minute bucket
- cards/red cards
- lineup/team-sheet context
- manager era
- season phase

Trigger events can include GOAL, CONCEDED_GOAL, RED_CARD, OPPONENT_RED_CARD, YELLOW_CARD, PENALTY, TEAM_SHEET, PLAYER_OUT, PLAYER_IN, INJURY_NEWS, SUBSTITUTION and HALF_TIME.

## Initial scenario library

Prioritize:

- favourite concedes first
- favourite trailing after 30/60/70 minutes
- underdog takes the lead
- team drawing after 70 minutes
- team leading by one after 70/80 minutes
- team/opponent red card while leading, level or trailing
- key attacker/defender/goalkeeper omitted
- early goal scored/conceded
- late equaliser/winner
- defender or holding midfielder booked early
- post-Champions-League / short-rest context

## Initial metrics

For the first historical dataset prioritize:

- corners
- yellow cards
- red cards
- goals

Extend later to shots, shots on target, xG where legitimately available, possession, market implied probability, spread and liquidity.

## Time-response windows

Every trigger should support at least:

- 0–5 minutes
- 0–10 minutes
- 0–15 minutes
- 0–30 minutes
- remainder of match

## Team, league and regime levels

Every result should support:

1. league scenario response
2. team scenario response
3. team-vs-league excess response
4. team + manager-era scenario response where data exists

Small team samples should be shrunk/partially pooled toward the league benchmark rather than treated as fully reliable.

## Required outputs

Each team/scenario/metric/window combination should expose:

- sample_size
- team_baseline
- team_scenario_value
- team_effect
- league_baseline
- league_scenario_value
- league_effect
- team_excess_response
- median / mean / p25 / p75
- confidence interval where practical
- positive response rate
- stability score
- Scenario Response Coefficient
- data quality status

## Scenario Response Index

The UI may also expose a normalized 0–100 Scenario Response Index for ranking teams. It must be derived from SRC, confidence and stability and must never hide the underlying values.

## Product views

### Scenario Lab

A team × scenario ranking/matrix with filters for scenario, metric, time window, competition, season, manager era and data quality.

### Team page

Each team should have a Scenario Response Profile showing strongest responses, weakest responses, current-manager responses, corner/card/goal scenarios, current form context and drill-down to individual historical matches.

### Scenario detail

Show league benchmark, team rankings, SRC, sample size, confidence, historical distribution, time-response curve, manager-era splits and individual matches.

## Historical odds overlay

When historical odds coverage exists, join scenario behaviour to market pricing. Any displayed edge must account for margin/fees/spread/liquidity/slippage/sample size and data confidence. This remains analysis, not automatic execution.

## Suggested storage

- scenario_definitions
- scenario_occurrences
- scenario_metric_observations
- scenario_team_aggregates
- scenario_league_aggregates
- scenario_response_profiles
- scenario_response_scores

## Guiding principle

Sports Hedge should answer "how does this team react to this situation?" rather than only "what normally happens in football?".

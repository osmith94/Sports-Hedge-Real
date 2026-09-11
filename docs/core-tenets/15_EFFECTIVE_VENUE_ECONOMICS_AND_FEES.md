# Core Tenet 15 — Effective Venue Economics & Fee-Aware Pricing

## Principle

Sports Hedge must compare **effective executable economics**, not headline odds.

Two venues showing the same decimal price can represent materially different economics once venue rules, side/action, order role, fee basis, account tier, market class, currency, depth and settlement treatment are applied.

This principle applies to both **Arbitrage** and **Research Value**.

## Non-negotiables

1. Headline odds are never sufficient for ranking or declaring an arbitrage/value signal.
2. Every economically relevant quote must retain the venue and the side/action being evaluated (for example BACK/BUY/LAY/SELL where supported).
3. Fee treatment must be capable of varying by venue, market, side/action, order role (for example maker/taker where relevant), account or fee tier, currency and effective date.
4. The fee model must preserve the fee basis rather than assuming all venues charge a simple percentage of winnings.
5. Required unknown fees fail closed. `unknown` must never silently become zero.
6. Fee assumptions must be timestamped and retain provenance/source so a paper decision can be reproduced later.
7. Arbitrage and Research must share the same provider-neutral effective-economics layer rather than implementing conflicting fee formulas independently.
8. Depth and fees must be evaluated together where economics vary with stake/levels consumed.
9. Currency conversion is separate from venue fees but must be included before cross-currency profitability is reported in GBP.
10. Accounting must post actual venue fees/commissions separately from betting/trading P&L where they can be identified.

## Required fee/cost snapshot dimensions

A production-ready fee snapshot should support, where applicable:

```text
venue
account_or_fee_tier_reference
source_market_id / market_class
canonical_action_or_side
maker_taker_or_order_role
fee_basis
rate
fixed_amount
formula_parameters
currency
effective_from
captured_at
source / provenance
known_status
```

No secrets or execution credentials belong in the snapshot.

## Fee bases

The model must be extensible to fee structures such as:

```text
PROFIT_COMMISSION
STAKE_OR_NOTIONAL
PAYOUT
TRANSACTION
FIXED
FORMULA
NONE_CONFIRMED
UNKNOWN
```

This list is illustrative rather than venue-specific. The system should model the actual authorised venue rule in force at decision time instead of fitting every venue into one generic formula.

## Economic interface

Downstream engines should ask a provider-neutral cost model for **net outcome economics** for a specific leg/stake rather than directly applying a hard-coded commission formula.

Useful outputs include:

```text
gross_odds_or_price
gross_payoff
venue_fee
other_known_leg_costs
net_payoff
net_decimal_equivalent
cost_adjusted_implied_probability
fee_snapshot_id
```

For lay/sell/synthetic legs, state-payoff economics should be used instead of forcing a back-bet decimal-odds representation where inappropriate.

## Arbitrage implications

An arb may only be considered valid when the minimum payoff across valid settlement states remains positive after:

```text
venue-specific fees
side/order-role effects
executable depth/slippage
FX/conversion costs
other explicitly modelled transaction costs
```

A nominal arb that disappears after fees is not an arb.

The solver should be free to select a venue combination that is economically superior even if another venue shows a more attractive headline price.

## Research Value implications

A Research Value signal must compare the model probability against the **best economically equivalent net price**, not the best displayed price.

For example, if two equivalent venues show similar headline odds but one produces a better net payout after its applicable fee structure, the latter is the price used for value ranking.

## Violation examples

This tenet is violated if Sports Hedge:

- uses one flat commission rate for every market/side on a venue when the real economics differ;
- treats a missing fee as 0%;
- chooses the highest displayed odds without comparing net payout;
- applies a back-bet profit haircut to a lay/sell leg whose economics use a different basis;
- hard-codes current venue fee rates without effective dates/provenance;
- calculates an arb before fees and merely subtracts an approximate fee afterward;
- duplicates one fee implementation in Research and a different one in Arbitrage.

## Review checks

Reviewers should verify:

- [ ] venue and action/side are explicit on every economic leg;
- [ ] required fee inputs are known or the opportunity fails closed;
- [ ] fee basis/formula is explicit rather than assumed;
- [ ] fee snapshot provenance and captured/effective time are retained;
- [ ] net payoff is calculated before arb/value ranking;
- [ ] maker/taker/account-tier/market differences can be represented where relevant;
- [ ] cross-currency costs are applied separately and correctly;
- [ ] Research and Arbitrage use a shared effective-economics contract;
- [ ] actual accounting fees can be reconciled to the assumptions used;
- [ ] tests prove that a headline opportunity can disappear after fees and that different fee structures can change the best venue.

## Guiding question

Sports Hedge should answer:

> After the exact costs that apply to this venue, market, side and stake, what is the net executable payoff — and is this still the best economic route?

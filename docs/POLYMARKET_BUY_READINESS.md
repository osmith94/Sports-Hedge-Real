# Polymarket BUY readiness

This note is the architecture rule for a separate Polymarket execution fix.
This cleanup does not change Polymarket execution files and does not place orders.

## Rule

BUY readiness asks only whether the exact BUY can execute:

- sufficient collateral for that BUY
- sufficient allowance for the actual required `exchange_v3` spender

A generic platform-wide `is_fully_approved` result must not become a live BUY veto for unrelated perps or auto-redeem permissions.

One risk, one control, one authority. Collateral and the spender allowance for this order are the BUY controls. Other approval bits stay diagnostic unless that specific order needs them.

import { LiquidityPools } from "../../components/liquidity-pools";
import { DEMO_LIQUIDITY_POOLS } from "../../lib/arbitrage-ops";

export default function TreasuryPage() {
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Treasury</div>
          <h1>Native liquidity pools</h1>
          <p className="page-subtitle">
            Matchbook GBP, Smarkets GBP and Polymarket USD remain separate. USD and GBP are never summed as one cash
            figure. These balances are DEMO / FIXTURE until the accounting ledger lands.
          </p>
        </div>
        <div className="demo-label">DEMO / FIXTURE DATA</div>
      </div>
      <LiquidityPools pools={DEMO_LIQUIDITY_POOLS} />
    </>
  );
}

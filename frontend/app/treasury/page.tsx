import { LiquidityPools } from "../../components/liquidity-pools";
import { getPaperLiquidityPools } from "../../lib/api";

export const dynamic = "force-dynamic";

export default async function TreasuryPage() {
  let snapshot = null;
  let available = true;
  try {
    snapshot = await getPaperLiquidityPools();
  } catch {
    available = false;
  }

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Treasury</div>
          <h1>Paper standing capital</h1>
          <p className="page-subtitle">
            Matchbook GBP, Polymarket USD and Smarkets GBP stay native. USD and GBP are never summed as one cash figure.
            These are PAPER CAPITAL / HYPOTHETICAL balances, not live venue funds. Smarkets stays excluded from the solver.
          </p>
        </div>
        <div className="demo-label">PAPER CAPITAL · HYPOTHETICAL</div>
      </div>
      <LiquidityPools snapshot={snapshot} available={available} />
    </>
  );
}

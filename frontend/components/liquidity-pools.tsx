import { LiquidityPool } from "../lib/arbitrage-ops";
import { money } from "../lib/format";

export function LiquidityPools({ pools }: { pools: LiquidityPool[] }) {
  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <div className="panel-title">Liquidity pools</div>
          <div className="panel-meta">Native-currency balances. USD and GBP are never summed as one cash figure.</div>
        </div>
        <span className="demo-chip">DEMO / FIXTURE · NO TREASURY LEDGER</span>
      </div>
      <div className="pool-grid">
        {pools.map((pool) => (
          <div className={`pool-card currency-${pool.nativeCurrency.toLowerCase()}`} key={`${pool.venue}-${pool.nativeCurrency}`}>
            <div className="pool-top">
              <div>
                <div className="pool-venue">{pool.venue}</div>
                <div className="pool-ccy">{pool.nativeCurrency} native</div>
              </div>
              <span className={pool.supported ? "demo-chip" : "ops-status is-reject"}>
                {pool.supported ? "DEMO" : "NOT FUNDED"}
              </span>
            </div>
            {pool.supported ? (
              <>
                <div className="pool-rows">
                  <div><span>Available</span><strong>{money(pool.available, pool.nativeCurrency)}</strong></div>
                  <div><span>Locked</span><strong>{money(pool.locked, pool.nativeCurrency)}</strong></div>
                  <div><span>Transit</span><strong>{money(pool.transit, pool.nativeCurrency)}</strong></div>
                </div>
                <div className="pool-gbp">GBP carrying value {money(pool.gbpCarryingValue)} · accounting/FX demo</div>
              </>
            ) : (
              <div className="pool-unsupported">Smarkets adapter is reserved. No native pool is shown as available.</div>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

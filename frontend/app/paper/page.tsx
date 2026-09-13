import { PaperTradeBook } from "../../components/paper-trade-book";
import { HoldVsUnwindCard } from "../../components/hold-vs-unwind";
import {
  getActivePaperTrades,
  getClosedPaperTrades,
  getPaperTradeSummary,
  PaperTrade,
  PaperTradeBookSummary,
} from "../../lib/api";

export const dynamic = "force-dynamic";

export default async function PaperPortfolioPage() {
  let summary: PaperTradeBookSummary | null = null;
  let active: PaperTrade[] = [];
  let closed: PaperTrade[] = [];
  let apiAvailable = true;

  try {
    [summary, active, closed] = await Promise.all([
      getPaperTradeSummary(),
      getActivePaperTrades(),
      getClosedPaperTrades(),
    ]);
  } catch {
    apiAvailable = false;
  }

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Paper Portfolio</div>
          <h1>Operator paper trade book</h1>
          <p className="page-subtitle">
            PAPER MODE records only. Headline metrics and tables come from persisted SQLite paper trades
            and the paper subledger. Guaranteed-profit-at-open is a solver snapshot, not realised P&L.
            Phase 1 still does not place venue orders.
          </p>
        </div>
        <div className="demo-label">PAPER MODE · RECORDED TRADES</div>
      </div>
      <PaperTradeBook summary={summary} active={active} closed={closed} apiAvailable={apiAvailable} />
      <div style={{ height: 14 }} />
      <section className="grid-equal">
        <div className="panel">
          <div className="panel-header">
            <div className="panel-title">Hold vs rotate</div>
          </div>
          <div className="panel-body">
            <HoldVsUnwindCard
              emptyHint="8D hold-vs-unwind is modelled from executable reverse-side quotes via POST /paper/trades/{id}/close-plan. Spread convergence and clock estimates never release capital. Open the labelled /demo fixture-replay utility when you need DEMO / FIXTURE REPLAY."
            />
          </div>
        </div>
        <div className="panel">
          <div className="panel-header">
            <div className="panel-title">Live execution</div>
          </div>
          <div className="panel-body">
            <div className="empty-live">
              Disabled. Phase 1 contains no bet-placement route. Live mode remains a separate gated engineering phase.
            </div>
          </div>
        </div>
      </section>
    </>
  );
}

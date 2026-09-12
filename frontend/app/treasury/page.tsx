import { TreasuryBoard } from "../../components/treasury-board";
import { getPaperTreasury } from "../../lib/api";

export const dynamic = "force-dynamic";

export default async function TreasuryPage() {
  let snapshot = null;
  let available = true;
  try {
    snapshot = await getPaperTreasury();
  } catch {
    available = false;
  }

  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Treasury</div>
          <h1>Paper venue bankrolls</h1>
          <p className="page-subtitle">
            Matchbook GBP, Polymarket USD and Kalshi USD are independent paper pools. Native
            amounts never mix. GBP figures are carrying values from an explicit FX snapshot, not
            live venue funds.
          </p>
        </div>
        <div className="demo-label">PAPER MODE · HYPOTHETICAL CAPITAL</div>
      </div>
      <TreasuryBoard snapshot={snapshot} available={available} />
    </>
  );
}

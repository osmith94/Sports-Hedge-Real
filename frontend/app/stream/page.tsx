import { StreamPanel } from "../../components/stream-panel";

export const dynamic = "force-dynamic";

export default function StreamPage() {
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">STREAM</div>
          <h1>Native-token market feed into Price-2</h1>
          <p className="page-subtitle">
            Manual one-fixture Polymarket public market WS observer. Significance-aware coalesced Matchbook
            exact-ID depth feeds the existing Price-2 and opening path. Execution stays disarmed by default.
          </p>
        </div>
      </div>
      <StreamPanel />
    </>
  );
}

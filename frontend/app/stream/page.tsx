import { StreamPanel } from "../../components/stream-panel";

export const dynamic = "force-dynamic";

export default function StreamPage() {
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">STREAM Phase 1</div>
          <h1>Shadow market-feed surveillance</h1>
          <p className="page-subtitle">
            Manual one-fixture Polymarket public market WS observer. Matchbook refresh uses the shared
            provider-access layer. Not a HOT replacement, not Price-2, and not paper entry.
          </p>
        </div>
      </div>
      <StreamPanel />
    </>
  );
}

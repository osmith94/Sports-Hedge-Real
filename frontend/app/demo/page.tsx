import { DemoWalkthroughBoard } from "../../components/demo-walkthrough";

export const dynamic = "force-dynamic";

export default function DemoWalkthroughPage() {
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Advanced / test</div>
          <h1>Fixture replay utility</h1>
          <p className="page-subtitle">
            Not the operator console. Use this labelled path only when you need DEMO / FIXTURE REPLAY
            because no live qualifying arb exists. Normal paper operations stay on `/`.
          </p>
        </div>
        <div className="demo-label">DEMO / FIXTURE REPLAY · not live operations</div>
      </div>
      <DemoWalkthroughBoard />
    </>
  );
}

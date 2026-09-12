import { Suspense } from "react";
import { ScenarioLabClient } from "../../components/scenario-lab/scenario-lab-client";

export default function ScenarioLabPage() {
  return (
    <Suspense fallback={<div className="empty-live">Loading Scenario Lab…</div>}>
      <ScenarioLabClient />
    </Suspense>
  );
}

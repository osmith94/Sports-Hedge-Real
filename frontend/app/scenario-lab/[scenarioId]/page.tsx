import { notFound } from "next/navigation";
import { Suspense } from "react";
import { ScenarioLabClient } from "../../../components/scenario-lab/scenario-lab-client";
import { scenarios } from "../../../lib/scenario-fixtures";
import { canonicalScenarioId, scenarioLabScenarioId } from "../../../lib/demo/ids";

type PageProps = {
  params: Promise<{ scenarioId: string }>;
};

export default async function ScenarioLabDetailPage({ params }: PageProps) {
  const { scenarioId } = await params;
  const labId = scenarioLabScenarioId(canonicalScenarioId(scenarioId));
  if (!scenarios.some((item) => item.id === labId || item.id === scenarioId)) {
    notFound();
  }
  return (
    <Suspense fallback={<div className="empty-live">Loading Scenario Lab…</div>}>
      <ScenarioLabClient key={labId} initialScenarioId={labId} />
    </Suspense>
  );
}

import Link from "next/link";
import { notFound } from "next/navigation";
import type { Metadata } from "next";

import { PriorityAlertDetail } from "../../../../components/arbitrage/priority-alerts/priority-alert-detail";
import {
  getPriorityAlert,
  getPriorityAlertProviderMeta,
  listPriorityAlerts,
} from "../../../../lib/priority-alerts/provider";

type PageProps = {
  params: Promise<{ alertId: string }>;
};

export async function generateStaticParams() {
  return listPriorityAlerts().map((alert) => ({ alertId: alert.alertId }));
}

export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { alertId } = await params;
  const alert = getPriorityAlert(alertId);
  if (!alert) return { title: "Priority alert" };
  return {
    title: `${alert.severity.replaceAll("_", " ")} · ${alert.event.homeTeam} v ${alert.event.awayTeam}`,
  };
}

export default async function PriorityAlertDetailPage({ params }: PageProps) {
  const { alertId } = await params;
  const alert = getPriorityAlert(alertId);
  if (!alert) notFound();
  const meta = getPriorityAlertProviderMeta();

  return (
    <>
      <div className="pa-detail-nav">
        <Link href="/arbitrage/priority-alerts">← All priority alerts</Link>
        <span className="pa-chip pa-chip-paper">PAPER MODE</span>
        <span className="pa-chip pa-chip-demo">{meta.label}</span>
      </div>
      <PriorityAlertDetail alert={alert} />
    </>
  );
}

import Link from "next/link";
import type { Metadata } from "next";

import { FixtureInventoryWorkspace } from "../../../../components/fixture-inventory";
import { getFixtureDetail } from "../../../../lib/api";

type PageProps = {
  params: Promise<{ eventId: string }>;
};

export const dynamic = "force-dynamic";

export async function generateMetadata({ params }: PageProps): Promise<Metadata> {
  const { eventId } = await params;
  try {
    const detail = await getFixtureDetail(decodeURIComponent(eventId));
    return {
      title: `${detail.fixture.home_team} v ${detail.fixture.away_team} · fixture inventory`,
    };
  } catch {
    return { title: "Fixture inventory" };
  }
}

export default async function FixtureDetailPage({ params }: PageProps) {
  const { eventId } = await params;
  const canonicalEventId = decodeURIComponent(eventId);
  try {
    const detail = await getFixtureDetail(canonicalEventId);
    return <FixtureInventoryWorkspace detail={detail} />;
  } catch {
    return (
      <>
        <div className="pa-detail-nav">
          <Link href="/">← Operations console</Link>
          <span className="pa-chip pa-chip-paper">PAPER MODE</span>
        </div>
        <div className="empty-live">
          Fixture {canonicalEventId} is not on the latest collection. No demo fixture is
          substituted. Collect live paper markets, then open the row from the operations console.
        </div>
      </>
    );
  }
}

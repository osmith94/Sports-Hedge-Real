import Link from "next/link";
import type { Metadata } from "next";

import { FixtureInventoryWorkspace } from "../../../../components/fixture-inventory";
import { getFixtureDetail } from "../../../../lib/api";
import { fixtureDetailUnavailableCopy } from "../../../../lib/fixture-detail-error";

type PageProps = {
  params: Promise<{ eventId: string }>;
  searchParams: Promise<{ bet?: string }>;
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

export default async function FixtureDetailPage({ params, searchParams }: PageProps) {
  const { eventId } = await params;
  const query = await searchParams;
  const canonicalEventId = decodeURIComponent(eventId);
  const focusOpportunityId = query.bet ? decodeURIComponent(query.bet) : null;
  try {
    const detail = await getFixtureDetail(canonicalEventId);
    return <FixtureInventoryWorkspace detail={detail} focusOpportunityId={focusOpportunityId} />;
  } catch (error) {
    return (
      <>
        <div className="pa-detail-nav">
          <Link href="/">← Operations console</Link>
          <span className="pa-chip pa-chip-paper">PAPER MODE</span>
        </div>
        <div className="empty-live">
          {fixtureDetailUnavailableCopy(error, canonicalEventId)}
        </div>
      </>
    );
  }
}

import { notFound } from "next/navigation";
import { TeamDashboardView } from "../../../components/teams/TeamDashboard";
import { getTeamDashboard, listTeams } from "../../../lib/team-fixtures";

export function generateStaticParams() {
  return listTeams().map((team) => ({ teamId: team.id }));
}

export default async function TeamDashboardPage({
  params,
}: {
  params: Promise<{ teamId: string }>;
}) {
  const { teamId } = await params;
  const dashboard = getTeamDashboard(teamId);
  if (!dashboard) notFound();
  return <TeamDashboardView dashboard={dashboard} />;
}

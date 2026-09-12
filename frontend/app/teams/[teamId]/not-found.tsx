import Link from "next/link";
import { DemoDataBanner } from "../../../components/teams/DemoDataBanner";

export default function TeamNotFound() {
  return (
    <>
      <div className="page-heading">
        <div>
          <div className="eyebrow">Team Explorer</div>
          <h1>Club not in the DEMO universe</h1>
          <p className="page-subtitle">
            That team id is not part of the fixture directory. Return to the Team Explorer and pick a labelled demo club.
          </p>
        </div>
        <div className="demo-label">DEMO / FIXTURE DATA</div>
      </div>
      <DemoDataBanner />
      <Link href="/teams">← Back to Team Explorer</Link>
    </>
  );
}

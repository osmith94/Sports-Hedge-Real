import type { TimeCurvePoint } from "../../lib/scenario-fixtures";
import styles from "./scenario-lab.module.css";

export function TimeResponseCurve({ points, teamLabel }: { points: TimeCurvePoint[]; teamLabel: string }) {
  const width = 520;
  const height = 168;
  const pad = { left: 36, right: 10, top: 12, bottom: 28 };
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const values = points.flatMap((point) => [point.teamEffect, point.leagueEffect]);
  const min = Math.min(0, ...values) - 0.15;
  const max = Math.max(0.4, ...values) + 0.15;
  const range = Math.max(max - min, 0.4);
  const x = (index: number) => pad.left + (index / Math.max(points.length - 1, 1)) * innerWidth;
  const y = (value: number) => pad.top + ((max - value) / range) * innerHeight;
  const teamLine = points.map((point, index) => `${x(index)},${y(point.teamEffect)}`).join(" ");
  const leagueLine = points.map((point, index) => `${x(index)},${y(point.leagueEffect)}`).join(" ");
  const ticks = [min, 0, max];

  return (
    <div className={styles.chartShell} aria-label={`${teamLabel} time-response curve versus league benchmark`}>
      <svg viewBox={`0 0 ${width} ${height}`} role="img">
        {ticks.map((tick) => (
          <g key={tick}>
            <line className={styles.chartGrid} x1={pad.left} x2={width - pad.right} y1={y(tick)} y2={y(tick)} />
            <text className={styles.chartLabel} x={2} y={y(tick) + 3}>
              {tick.toFixed(1)}
            </text>
          </g>
        ))}
        <polyline className={styles.chartLeague} points={leagueLine} />
        <polyline className={styles.chartTeam} points={teamLine} />
        {points.map((point, index) => (
          <g key={point.window}>
            <circle cx={x(index)} cy={y(point.teamEffect)} r="3.1" fill="var(--accent)" />
            <circle cx={x(index)} cy={y(point.leagueEffect)} r="2.4" fill="var(--blue)" />
            <text className={styles.chartLabel} x={x(index)} y={height - 8} textAnchor="middle">
              {point.label}
            </text>
          </g>
        ))}
      </svg>
    </div>
  );
}

type Point = { label: string; probability: number };

type Props = {
  points: Point[];
  eventIndex: number;
  eventLabel: string;
};

export function MarketReactionChart({ points, eventIndex, eventLabel }: Props) {
  const width = 760;
  const height = 280;
  const pad = { left: 42, right: 18, top: 22, bottom: 34 };
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;
  const values = points.map((point) => point.probability);
  const min = Math.floor((Math.min(...values) - 1) / 2) * 2;
  const max = Math.ceil((Math.max(...values) + 1) / 2) * 2;
  const range = Math.max(max - min, 1);
  const x = (index: number) => pad.left + (index / Math.max(points.length - 1, 1)) * innerWidth;
  const y = (value: number) => pad.top + ((max - value) / range) * innerHeight;
  const polyline = points.map((point, index) => `${x(index)},${y(point.probability)}`).join(" ");
  const area = `${pad.left},${pad.top + innerHeight} ${polyline} ${x(points.length - 1)},${pad.top + innerHeight}`;
  const eventX = x(eventIndex);
  const ticks = [min, min + range / 2, max];

  return (
    <div className="chart-shell" aria-label="Illustrative implied probability movement chart">
      <svg viewBox={`0 0 ${width} ${height}`} role="img">
        {ticks.map((tick) => (
          <g key={tick}>
            <line className="chart-grid" x1={pad.left} x2={width - pad.right} y1={y(tick)} y2={y(tick)} />
            <text className="chart-label" x={0} y={y(tick) + 3}>{tick.toFixed(0)}%</text>
          </g>
        ))}
        <polygon className="chart-area" points={area} />
        <polyline className="chart-line" points={polyline} />
        <line className="chart-marker" x1={eventX} x2={eventX} y1={pad.top} y2={pad.top + innerHeight} />
        <text className="chart-event-label" x={Math.min(eventX + 7, width - 115)} y={pad.top + 12}>{eventLabel}</text>
        {points.map((point, index) => (
          <g key={`${point.label}-${index}`}>
            <circle cx={x(index)} cy={y(point.probability)} r="3.2" fill="var(--accent)" />
            {(index === 0 || index === eventIndex || index === points.length - 1) && (
              <text className="chart-label" x={x(index)} y={height - 10} textAnchor="middle">{point.label}</text>
            )}
          </g>
        ))}
      </svg>
    </div>
  );
}

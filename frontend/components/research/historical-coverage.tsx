import { HistoricalCoverageReadModel } from "../../lib/api";

export function HistoricalCoverageSeam({ coverage }: { coverage: HistoricalCoverageReadModel | null }) {
  const available = coverage?.data_class === "REAL_HISTORICAL";
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header">
        <div>
          <div className="panel-title">Historical repository coverage</div>
          <div className="panel-meta">
            {coverage
              ? `${coverage.data_class.replaceAll("_", " ")} · analogue model ${coverage.analogue_model}`
              : "UNAVAILABLE"}
          </div>
        </div>
        <span className={available ? "status-badge" : "demo-chip"}>
          {available ? "REAL HISTORICAL" : "UNAVAILABLE"}
        </span>
      </div>
      <div className="panel-body">
        {coverage ? (
          <>
            <p className="section-copy">{coverage.analogue_note}</p>
            <p className="section-copy">{coverage.movement_semantics}.</p>
            {coverage.unavailable_reason ? <p className="section-copy">{coverage.unavailable_reason}</p> : null}
            <div className="metric-grid" style={{ marginTop: 12 }}>
              <div className="metric-card">
                <div className="metric-label">Matches</div>
                <div className="metric-value">{coverage.match_count ?? "—"}</div>
                <div className="metric-foot">Facts repository count</div>
              </div>
              <div className="metric-card">
                <div className="metric-label">Stored observations</div>
                <div className="metric-value">{coverage.stored_observation_count ?? "—"}</div>
                <div className="metric-foot">Odds repository count</div>
              </div>
              <div className="metric-card">
                <div className="metric-label">Same-line open→close</div>
                <div className="metric-value">{coverage.same_line_opening_closing_pairs ?? "—"}</div>
                <div className="metric-foot">Equivalent proposition/line only</div>
              </div>
              <div className="metric-card">
                <div className="metric-label">AH line shifts</div>
                <div className="metric-value">{coverage.asian_handicap_line_shifts ?? "—"}</div>
                <div className="metric-foot">Structural line changes, not price movement</div>
              </div>
            </div>
            <div className="table-wrap" style={{ marginTop: 12 }}>
              <table>
                <thead>
                  <tr>
                    <th>Competition</th>
                    <th>Season</th>
                    <th>Matches</th>
                  </tr>
                </thead>
                <tbody>
                  {coverage.competitions.length ? (
                    coverage.competitions.map((row) => (
                      <tr key={`${row.competition}-${row.season}`}>
                        <td>{row.competition}</td>
                        <td>{row.season}</td>
                        <td>{row.matches}</td>
                      </tr>
                    ))
                  ) : (
                    <tr>
                      <td colSpan={3}>No competition coverage rows in the facts repository.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </>
        ) : (
          <p className="section-copy">
            Historical coverage API is unreachable. Counts are not invented. Historical analogue scoring remains
            UNAVAILABLE.
          </p>
        )}
      </div>
    </section>
  );
}

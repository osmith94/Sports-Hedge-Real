import { HISTORICAL_SEAM } from "../../lib/demo/catalog";

export function HistoricalCoverageSeam() {
  return (
    <section className="panel" style={{ marginTop: 16 }}>
      <div className="panel-header">
        <div>
          <div className="panel-title">{HISTORICAL_SEAM.title}</div>
          <div className="panel-meta">{HISTORICAL_SEAM.status}</div>
        </div>
        <span className="demo-chip">UNAVAILABLE</span>
      </div>
      <div className="panel-body">
        <p className="section-copy">{HISTORICAL_SEAM.note}</p>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Competition</th>
                <th>Coverage</th>
                <th>Quality</th>
              </tr>
            </thead>
            <tbody>
              {HISTORICAL_SEAM.competitions.map((row) => (
                <tr key={row.competition}>
                  <td>{row.competition}</td>
                  <td>{row.coverage}</td>
                  <td>{row.quality}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

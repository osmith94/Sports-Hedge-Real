import Link from "next/link";
import styles from "../../app/research/research.module.css";
import {
  RESEARCH_HUB,
  bestAvailableQuote,
  formatDecimal,
  formatEv,
  formatPercent,
  formatPp,
  formatSrc,
  probabilityEdgePp,
  evPerPound,
  rankedResearchSignals,
  referenceQuote,
  scenarioLabHref,
  teamHref,
  valueStateFor,
  type ValueSignal,
} from "../../lib/research-fixtures";

function qualityChip(quality: ValueSignal["dataQuality"]): string {
  if (quality === "sufficient") return styles.chipGood;
  if (quality === "limited") return styles.chipWarn;
  return styles.chipBad;
}

function confidenceChip(band: ValueSignal["confidence"]): string {
  if (band === "high") return styles.chipGood;
  if (band === "medium") return styles.chipWarn;
  return styles.chipBad;
}

function SignalRow({ signal }: { signal: ValueSignal }) {
  const reference = referenceQuote(signal);
  const best = bestAvailableQuote(signal);
  const marketProb = best?.netDecimal ? 1 / best.netDecimal : null;
  const edgePp = marketProb === null ? null : probabilityEdgePp(signal.modelProbability, marketProb);
  const ev = best?.netDecimal != null ? evPerPound(signal.modelProbability, best.netDecimal) : null;
  const state = valueStateFor(signal);

  return (
    <tr>
      <td>
        <div className={styles.stack}>
          <Link className={styles.rowLink} href={teamHref(signal.teamId)}>
            {signal.teamName}
          </Link>
          <span className={styles.muted}>{signal.fixtureLabel}</span>
        </div>
      </td>
      <td>
        <div className={styles.stack}>
          <span className={styles.rowTitle}>{signal.scenarioTitle}</span>
          <span className={styles.muted}>{signal.scenarioId.replaceAll("-", " ")}</span>
          <span className={`${styles.chip} ${state === "VALUE" ? styles.chipGood : state === "NO_VALUE" ? styles.chipWarn : styles.chipBad}`}>
            {state.replaceAll("_", " ")}
          </span>
        </div>
      </td>
      <td>
        <div className={styles.stack}>
          <span>{signal.marketFamily}</span>
          <span className={styles.muted}>{signal.line}</span>
        </div>
      </td>
      <td>{signal.responseWindow}</td>
      <td className={styles.rowTitle}>{formatSrc(signal.src)}</td>
      <td>N={signal.sampleSize}</td>
      <td>
        <div className={styles.chipRow}>
          <span className={`${styles.chip} ${confidenceChip(signal.confidence)}`}>
            {signal.confidence} conf
          </span>
          <span className={styles.chip}>stab {(signal.stability * 100).toFixed(0)}</span>
          <span className={`${styles.chip} ${qualityChip(signal.dataQuality)}`}>{signal.dataQuality}</span>
        </div>
      </td>
      <td>{formatPercent(signal.modelProbability)}</td>
      <td>{marketProb === null ? "—" : formatPercent(marketProb)}</td>
      <td>
        {reference ? (
          <div className={styles.priceBlock}>
            <span>
              {formatDecimal(reference.decimalPrice)} · {reference.venue}
            </span>
            <span className={styles.muted}>reference</span>
          </div>
        ) : (
          "—"
        )}
      </td>
      <td>
        {best ? (
          <div className={styles.priceBlock}>
            <span className={styles.rowTitle}>
              net {best.netDecimal?.toFixed(3)} · {best.venue}
            </span>
            <span className={styles.muted}>
              headline {formatDecimal(best.decimalPrice)} · fee {best.feeKnown ? `${((best.feeRate ?? 0) * 100).toFixed(0)}% ${best.feeBasis.toLowerCase()}` : "UNKNOWN"}
            </span>
          </div>
        ) : (
          "—"
        )}
      </td>
      <td className={edgePp !== null && edgePp > 0 ? styles.edge : undefined}>
        {edgePp === null ? "—" : formatPp(edgePp)}
      </td>
      <td className={ev !== null && ev > 0 ? styles.edge : styles.softEdge}>
        {ev === null ? "—" : `${formatEv(ev)} / £1`}
      </td>
      <td>
        <div className={styles.stack}>
          <span>{signal.quoteFreshness}</span>
          {signal.polymarketOmittedReason ? (
            <span className={styles.omitNote}>{signal.polymarketOmittedReason}</span>
          ) : (
            <span className={styles.muted}>Polymarket included (settlement treated as equivalent)</span>
          )}
        </div>
      </td>
      <td>
        <div className={styles.rowActions}>
          <span className={`${styles.chip} ${styles.chipWarn}`}>DEMO</span>
          <Link
            className={styles.ghostLink}
            href={scenarioLabHref({
              teamId: signal.teamId,
              scenarioId: signal.scenarioId,
              metric: signal.metric,
              window: signal.responseWindow,
            })}
          >
            Scenario Lab
          </Link>
        </div>
      </td>
    </tr>
  );
}

export function ValueSignals() {
  return (
    <section className={styles.panel}>
      <div className={styles.panelHeader}>
        <div>
          <div className={styles.panelTitle}>Value signals / opportunities</div>
          <div className={styles.panelMeta}>{RESEARCH_HUB.rankingRule} Each row is labelled DEMO / FIXTURE DATA.</div>
        </div>
        <span className={styles.notArb}>NOT ARBITRAGE · NOT GUARANTEED PROFIT</span>
      </div>
      <div className={styles.tableWrap}>
        <table className={styles.table}>
          <thead>
            <tr>
              <th>Team / fixture</th>
              <th>Scenario</th>
              <th>Metric / market / line</th>
              <th>Window</th>
              <th>SRC</th>
              <th>N</th>
              <th>Confidence / stability / DQ</th>
              <th>Model p</th>
              <th>Market p</th>
              <th>Reference price</th>
              <th>Best net / effective</th>
              <th>Edge</th>
              <th>Est. EV / £1</th>
              <th>Quote freshness</th>
              <th>Data</th>
            </tr>
          </thead>
          <tbody>
            {rankedResearchSignals.map((signal) => (
              <SignalRow key={signal.signalId} signal={signal} />
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

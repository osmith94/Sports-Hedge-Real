import { rankedDemoValueSignals, type DemoValueSignal } from "../../lib/demo/value-signals";
import { DEMO_MAX_QUOTE_AGE_MINUTES, bestNetQuote, evPerPound } from "../../lib/demo/economics";
import type { ValueState } from "../../lib/demo/types";

export function valueStateLabel(state: ValueState): string {
  return state.replaceAll("_", " ");
}

export function ValueOverlay({ signal }: { signal: DemoValueSignal }) {
  const best = bestNetQuote(signal.quotes);
  const ev = best?.netDecimal != null ? evPerPound(signal.modelProbability, best.netDecimal) : null;
  return (
    <div className="value-overlay">
      <div className="value-overlay-kicker">Odds-weighted value · DEMO / FIXTURE</div>
      <div className="value-overlay-row">
        <span className={`value-state is-${signal.valueState.toLowerCase()}`}>{valueStateLabel(signal.valueState)}</span>
        <span>SRC {signal.src.toFixed(2)}</span>
        <span>model {(signal.modelProbability * 100).toFixed(1)}%</span>
        {best?.costAdjustedImplied != null ? (
          <span>net-implied {(best.costAdjustedImplied * 100).toFixed(1)}%</span>
        ) : (
          <span>net-implied unavailable</span>
        )}
        {best ? (
          <span>
            best net {best.netDecimal?.toFixed(3)} {best.venue} (headline {best.headlineDecimal.toFixed(2)})
          </span>
        ) : (
          <span>no comparable net quote</span>
        )}
        {ev !== null ? <span>EV/£1 {ev.toFixed(3)}</span> : null}
        {best ? (
          <span>
            quote age {best.quoteAgeMinutes}m · DEMO max {DEMO_MAX_QUOTE_AGE_MINUTES}m
          </span>
        ) : null}
      </div>
      <p className="value-overlay-note">{signal.rankingNote}</p>
    </div>
  );
}

export function FeaturedValueCallout() {
  const top = rankedDemoValueSignals().find((signal) => signal.valueState === "VALUE");
  const noValue = rankedDemoValueSignals().find((signal) => signal.valueState === "NO_VALUE");
  return (
    <div className="value-callouts">
      {top ? <ValueOverlay signal={top} /> : null}
      {noValue ? <ValueOverlay signal={noValue} /> : null}
    </div>
  );
}

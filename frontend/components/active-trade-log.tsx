"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import {
  ActiveTradeJournalEvent,
  ActiveTradeTimelineItem,
  PaperTrade,
  getActivePaperTrades,
  getActiveTradeEvents,
  getClosedPaperTrades,
} from "../lib/api";
import {
  ACTIVE_TRADE_LOG_EMPTY,
  activeTradeEventQuestion,
  activeTradePayloadBits,
} from "../lib/active-trade-timeline-display";

type Props = {
  recentItems?: ActiveTradeTimelineItem[] | null;
  presetTradeId?: string;
  initialEvents?: ActiveTradeJournalEvent[] | null;
  compact?: boolean;
};

function fixtureLabel(trade: PaperTrade): string {
  return trade.fixture_label || `${trade.home_team ?? "Unknown"} v ${trade.away_team ?? "Unknown"}`;
}

function uniqueTradeIds(items: ActiveTradeTimelineItem[] | null | undefined): string[] {
  const ids: string[] = [];
  for (const item of items ?? []) {
    if (item.trade_id && !ids.includes(item.trade_id)) ids.push(item.trade_id);
  }
  return ids;
}

function Timeline({ events }: { events: ActiveTradeJournalEvent[] }) {
  if (events.length === 0) {
    return (
      <div className="scan-note" aria-label="ACTIVE TRADE history empty">
        {ACTIVE_TRADE_LOG_EMPTY}
      </div>
    );
  }
  return (
    <ol className="trade-log-list" aria-label="ACTIVE TRADE history">
      {events.map((item) => {
        const bits = activeTradePayloadBits(item.payload);
        const when = item.occurred_at.replace("T", " ").slice(0, 19);
        return (
          <li key={item.event_id} className="trade-log-item">
            <div className="trade-log-question">{activeTradeEventQuestion(item.event_type)}</div>
            <div>
              {when}Z · {item.event_type} · {item.reason_code}
              {item.venue ? ` · ${item.venue}` : ""}
              {item.cycle_id ? ` · cycle ${item.cycle_id}` : ""}
            </div>
            <div>{item.operator_copy}</div>
            {bits ? <div className="panel-meta">{bits}</div> : null}
            {item.active_phase ? (
              <div className="panel-meta">phase {item.active_phase}</div>
            ) : null}
          </li>
        );
      })}
    </ol>
  );
}

export function ActiveTradeLog({
  recentItems,
  presetTradeId,
  initialEvents,
  compact = false,
}: Props) {
  const locked = Boolean(presetTradeId);
  const [open, setOpen] = useState(locked);
  const [selectedId, setSelectedId] = useState(presetTradeId ?? "");
  const [trades, setTrades] = useState<PaperTrade[]>([]);
  const [events, setEvents] = useState<ActiveTradeJournalEvent[] | null>(initialEvents ?? null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  const recentIds = useMemo(() => uniqueTradeIds(recentItems), [recentItems]);

  const loadCatalogue = useCallback(async () => {
    const [active, closed] = await Promise.all([
      getActivePaperTrades().catch(() => [] as PaperTrade[]),
      getClosedPaperTrades().catch(() => [] as PaperTrade[]),
    ]);
    const listed = [...active, ...closed];
    setTrades(listed);
    setSelectedId((current) => {
      if (presetTradeId) return presetTradeId;
      if (current) return current;
      return listed[0]?.trade_id ?? recentIds[0] ?? "";
    });
  }, [presetTradeId, recentIds]);

  useEffect(() => {
    if (!open || locked) return;
    void loadCatalogue();
  }, [open, locked, loadCatalogue]);

  useEffect(() => {
    if (!open || !selectedId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    void getActiveTradeEvents(`trade_id=${encodeURIComponent(selectedId)}&limit=200`)
      .then((rows) => {
        if (!cancelled) setEvents(rows);
      })
      .catch((err) => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Unable to load trade log");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, selectedId]);

  const options = useMemo(() => {
    const byId = new Map(trades.map((trade) => [trade.trade_id, trade]));
    const ids = [...trades.map((trade) => trade.trade_id)];
    for (const id of recentIds) {
      if (!ids.includes(id)) ids.push(id);
    }
    if (selectedId && !ids.includes(selectedId)) ids.unshift(selectedId);
    return ids.map((id) => {
      const trade = byId.get(id);
      const label = trade ? `${fixtureLabel(trade)} · ${trade.state}` : id;
      return { id, label };
    });
  }, [trades, recentIds, selectedId]);

  const body = (
    <>
      <p className="scan-advanced-copy">
        Persisted PAPER ACTIVE TRADE journal. Read-only. Opening this log does not call venues
        or start a scan. Facts already recorded during ACTIVE TRADE processing, including
        no-action 5s cycles.
      </p>
      {locked ? null : (
        <label className="scan-field">
          <span>Selected paper trade</span>
          <select
            aria-label="Trade log selected paper trade"
            value={selectedId}
            onChange={(event) => setSelectedId(event.target.value)}
          >
            {options.length === 0 ? (
              <option value="">No persisted ACTIVE TRADE yet</option>
            ) : (
              options.map((option) => (
                <option key={option.id} value={option.id}>
                  {option.label}
                </option>
              ))
            )}
          </select>
        </label>
      )}
      {error ? <div className="empty-live">{error}</div> : null}
      {loading ? <div className="scan-note">Loading persisted journal…</div> : null}
      {events ? <Timeline events={events} /> : null}
      {!loading && !events && !error ? (
        <div className="scan-note">{ACTIVE_TRADE_LOG_EMPTY}</div>
      ) : null}
    </>
  );

  if (locked) {
    return (
      <section className={compact ? "trade-log" : "panel"} id="trade-log">
        {compact ? (
          <div className="panel-title">Trade log</div>
        ) : (
          <div className="panel-header">
            <div className="panel-title">Trade log</div>
            <span className="demo-chip">PAPER JOURNAL · NO VENUE CALL</span>
          </div>
        )}
        <div className={compact ? undefined : "panel-body"}>{body}</div>
      </section>
    );
  }

  return (
    <details
      className="scan-advanced trade-log"
      open={open}
      onToggle={(event) => setOpen(event.currentTarget.open)}
    >
      <summary>Trade log · ACTIVE TRADE history</summary>
      {body}
    </details>
  );
}

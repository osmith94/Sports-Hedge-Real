"use client";

import { useState } from "react";

import { API_BASE } from "../lib/api";

export function GenerateMatchingReport() {
  const [message, setMessage] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function downloadReport() {
    setBusy(true);
    setMessage(null);
    try {
      const response = await fetch(`${API_BASE}/operations/universe-matching-report`);
      if (response.status === 404) {
        setMessage("No UNIVERSE matching evidence is retained yet. This does not start a scan.");
        return;
      }
      if (!response.ok) {
        setMessage("Matching report unavailable. No report was fabricated.");
        return;
      }
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "universe-matching-report.json";
      link.click();
      URL.revokeObjectURL(url);
      setMessage("Downloaded the latest retained UNIVERSE matching report. Diagnostic only. Not a scan.");
    } catch {
      setMessage("Paper API unreachable. No report was fabricated.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="ops-section matching-report" id="matching-report">
      <div className="section-label">
        <span>Fixture identity</span>
        <span className="status-badge">DIAGNOSTIC</span>
      </div>
      <p className="section-copy">
        Generate the latest UNIVERSE matching report from retained identity evidence. Upload it for
        review. This does not start a scan or change scanner state.
      </p>
      <button type="button" className="scan-button-secondary" onClick={downloadReport} disabled={busy}>
        {busy ? "Generating…" : "Generate matching report"}
      </button>
      {message ? <p className="section-copy">{message}</p> : null}
    </section>
  );
}

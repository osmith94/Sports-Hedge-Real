"use client";

import { useMemo, useState } from "react";

import {
  MappingProposedRule,
  MappingProvenance,
  MappingReviewCandidate,
  MappingVerdict,
  canActivateLearnedRule,
  mappingConfidencePercent,
  mappingProvenanceLabel,
  shouldOfferMappingVerify,
} from "../lib/mapping-verification";

export type MappingVerificationPanelProps = {
  candidate: MappingReviewCandidate;
  promptText: string;
  provenance?: MappingProvenance | null;
  proposedRule?: MappingProposedRule | null;
  activationBlockedReason?: string | null;
  dataClass?: string;
  onCopyPrompt?: (prompt: string) => void;
  onInterpret?: (input: { chatgptText: string; verdict: MappingVerdict }) => void;
  onConfirm?: (input: { chatgptText: string; verdict: MappingVerdict }) => void;
};

/**
 * Reusable Verify-mapping seam for #169 / Wave D Opportunity Monitor.
 * Opportunity Monitor mounts this panel with current radar evidence only.
 */
export function MappingVerificationPanel({
  candidate,
  promptText,
  provenance,
  proposedRule,
  activationBlockedReason = null,
  dataClass = "LIVE PAPER / operator review",
  onCopyPrompt,
  onInterpret,
  onConfirm,
}: MappingVerificationPanelProps) {
  const confidence = candidate.current_confidence ?? 0;
  const offerVerify = shouldOfferMappingVerify(confidence);
  const [chatgptText, setChatgptText] = useState("");
  const [verdict, setVerdict] = useState<MappingVerdict>("ambiguous");
  const [confirmed, setConfirmed] = useState(false);

  const canSave = useMemo(
    () =>
      canActivateLearnedRule({
        verdict,
        operatorConfirmed: confirmed,
        conflictingFields: candidate.conflicting_fields,
        activationBlockedReason,
      }),
    [verdict, confirmed, candidate.conflicting_fields, activationBlockedReason],
  );

  return (
    <section className="mapping-verify-panel" data-offer-verify={offerVerify ? "yes" : "no"}>
      <header className="mapping-verify-header">
        <div>
          <div className="eyebrow">Verify mapping · PAPER MODE</div>
          <h2 className="mapping-verify-title">Human-in-the-loop mapping review</h2>
        </div>
        <span className="status-badge">{dataClass}</span>
      </header>
      <p className="panel-meta">
        Mapping {mappingConfidencePercent(confidence)} · {mappingProvenanceLabel(provenance)}
      </p>
      {!offerVerify ? (
        <p className="mapping-verify-note">
          Native 100% mapping does not require Verify. Learned 100% mappings stay labelled
          operator_verified rather than provider truth.
        </p>
      ) : (
        <p className="mapping-verify-note">
          Confidence is below 100%. Copy the prompt, obtain a ChatGPT verdict, then confirm a
          narrow venue-scoped rule. AI text cannot activate a rule by itself.
        </p>
      )}
      <div className="mapping-verify-grid">
        {candidate.sides.map((side) => (
          <article key={`${side.venue}-${side.source_event_id}`} className="mapping-verify-side">
            <h3>{side.venue}</h3>
            <dl>
              <div>
                <dt>Canonical candidate</dt>
                <dd>{side.current_canonical_candidate || `${side.raw_home_team} vs ${side.raw_away_team}`}</dd>
              </div>
              <div>
                <dt>Raw teams</dt>
                <dd>
                  {side.raw_home_team} vs {side.raw_away_team}
                </dd>
              </div>
              <div>
                <dt>Competition / kickoff</dt>
                <dd>
                  {side.raw_competition} · {side.kickoff_utc}
                </dd>
              </div>
              <div>
                <dt>Market</dt>
                <dd>
                  {side.raw_market_name || side.raw_market_type || "—"} · {side.family} / {side.period} /{" "}
                  {side.settlement_scope}
                </dd>
              </div>
              <div>
                <dt>Source IDs</dt>
                <dd>
                  {side.source_event_id} / {side.source_market_id}
                </dd>
              </div>
            </dl>
          </article>
        ))}
      </div>
      {candidate.conflicting_fields && candidate.conflicting_fields.length > 0 ? (
        <p className="mapping-verify-conflict">
          Conflicting/missing: {candidate.conflicting_fields.join(", ")}
        </p>
      ) : null}
      {activationBlockedReason ? (
        <p className="mapping-verify-conflict">
          Activation blocked: {activationBlockedReason}
        </p>
      ) : null}
      <label className="mapping-verify-prompt">
        Verification prompt
        <textarea readOnly value={promptText} rows={10} />
      </label>
      <div className="mapping-verify-actions">
        <button type="button" onClick={() => onCopyPrompt?.(promptText)}>
          Copy verification prompt
        </button>
      </div>
      <label className="mapping-verify-prompt">
        ChatGPT response
        <textarea
          value={chatgptText}
          onChange={(event) => setChatgptText(event.target.value)}
          rows={6}
          placeholder="Paste VERIFIED / NOT VERIFIED / AMBIGUOUS…"
        />
      </label>
      <label>
        Verdict
        <select value={verdict} onChange={(event) => setVerdict(event.target.value as MappingVerdict)}>
          <option value="verified">VERIFIED</option>
          <option value="not_verified">NOT VERIFIED</option>
          <option value="ambiguous">AMBIGUOUS</option>
        </select>
      </label>
      {proposedRule ? (
        <div className="mapping-verify-rule">
          <h3>Proposed learned rule</h3>
          <p>
            {proposedRule.rule_type} · {proposedRule.venue} · {proposedRule.raw_pattern} →{" "}
            {proposedRule.canonical_transformation}
          </p>
          <p className="panel-meta">
            {proposedRule.rule_id} v{proposedRule.version} · {proposedRule.source}
          </p>
        </div>
      ) : null}
      <label className="mapping-verify-confirm">
        <input
          type="checkbox"
          checked={confirmed}
          onChange={(event) => setConfirmed(event.target.checked)}
        />
        I confirm this narrow rule after reviewing evidence. ChatGPT output is not sufficient.
      </label>
      <div className="mapping-verify-actions">
        <button
          type="button"
          onClick={() => onInterpret?.({ chatgptText, verdict })}
        >
          Interpret without saving
        </button>
        <button
          type="button"
          disabled={!canSave}
          onClick={() => onConfirm?.({ chatgptText, verdict })}
        >
          Save learned rule
        </button>
      </div>
    </section>
  );
}

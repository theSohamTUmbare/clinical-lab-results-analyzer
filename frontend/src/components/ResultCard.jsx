import { useState } from 'react'
import SeverityBadge, { tone } from './SeverityBadge'
import RangeGauge from './RangeGauge'
import EvidencePanel from './EvidencePanel'

// Human-readable names for the engine's internal flags. Anything not listed
// falls back to the raw flag, so a new flag degrades to something ugly but
// truthful rather than disappearing.
const FLAG_LABELS = {
  name_inferred: 'Test name inferred',
  panic_limit_breached: 'Action limit breached',
  borderline: 'Borderline',
  unit_mismatch: 'Unit mismatch',
  censored_value: 'Censored value',
  censored_value_normal_assumed: 'Censored, assumed within interval',
  needs_human_review: 'Needs human review',
  sex_specific_range_unavailable: 'Sex-specific interval unavailable',
  numeric_value_on_qualitative_test: 'Numeric value on a qualitative test',
  knowledge_server_unavailable: 'Knowledge server unavailable',
}

export default function ResultCard({ result, defaultOpen }) {
  const [open, setOpen] = useState(Boolean(defaultOpen))
  const [showEvidence, setShowEvidence] = useState(false)
  const t = tone(result.status)
  const exp = result.explanation
  const isAi = exp.source === 'ai'

  return (
    <article className={`result ${t.key}`}>
      <button
        className="result-head"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <div className="result-title">
          <div className="name">{result.display_name}</div>
          <div className="meta">
            {result.category}
            {result.test_name !== result.display_name ? ` · reported as "${result.test_name}"` : ''}
          </div>
        </div>

        <div className="result-value">
          <div className="v">
            {result.value_display} {result.unit}
          </div>
          <div className="r">
            {result.unit_converted && result.canonical_value != null
              ? `= ${result.canonical_value} ${result.canonical_unit} · ref ${result.reference_text}`
              : result.reference_text
                ? `ref ${result.reference_text}`
                : 'no interval'}
          </div>
        </div>

        <SeverityBadge status={result.status} />
        <span className={`caret ${open ? 'open' : ''}`} aria-hidden="true">▶</span>
      </button>

      {open && (
        <div className="result-body">
          {result.reference_low != null && result.canonical_value != null && (
            <div className="section">
              <RangeGauge
                low={result.reference_low}
                high={result.reference_high}
                value={result.canonical_value}
                status={result.status}
                unit={result.canonical_unit}
              />
            </div>
          )}

          {result.error && (
            <div className="section">
              <div className="callout unknown">
                <strong>Not classified.</strong> {result.error}
              </div>
            </div>
          )}

          {result.flags.length > 0 && (
            <div className="section row" style={{ gap: 6 }}>
              {result.flags.map((f) => (
                <span key={f} className="chip flag">
                  {FLAG_LABELS[f] ?? f}
                </span>
              ))}
            </div>
          )}

          <div className="section">
            <h4>
              Explanation
              <span style={{ marginLeft: 8 }}>
                <span className={`chip ${isAi ? 'ai' : ''}`}>
                  {isAi ? 'AI generated' : 'Rule based'}
                </span>
                {result.verification?.passed && (
                  <span className="chip verified" style={{ marginLeft: 5 }}>
                    ✓ grounding verified
                  </span>
                )}
              </span>
            </h4>

            <dl className="prose">
              <dt>What this test measures</dt>
              <dd>{exp.what_it_measures}</dd>
              <dt>Why it was flagged</dt>
              <dd>{exp.why_flagged}</dd>
              <dt>Clinical significance</dt>
              <dd>{exp.clinical_significance}</dd>
              <dt>In plain language</dt>
              <dd>{exp.patient_friendly}</dd>
            </dl>

            {exp.source_note && (
              <p className="faint" style={{ fontSize: 11.5, marginTop: 8 }}>
                {exp.source_note}
              </p>
            )}
          </div>

          <div className="section">
            <h4>Suggested next steps</h4>
            <div className={`callout ${t.key === 'normal' ? '' : t.key}`}>
              <div className="row" style={{ gap: 6, marginBottom: 2 }}>
                <span className="chip">{result.next_steps.urgency}</span>
                <span className="chip">{result.next_steps.sla}</span>
                <span className="chip">{result.next_steps.specialty}</span>
              </div>
              <ul>
                {result.next_steps.actions.map((a, i) => (
                  <li key={i}>{a}</li>
                ))}
              </ul>
              <p className="faint" style={{ fontSize: 11.5, marginTop: 8 }}>
                Retrieved from the care-pathway table ({result.next_steps.source}), not
                generated by the model.
              </p>
            </div>

            {exp.ai_suggested_next_steps?.length > 0 && (
              <div className="callout" style={{ marginTop: 8 }}>
                <span className="chip ai">AI suggested</span>
                <ul>
                  {exp.ai_suggested_next_steps.map((a, i) => (
                    <li key={i}>{a}</li>
                  ))}
                </ul>
              </div>
            )}
          </div>

          {result.verification && (
            <div className="section">
              <h4>Grounding checks</h4>
              <ul className="checks">
                {result.verification.checks.map((c) => (
                  <li key={c.id} className={c.passed ? 'pass' : 'fail'}>
                    <span className="tick">{c.passed ? '✓' : '✕'}</span>
                    <span>
                      {c.name}
                      <span className="c-detail"> — {c.detail}</span>
                    </span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          <div className="section">
            <button className="btn btn-ghost btn-sm" onClick={() => setShowEvidence((v) => !v)}>
              {showEvidence ? 'Hide' : 'Show'} full evidence trail
            </button>
            {showEvidence && (
              <div style={{ marginTop: 9 }}>
                <EvidencePanel result={result} />
              </div>
            )}
          </div>
        </div>
      )}
    </article>
  )
}

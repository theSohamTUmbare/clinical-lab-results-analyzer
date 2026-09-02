// The explainability core: why this result got this status, in full.
//
// Everything shown here is emitted by the deterministic engine, so the panel is
// a rendering of the audit trail rather than a summary of it. If a reviewer
// disagrees with a flag, this is where they find out exactly which rule fired
// and which reference interval it fired against.

const STEP_LABELS = {
  resolve: 'Resolve test name',
  parse_value: 'Read the value',
  harmonise_units: 'Harmonise units',
  select_reference_range: 'Select reference interval',
  apply_rules: 'Apply severity rules',
  classify: 'Classify',
}

const SOURCE_LABELS = {
  row_reference_range: "The issuing laboratory's own interval, supplied with the result",
  knowledge_base: 'Knowledge base reference interval',
  qualitative_scale: 'Qualitative grading scale for this test',
}

export default function EvidencePanel({ result }) {
  const ev = result.evidence
  if (!ev) return null

  const range = ev.reference_range ?? {}
  const resolution = ev.resolution ?? {}
  const rule = ev.rule ?? {}
  const conversion = ev.unit_conversion
  const lookup = ev.fallback_lookup

  return (
    <div className="evidence stack" style={{ gap: 13 }}>
      <div>
        <h4 style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.06em',
                     color: 'var(--text-faint)', marginBottom: 7 }}>
          Decision
        </h4>
        <p className="prose" style={{ fontSize: 13 }}>{rule.rationale}</p>
        <dl className="kv" style={{ marginTop: 9 }}>
          <dt>Rule applied</dt>
          <dd>{rule.id}</dd>
          {rule.bands && typeof rule.bands === 'object' && rule.bands.normal && (
            <>
              <dt>Severity bands</dt>
              <dd>
                Normal {rule.bands.normal} · Warning {rule.bands.warning} · Critical{' '}
                {rule.bands.critical}
              </dd>
            </>
          )}
          {result.deviation_index != null && (
            <>
              <dt>Deviation index</dt>
              <dd>
                {result.deviation_index} interval widths
                {ev.deviation?.interpretation ? ` (${ev.deviation.interpretation})` : ''}
              </dd>
            </>
          )}
          {rule.panic_limit && (
            <>
              <dt>Action limit</dt>
              <dd>
                {rule.panic_limit.bound} {rule.panic_limit.limit} {result.canonical_unit}
              </dd>
            </>
          )}
        </dl>
      </div>

      <div>
        <h4 style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.06em',
                     color: 'var(--text-faint)', marginBottom: 7 }}>
          Reference interval used
        </h4>
        <dl className="kv">
          <dt>Interval</dt>
          <dd>{result.reference_text || 'not applicable'}</dd>
          <dt>Provenance</dt>
          <dd>{SOURCE_LABELS[range.source] ?? range.source ?? 'unknown'}</dd>
          {range.matched_on_sex && (
            <>
              <dt>Sex profile</dt>
              <dd>{range.matched_on_sex}</dd>
            </>
          )}
          {range.citation && (
            <>
              <dt>Citation</dt>
              <dd>{range.citation}</dd>
            </>
          )}
        </dl>
      </div>

      <div>
        <h4 style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.06em',
                     color: 'var(--text-faint)', marginBottom: 7 }}>
          How the input was interpreted
        </h4>
        <dl className="kv">
          <dt>Reported as</dt>
          <dd>
            {resolution.input}
            {result.loinc ? ` · LOINC ${result.loinc}` : ''}
          </dd>
          <dt>Matched to</dt>
          <dd>
            {result.display_name} ({resolution.matched_via}
            {resolution.confidence != null
              ? `, confidence ${Math.round(resolution.confidence * 100)}%`
              : ''}
            )
          </dd>
          {conversion && (
            <>
              <dt>Unit conversion</dt>
              <dd>
                {conversion.original} {conversion.from} × {conversion.factor} ={' '}
                {conversion.converted} {conversion.to}
              </dd>
            </>
          )}
          {ev.parsed_value?.note && (
            <>
              <dt>Note</dt>
              <dd>{ev.parsed_value.note}</dd>
            </>
          )}
          {resolution.note && (
            <>
              <dt>Resolver note</dt>
              <dd>{resolution.note}</dd>
            </>
          )}
        </dl>
      </div>

      {lookup && (
        <div className="callout unknown">
          <strong style={{ fontSize: 12.5 }}>Fallback lookup via MCP</strong>
          <p className="muted" style={{ fontSize: 12.5, marginTop: 4 }}>
            {lookup.found
              ? `reference_range_lookup matched "${lookup.display_name}".`
              : lookup.reason}
          </p>
          {lookup.guidance && (
            <p className="faint" style={{ fontSize: 12, marginTop: 4 }}>{lookup.guidance}</p>
          )}
        </div>
      )}

      <div>
        <h4 style={{ fontSize: 11, textTransform: 'uppercase', letterSpacing: '0.06em',
                     color: 'var(--text-faint)', marginBottom: 7 }}>
          Pipeline steps
        </h4>
        <ul className="steps">
          {(ev.steps ?? []).map((s, i) => (
            <li key={i}>
              <span className="s-name">{STEP_LABELS[s.step] ?? s.step}</span>
              <span className="s-out">{s.outcome}</span>
              {s.note && <span className="faint" style={{ fontSize: 12 }}>{s.note}</span>}
            </li>
          ))}
        </ul>
      </div>

      <p className="faint" style={{ fontSize: 11.5 }}>
        Knowledge base v{ev.kb_version}. No language model contributed to any step above.
      </p>
    </div>
  )
}

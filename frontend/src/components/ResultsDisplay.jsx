import ResultCard from './ResultCard'
import { tone } from './SeverityBadge'

// Results are already ordered by the backend (severity, then how far outside the
// interval each one sits). The frontend renders that order rather than
// re-sorting, so what a clinician sees matches what the audit trail says.

const GROUP_TITLES = {
  Critical: 'Critical — immediate review',
  Warning: 'Warning — clinician review',
  Unknown: 'Needs human review',
  Normal: 'Within reference interval',
}

export default function ResultsDisplay({ report }) {
  const { summary, groups, pipeline, ai, ingest } = report

  return (
    <div className="stack">
      <SummaryCard report={report} />

      {ingest && <IngestNote ingest={ingest} />}

      {groups.map((group) => (
        <section key={group.status}>
          <h3 className="group-head">
            <span>{GROUP_TITLES[group.status]}</span>
            <span className="faint">({group.count})</span>
          </h3>
          {group.results.map((r) => (
            <ResultCard
              key={r.id}
              result={r}
              // Critical results open by default: the point of triage is that
              // the urgent things do not need a click to be read.
              defaultOpen={group.status === 'Critical'}
            />
          ))}
        </section>
      ))}

      <PipelineTrace pipeline={pipeline} ai={ai} summary={summary} report={report} />
    </div>
  )
}

function SummaryCard({ report }) {
  const s = report.summary
  const t = tone(s.highest_severity)
  return (
    <div className="card">
      <div className="card-body stack" style={{ gap: 13 }}>
        <div className="summary-head">
          <h2>
            <span aria-hidden="true" style={{ marginRight: 8 }}>{t.icon}</span>
            {s.headline}
          </h2>
          <span className="faint mono" style={{ fontSize: 11.5 }}>
            {report.request_id}
          </span>
        </div>

        <div className="tiles">
          <Tile n={s.critical} label="Critical" cls="critical" />
          <Tile n={s.warning} label="Warning" cls="warning" />
          <Tile n={s.normal} label="Normal" cls="normal" />
          <Tile n={s.unknown} label="Needs review" cls="unknown" />
          <Tile n={s.total} label="Total tests" cls="" />
        </div>

        {s.needs_review > 0 && (
          <div className="alert warn">
            {s.needs_review} result{s.needs_review > 1 ? 's' : ''} could not be classified
            automatically and {s.needs_review > 1 ? 'have' : 'has'} been routed for manual
            review rather than being given a severity that cannot be justified.
          </div>
        )}
      </div>
    </div>
  )
}

const Tile = ({ n, label, cls }) => (
  <div className={`tile ${cls}`}>
    <div className="n">{n}</div>
    <div className="l">{label}</div>
  </div>
)

function IngestNote({ ingest }) {
  const mapped = Object.entries(ingest.column_mapping ?? {})
  return (
    <details className="card">
      <summary className="card-head" style={{ cursor: 'pointer', borderBottom: 'none' }}>
        <h2>File read</h2>
        <span className="hint">
          {ingest.filename} · {ingest.rows_accepted} rows · delimiter “{ingest.delimiter}”
          {ingest.warnings?.length ? ` · ${ingest.warnings.length} warning(s)` : ''}
        </span>
      </summary>
      <div className="card-body stack" style={{ gap: 11, paddingTop: 0 }}>
        <div>
          <h4 className="faint" style={{ fontSize: 11, textTransform: 'uppercase',
                                         letterSpacing: '0.06em', marginBottom: 6 }}>
            Column mapping
          </h4>
          <dl className="kv">
            {mapped.map(([field, column]) => (
              <div key={field} style={{ display: 'contents' }}>
                <dt>{field}</dt>
                <dd>{column}</dd>
              </div>
            ))}
          </dl>
        </div>
        {ingest.unmapped_columns?.length > 0 && (
          <p className="faint" style={{ fontSize: 12 }}>
            Columns not used: {ingest.unmapped_columns.join(', ')}
          </p>
        )}
        {ingest.warnings?.length > 0 && (
          <ul className="muted" style={{ fontSize: 12.5, margin: 0, paddingLeft: 18 }}>
            {ingest.warnings.map((w, i) => <li key={i}>{w}</li>)}
          </ul>
        )}
      </div>
    </details>
  )
}

function PipelineTrace({ pipeline, ai, report }) {
  return (
    <div className="card">
      <div className="card-head">
        <h2>How this analysis was produced</h2>
        <span className="hint">
          Knowledge base v{report.knowledge_base_version || 'n/a'}
        </span>
      </div>
      <div className="card-body stack" style={{ gap: 12 }}>
        <div className="pipeline">
          {pipeline.map((step) => (
            <div className="pstep" key={step.step}>
              <div className="p-top">
                <span className="p-name">{step.step}</span>
                <span className="p-ms">{step.duration_ms} ms</span>
              </div>
              <p className="p-detail">{step.detail}</p>
              {step.tool_calls?.length > 0 && (
                <div className="p-tools">
                  {step.tool_calls.map((t) => (
                    <span key={t} className="chip mono">{t}</span>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>

        <dl className="kv">
          <dt>AI provider</dt>
          <dd>{ai.provider} · {ai.model}</dd>
          <dt>AI used</dt>
          <dd>{ai.used ? `yes (${ai.latency_ms} ms)` : 'no'}</dd>
          <dt>Status</dt>
          <dd>{ai.detail}</dd>
          <dt>Explanations verified</dt>
          <dd>
            {ai.verified_ok} passed · {ai.replaced_after_failed_check} replaced after a
            failed grounding check · {ai.explanations_from_cache} served from cache
          </dd>
        </dl>

        <p className="faint" style={{ fontSize: 11.5 }}>{report.disclaimer}</p>
      </div>
    </div>
  )
}

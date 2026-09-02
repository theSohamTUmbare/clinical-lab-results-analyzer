import { useEffect, useState } from 'react'
import { getEvaluation } from '../lib/api'

// Scores the classifier against the Kaggle dataset's own Status column.
//
// This is the honest version of "our classification works": the dataset ships
// clinician-assigned labels, so agreement is measured rather than asserted.

export default function ValidationPanel() {
  const [report, setReport] = useState(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(true)

  useEffect(() => {
    getEvaluation()
      .then(setReport)
      .catch((e) => setError(e.message))
      .finally(() => setBusy(false))
  }, [])

  if (busy) return <div className="card"><div className="card-body muted"><span className="spinner" /> Scoring the classifier…</div></div>
  if (error) return <div className="alert">{error}</div>
  if (!report) return null

  const statuses = ['Critical', 'Warning', 'Normal', 'Unknown']
  const expectedRows = Object.keys(report.by_expected)

  return (
    <div className="stack">
      <div className="card">
        <div className="card-head">
          <h2>Classifier validation</h2>
          <span className="hint">{report.dataset}</span>
        </div>
        <div className="card-body stack" style={{ gap: 14 }}>
          <div className="score">
            <span className="big">{(report.accuracy * 100).toFixed(1)}%</span>
            <span className="muted">
              agreement — {report.agreements} of {report.rows} labelled rows
            </span>
          </div>

          <p className="muted" style={{ fontSize: 13 }}>{report.note}</p>

          <div>
            <h4 className="faint" style={{ fontSize: 11, textTransform: 'uppercase',
                                           letterSpacing: '0.06em', marginBottom: 7 }}>
              Dataset label vs engine status
            </h4>
            <div style={{ overflowX: 'auto' }}>
              <table className="matrix">
                <thead>
                  <tr>
                    <th>Dataset label</th>
                    {statuses.map((s) => <th key={s}>{s}</th>)}
                  </tr>
                </thead>
                <tbody>
                  {expectedRows.map((label) => (
                    <tr key={label}>
                      <td><strong>{label}</strong></td>
                      {statuses.map((s) => (
                        <td key={s} className="num">
                          {report.by_expected[label][s] ?? '·'}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>

          {report.disagreements.length === 0 ? (
            <div className="callout" style={{ background: 'var(--normal-bg)',
                                              borderColor: 'var(--normal-border)' }}>
              <strong>No disagreements.</strong> Every labelled row was classified with the
              same abnormality and direction as the dataset, including the Turkish test
              names and the qualitative dipstick results.
            </div>
          ) : (
            <div>
              <h4 className="faint" style={{ fontSize: 11, textTransform: 'uppercase',
                                             letterSpacing: '0.06em', marginBottom: 7 }}>
                Disagreements ({report.disagreements.length})
              </h4>
              <table className="matrix">
                <thead>
                  <tr>
                    <th>Test</th><th>Value</th><th>Expected</th><th>Predicted</th><th>Note</th>
                  </tr>
                </thead>
                <tbody>
                  {report.disagreements.map((d, i) => (
                    <tr key={i}>
                      <td>{d.test_name}</td>
                      <td className="num">{d.value}</td>
                      <td>{d.expected}</td>
                      <td>{d.predicted} {d.direction}</td>
                      <td className="muted" style={{ fontSize: 12 }}>{d.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>

      <div className="card">
        <div className="card-head"><h2>Why this number is meaningful</h2></div>
        <div className="card-body prose muted" style={{ fontSize: 13.5 }}>
          <p style={{ marginBottom: 9 }}>
            The classifier never sees the dataset's <code>Status</code> column — it works
            from the test name, the value, the unit and the reference interval, exactly as
            it does for an uploaded file. Agreement is therefore a check against labels the
            engine had no access to, not a restatement of its own output.
          </p>
          <p>
            Because no language model participates in classification, this score is
            reproducible: the same dataset yields the same number on every run, and the
            engine's own test suite pins each severity decision to an expected value.
          </p>
        </div>
      </div>
    </div>
  )
}

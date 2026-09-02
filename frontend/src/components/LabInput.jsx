import { useEffect, useRef, useState } from 'react'
import { getCatalog, getSamples } from '../lib/api'

// Three ways in, because a hackathon demo, a real CSV export and a single
// ad-hoc value are genuinely different tasks:
//   samples  - one click, for showing the thing working
//   upload   - the real path, any CSV whose columns we can map
//   manual   - type a few results, with the catalogue behind an autocomplete

const BLANK_ROW = { test_name: '', value: '', unit: '' }

export default function LabInput({ onAnalyze, busy, aiAvailable }) {
  const [mode, setMode] = useState('samples')
  const [samples, setSamples] = useState([])
  const [catalog, setCatalog] = useState([])
  const [rows, setRows] = useState([{ ...BLANK_ROW }, { ...BLANK_ROW }, { ...BLANK_ROW }])
  const [sex, setSex] = useState('unspecified')
  const [age, setAge] = useState('')
  const [explain, setExplain] = useState(true)
  const [dragging, setDragging] = useState(false)
  const [localError, setLocalError] = useState('')
  const fileRef = useRef(null)

  useEffect(() => {
    getSamples().then((d) => setSamples(d.samples ?? [])).catch(() => {})
    getCatalog().then((d) => setCatalog(d.tests ?? [])).catch(() => {})
  }, [])

  const patient = () => ({
    sex,
    age_years: age ? Number(age) : null,
    reference: null,
  })

  function submitManual(event) {
    event.preventDefault()
    setLocalError('')
    const labs = rows
      .filter((r) => r.test_name.trim() && String(r.value).trim())
      .map((r) => ({
        test_name: r.test_name.trim(),
        value: String(r.value).trim(),
        unit: r.unit.trim() || null,
      }))
    if (labs.length === 0) {
      setLocalError('Enter at least one test name and result.')
      return
    }
    onAnalyze({ kind: 'manual', labs, patient: patient(), explain })
  }

  function handleFile(file) {
    setLocalError('')
    if (!file) return
    if (!/\.(csv|tsv|txt)$/i.test(file.name)) {
      setLocalError('Please choose a .csv, .tsv or .txt file.')
      return
    }
    onAnalyze({ kind: 'csv', file, sex, ageYears: age, explain })
  }

  const updateRow = (i, key, value) =>
    setRows((rs) => rs.map((r, idx) => (idx === i ? { ...r, [key]: value } : r)))

  // Offer the canonical unit as soon as a known test is picked, so the most
  // common source of a unit mismatch simply does not arise.
  function onTestNameChange(i, value) {
    const match = catalog.find(
      (c) => c.display_name.toLowerCase() === value.trim().toLowerCase()
    )
    setRows((rs) =>
      rs.map((r, idx) =>
        idx === i
          ? { ...r, test_name: value, unit: match && !r.unit ? match.unit : r.unit }
          : r
      )
    )
  }

  return (
    <div className="card">
      <div className="card-head">
        <h2>Lab results</h2>
        <span className="hint">
          {catalog.length > 0 ? `${catalog.length} tests supported` : 'loading catalogue…'}
        </span>
      </div>

      <div className="card-body stack">
        <div className="modes" role="tablist">
          {[
            ['samples', 'Sample panels'],
            ['upload', 'Upload CSV'],
            ['manual', 'Enter manually'],
          ].map(([id, label]) => (
            <button
              key={id}
              role="tab"
              className="mode"
              aria-selected={mode === id}
              onClick={() => { setMode(id); setLocalError('') }}
            >
              {label}
            </button>
          ))}
        </div>

        {localError && <div className="alert">{localError}</div>}

        {mode === 'samples' && (
          <div className="samples">
            {samples.map((s) => (
              <button
                key={s.id}
                className="sample"
                disabled={busy || !s.available}
                onClick={() => onAnalyze({ kind: 'sample', id: s.id, explain })}
              >
                <strong>{s.label}</strong>
                <span>{s.description}</span>
              </button>
            ))}
            {samples.length === 0 && <p className="muted">Loading sample panels…</p>}
          </div>
        )}

        {mode === 'upload' && (
          <div
            className={`dropzone ${dragging ? 'over' : ''}`}
            onDragOver={(e) => { e.preventDefault(); setDragging(true) }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault()
              setDragging(false)
              handleFile(e.dataTransfer.files?.[0])
            }}
          >
            <p style={{ marginBottom: 10 }}>
              Drop a lab CSV here, or choose a file.
              <br />
              Column names are matched by synonym, so most lab exports work as-is.
            </p>
            <button
              className="btn btn-primary"
              disabled={busy}
              onClick={() => fileRef.current?.click()}
            >
              Choose file
            </button>
            <input
              ref={fileRef}
              type="file"
              accept=".csv,.tsv,.txt"
              hidden
              onChange={(e) => {
                handleFile(e.target.files?.[0])
                e.target.value = ''
              }}
            />
          </div>
        )}

        {mode === 'manual' && (
          <form onSubmit={submitManual} className="stack" style={{ gap: 11 }}>
            <table className="entry-table">
              <thead>
                <tr>
                  <th style={{ width: '52%' }}>Test name</th>
                  <th style={{ width: '22%' }}>Result</th>
                  <th style={{ width: '20%' }}>Unit</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {rows.map((row, i) => (
                  <tr key={i}>
                    <td>
                      <input
                        list="test-catalog"
                        value={row.test_name}
                        placeholder="e.g. Hemoglobin, Potasyum, HbA1c"
                        onChange={(e) => onTestNameChange(i, e.target.value)}
                      />
                    </td>
                    <td>
                      <input
                        value={row.value}
                        placeholder="12.9 or Negatif"
                        onChange={(e) => updateRow(i, 'value', e.target.value)}
                      />
                    </td>
                    <td>
                      <input
                        value={row.unit}
                        placeholder="g/dL"
                        onChange={(e) => updateRow(i, 'unit', e.target.value)}
                      />
                    </td>
                    <td>
                      <button
                        type="button"
                        className="icon-btn"
                        title="Remove row"
                        disabled={rows.length === 1}
                        onClick={() => setRows((rs) => rs.filter((_, idx) => idx !== i))}
                      >
                        ×
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>

            <datalist id="test-catalog">
              {catalog.map((c) => (
                <option key={c.key} value={c.display_name}>
                  {c.category} · {c.unit}
                </option>
              ))}
            </datalist>

            <div className="row">
              <button
                type="button"
                className="btn btn-sm"
                onClick={() => setRows((rs) => [...rs, { ...BLANK_ROW }])}
              >
                + Add row
              </button>
              <button type="submit" className="btn btn-primary" disabled={busy}>
                {busy ? <><span className="spinner" /> Analysing…</> : 'Analyse results'}
              </button>
            </div>
          </form>
        )}

        <div className="row" style={{ gap: 14, paddingTop: 4, borderTop: '1px solid var(--border)' }}>
          <label className="toggle">
            Sex
            <select
              className="field"
              style={{ width: 'auto' }}
              value={sex}
              onChange={(e) => setSex(e.target.value)}
            >
              <option value="unspecified">Unspecified</option>
              <option value="female">Female</option>
              <option value="male">Male</option>
            </select>
          </label>
          <label className="toggle">
            Age
            <input
              className="field"
              style={{ width: 76 }}
              type="number"
              min="0"
              max="130"
              value={age}
              placeholder="yrs"
              onChange={(e) => setAge(e.target.value)}
            />
          </label>
          <label className="toggle" title={
            aiAvailable
              ? 'Turn off to see the deterministic explanations on their own.'
              : 'No API key configured, so rule-based explanations are used regardless.'
          }>
            <input
              type="checkbox"
              checked={explain && aiAvailable}
              disabled={!aiAvailable}
              onChange={(e) => setExplain(e.target.checked)}
            />
            AI explanations
          </label>
          <span className="faint" style={{ fontSize: 11.5, marginLeft: 'auto' }}>
            Sex and age select the reference interval; neither is sent to the AI provider.
          </span>
        </div>
      </div>
    </div>
  )
}

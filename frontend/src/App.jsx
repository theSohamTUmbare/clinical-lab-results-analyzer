import { useEffect, useState } from 'react'
import LabInput from './components/LabInput'
import ResultsDisplay from './components/ResultsDisplay'
import ValidationPanel from './components/ValidationPanel'
import { analyzeCsv, analyzeLabs, analyzeSample, getHealth } from './lib/api'

export default function App() {
  const [tab, setTab] = useState('analyze')
  const [health, setHealth] = useState(null)
  const [report, setReport] = useState(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    getHealth().then(setHealth).catch((e) => setError(e.message))
  }, [])

  const aiAvailable = Boolean(health?.ai_provider?.available)

  async function handleAnalyze(request) {
    setBusy(true)
    setError('')
    try {
      let result
      if (request.kind === 'sample') {
        result = await analyzeSample(request.id, request.explain)
      } else if (request.kind === 'csv') {
        result = await analyzeCsv(request.file, {
          sex: request.sex,
          ageYears: request.ageYears,
          explain: request.explain,
        })
      } else {
        result = await analyzeLabs(request.labs, request.patient, {
          explain: request.explain,
          include_evidence: true,
        })
      }
      setReport(result)
      // Bring the summary into view: on a long panel the results otherwise land
      // below the fold and it looks as though nothing happened.
      requestAnimationFrame(() =>
        document.getElementById('results')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
      )
    } catch (e) {
      setError(e.message)
      setReport(null)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="app">
      <header className="masthead">
        <div className="masthead-inner">
          <div className="brand">
            <div className="brand-mark" aria-hidden="true">&#9877;</div>
            <div>
              <h1>Clinical Lab Results Analyzer</h1>
              <div className="sub">
                Deterministic classification over MCP · AI explanations, verified
              </div>
            </div>
          </div>

          <nav className="tabs" role="tablist">
            <button role="tab" className="tab" aria-selected={tab === 'analyze'}
                    onClick={() => setTab('analyze')}>
              Analyse
            </button>
            <button role="tab" className="tab" aria-selected={tab === 'validation'}
                    onClick={() => setTab('validation')}>
              Validation
            </button>
          </nav>
        </div>
      </header>

      <main className="main stack">
        {health && <ModeBanner health={health} />}
        {error && <div className="alert">{error}</div>}

        {tab === 'analyze' ? (
          <>
            <LabInput onAnalyze={handleAnalyze} busy={busy} aiAvailable={aiAvailable} />
            <div id="results">
              {busy && (
                <div className="card">
                  <div className="card-body muted">
                    <span className="spinner" /> Classifying through the MCP knowledge
                    server{aiAvailable ? ', then generating and verifying explanations' : ''}…
                  </div>
                </div>
              )}
              {!busy && report && <ResultsDisplay report={report} />}
              {!busy && !report && !error && (
                <div className="card">
                  <div className="card-body muted" style={{ fontSize: 13.5 }}>
                    Pick a sample panel, upload a CSV, or type a few results to begin.
                  </div>
                </div>
              )}
            </div>
          </>
        ) : (
          <ValidationPanel />
        )}
      </main>

      <footer className="page-foot">
        {health?.disclaimer}
      </footer>
    </div>
  )
}

function ModeBanner({ health }) {
  if (!health.mcp?.connected) {
    return (
      <div className="alert">
        The clinical knowledge server is not connected ({health.mcp?.detail}). Nothing can
        be classified until it is.
      </div>
    )
  }
  if (!health.ai_provider?.available) {
    return (
      <div className="alert info">
        <strong>Rule-based mode.</strong> No <code>GEMINI_API_KEY</code> is configured, so
        explanations come from the deterministic templates. Classification is unaffected —
        it never uses AI. Add a key to <code>backend/.env</code> and restart to enable AI
        explanations.
      </div>
    )
  }
  return null
}

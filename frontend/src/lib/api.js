// All backend calls live here so components never build URLs themselves.
// Requests go to /api, which Vite proxies to the FastAPI server in development.

const BASE = import.meta.env.VITE_API_BASE ?? '/api'

async function request(path, options = {}) {
  let response
  try {
    response = await fetch(`${BASE}${path}`, options)
  } catch {
    throw new Error(
      'Could not reach the backend. Start it with "python run.py" in the backend folder.'
    )
  }

  if (!response.ok) {
    // FastAPI puts the useful message in `detail`; fall back to the status text.
    let detail = `${response.status} ${response.statusText}`
    try {
      const body = await response.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : detail
    } catch {
      /* non-JSON error body; the status line is the best we have */
    }
    throw new Error(detail)
  }
  return response.json()
}

export const getHealth = () => request('/health')
export const getCatalog = () => request('/catalog')
export const getSamples = () => request('/samples')
export const getEvaluation = () => request('/evaluate')

export const analyzeLabs = (labs, patient, options) =>
  request('/analyze_labs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ labs, patient, options }),
  })

export const analyzeSample = (id, explain) =>
  request(`/analyze_sample/${id}?explain=${explain}`, { method: 'POST' })

export function analyzeCsv(file, { sex, ageYears, explain }) {
  const form = new FormData()
  form.append('file', file)
  form.append('explain', String(explain))
  if (sex && sex !== 'unspecified') form.append('sex', sex)
  if (ageYears) form.append('age_years', String(ageYears))
  return request('/analyze_csv', { method: 'POST', body: form })
}

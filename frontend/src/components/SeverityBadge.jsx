// The single source of truth for how a severity looks and reads.
// Every other component imports `tone()` rather than mapping status to colour
// itself, so a status can never be red in one place and amber in another.

const TONES = {
  Critical: { key: 'critical', icon: '\u{1F6A8}', label: 'Critical' },
  Warning: { key: 'warning', icon: '\u26A0\uFE0F', label: 'Warning' },
  Normal: { key: 'normal', icon: '\u2713', label: 'Normal' },
  Unknown: { key: 'unknown', icon: '?', label: 'Needs review' },
}

export const tone = (status) => TONES[status] ?? TONES.Unknown

export default function SeverityBadge({ status, showIcon = true }) {
  const t = tone(status)
  return (
    <span className={`badge ${t.key}`} title={`Classified as ${t.label}`}>
      {showIcon ? <span aria-hidden="true">{t.icon}</span> : <span className="dot" />}
      {t.label}
    </span>
  )
}

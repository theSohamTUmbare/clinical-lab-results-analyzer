import { tone } from './SeverityBadge'

// Where the value sits relative to its reference interval, at a glance.
//
// The scale is padded by one interval width on each side so that a result just
// outside the interval is visibly just outside, rather than pinned to the edge.
// Values further out than that are clamped, and the axis labels say so.
export default function RangeGauge({ low, high, value, status, unit }) {
  if (low == null || high == null || value == null || high <= low) return null

  const width = high - low
  const min = low - width
  const max = high + width
  const pct = (n) => ((n - min) / (max - min)) * 100
  const clamped = Math.min(Math.max(value, min), max)
  const offScale = value < min || value > max

  return (
    <div className="gauge">
      <div className="gauge-track">
        <div
          className="gauge-normal"
          style={{ left: `${pct(low)}%`, width: `${pct(high) - pct(low)}%` }}
        />
        <div
          className={`gauge-marker ${tone(status).key}`}
          style={{ left: `${pct(clamped)}%` }}
          title={`${value} ${unit ?? ''}`}
        />
      </div>
      <div className="gauge-scale">
        <span>{offScale && value < min ? `\u2039 ${fmt(value)}` : fmt(min)}</span>
        <span>
          {`${fmt(low)}\u2013${fmt(high)}`} {unit}
        </span>
        <span>{offScale && value > max ? `${fmt(value)} \u203A` : fmt(max)}</span>
      </div>
    </div>
  )
}

const fmt = (n) => (Number.isInteger(n) ? String(n) : Number(n.toFixed(2)).toString())

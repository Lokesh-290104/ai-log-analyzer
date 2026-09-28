import { lazy, Suspense } from 'react'
import type { UploadInfo } from '../types'
import { fmtBytes, fmtDate, fmtInt, fmtTime } from '../format'

// Recharts is most of the bundle; load it only once there is a log to chart.
const TimelineChart = lazy(() => import('./TimelineChart').then((m) => ({ default: m.TimelineChart })))

export function OverviewPanel({ upload }: { upload: UploadInfo }) {
  const o = upload.overview
  const p = upload.parse_stats
  const services = Object.entries(o.by_service)
  return (
    <section className="card" aria-labelledby="overview-title">
      <div className="card-head">
        <h2 id="overview-title">2. What's in {upload.name}</h2>
        <span className="muted small">
          {fmtBytes(upload.size_bytes)} · computed by code, no AI
        </span>
      </div>

      <div className="tiles">
        <div className="tile">
          <div className="tile-label">Entries</div>
          <div className="tile-value">{fmtInt(o.total_entries)}</div>
        </div>
        <div className="tile">
          <div className="tile-label">Errors (ERROR + FATAL)</div>
          <div className="tile-value">{fmtInt(o.error_entries)}</div>
          <div className="tile-sub">{o.error_share_pct}% of entries</div>
        </div>
        <div className="tile">
          <div className="tile-label">Services</div>
          <div className="tile-value">{o.service_count}</div>
        </div>
        <div className="tile">
          <div className="tile-label">Time range (UTC)</div>
          <div className="tile-value tile-value-sm">
            {fmtTime(o.first_ts, false)} – {fmtTime(o.last_ts, false)}
          </div>
          <div className="tile-sub">{fmtDate(o.first_ts)}</div>
        </div>
      </div>

      {p.unparsed_lines > 0 && (
        <p className="notice" role="status">
          {fmtInt(p.unparsed_lines)} line{p.unparsed_lines === 1 ? ' was' : 's were'} not recognized and skipped
          {p.unparsed_samples[0] && (
            <>
              {' '}
              (e.g. line {p.unparsed_samples[0].line_no}: <code>{p.unparsed_samples[0].text}</code>)
            </>
          )}
          .
        </p>
      )}

      {o.timeline.buckets.length > 0 && (
        <Suspense fallback={<div className="chart-placeholder muted small">Loading chart…</div>}>
          <TimelineChart buckets={o.timeline.buckets} bucketMinutes={o.timeline.bucket_minutes} />
        </Suspense>
      )}

      <div className="split">
        <div>
          <h3>Top error messages</h3>
          {o.top_errors.rows.length === 0 ? (
            <p className="muted">No errors in this log.</p>
          ) : (
            <ol className="top-errors">
              {o.top_errors.rows.map((row) => (
                <li key={row.signature}>
                  <span className="count">{fmtInt(row.count)}×</span>
                  <span className="sig" title={row.example}>
                    {row.signature}
                  </span>
                  <span className="muted small">
                    {row.services.join(', ')} · {fmtTime(row.first_ts)}–{fmtTime(row.last_ts)}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>
        <div>
          <h3>Entries by service</h3>
          <table className="compact">
            <tbody>
              {services.map(([name, count]) => (
                <tr key={name}>
                  <td>{name}</td>
                  <td className="num">{fmtInt(count)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  )
}
